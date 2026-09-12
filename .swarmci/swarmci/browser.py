import asyncio
import json
import socket
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from swarmci.adapters.tracing import attributes, rename, summary, traced
from swarmci.config import settings
from swarmci.models import Action, Target


class BrowserSession:
    """Playwright owns isolation, video and deterministic replay; BU attaches over CDP."""

    def __init__(self, target: Target, folder: Path, cloud=False):
        self.target, self.folder, self.cloud = target, folder, cloud
        self.errors = []
        self.cdp_url = ""
        self.cloud_id = None
        self.fixture = None

    @traced("Open browser session")
    async def __aenter__(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        self.pw = await async_playwright().start()
        self.tmp = tempfile.TemporaryDirectory(prefix="swarmci-")
        try:
            if self.cloud:
                from browser_use_sdk import AsyncBrowserUse

                self.cloud_client = AsyncBrowserUse(api_key=settings.browser_use_api_key)
                remote = await self.cloud_client.browsers.create()
                self.cloud_id = remote.id
                self.cdp_url = remote.cdp_url
                self.browser = await self.pw.chromium.connect_over_cdp(self.cdp_url)
                self.context = await self.browser.new_context(
                    record_video_dir=str(self.folder), viewport={"width": 1440, "height": 900}
                )
            else:
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                self.cdp_url = f"http://127.0.0.1:{port}"
                self.context = await self.pw.chromium.launch_persistent_context(
                    self.tmp.name,
                    headless=settings.headless,
                    args=[f"--remote-debugging-port={port}"],
                    viewport={"width": 1440, "height": 900},
                    record_video_dir=str(self.folder),
                    record_video_size={"width": 1440, "height": 900},
                )
            self.context.set_default_timeout(7000)
            if self.target.storage_state:
                storage = json.loads(Path(self.target.storage_state).read_text())
                await self.context.add_cookies(storage.get("cookies", []))
                for origin in storage.get("origins", []):
                    await self.context.add_init_script(
                        "if(location.origin === "
                        + json.dumps(origin["origin"])
                        + ") {"
                        + "".join(
                            "localStorage.setItem("
                            + json.dumps(item["name"])
                            + ","
                            + json.dumps(item["value"])
                            + ");"
                            for item in origin["localStorage"]
                        )
                        + "}"
                    )
            await self.context.tracing.start(screenshots=True, snapshots=True, sources=True)
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            initial_url = self.target.url
            if self.target.fixture_adapter == "plane":
                from swarmci.plane import PlaneFixture

                self.fixture = PlaneFixture(self.target.url)
                initial_url = await self.fixture.create(self.context)
                self.page._swarm_fixture = self.fixture
            self.page.on("pageerror", lambda e: self.errors.append({"kind": "pageerror", "message": str(e)}))
            self.page.on(
                "console",
                lambda m: (
                    self.errors.append({"kind": "console", "message": m.text}) if m.type == "error" else None
                ),
            )
            self.page.on(
                "response",
                lambda r: (
                    self.errors.append({"kind": "http", "status": r.status, "url": r.url})
                    if r.status >= 500
                    else None
                ),
            )
            self.page.on(
                "crash", lambda _: self.errors.append({"kind": "crash", "message": "Browser page crashed"})
            )
            await self.page.goto(initial_url, wait_until="domcontentloaded")
            if self.fixture:
                await self.page.get_by_text("Launch checklist", exact=True).first.wait_for(timeout=60000)
            return self
        except BaseException:
            await self.close()
            raise

    @traced("Save evidence and close browser")
    async def close(self):
        try:
            if hasattr(self, "context"):
                try:
                    await self.context.tracing.stop(path=str(self.folder / "trace.zip"))
                finally:
                    await self.context.close()
        finally:
            if self.cloud_id:
                try:
                    await self.cloud_client.browsers.stop(self.cloud_id)
                finally:
                    self.cloud_id = None
            await self.pw.stop()
            self.tmp.cleanup()
            if self.fixture:
                await self.fixture.close()

    async def __aexit__(self, *exc):
        await self.close()

    @traced("Browser action", kind="tool")
    async def act(self, a: Action):
        if self.fixture:
            a = self.fixture.action(a, restore=True)
        rename(f"Browser: {a.kind}" + (f" — {a.label[:100]}" if a.label else ""))
        summary(inputs={"action": a.kind, "label": a.label[:200]})
        attributes(action_kind=a.kind)
        p = self.page
        loc = p.locator(a.selector) if a.selector else None
        if a.kind == "click":
            if loc is not None:
                await loc.click(button=a.button)
            elif a.x is not None and a.y is not None:
                await p.mouse.click(a.x, a.y, button=a.button)
            else:
                raise ValueError("Click requires a selector or coordinates")
        elif a.kind == "fill":
            if a.clear:
                await loc.fill(a.value)
            else:
                await loc.press("End")
                await loc.press_sequentially(a.value)
        elif a.kind == "press":
            await (loc.press(a.value) if loc is not None else p.keyboard.press(a.value))
        elif a.kind == "select":
            await loc.select_option(label=a.value)
        elif a.kind == "goto":
            allowed = {urlparse(self.target.url).hostname, *self.target.allowed_domains}
            if urlparse(a.value).hostname not in allowed:
                raise ValueError("Navigation outside target domains")
            await p.goto(a.value, wait_until="domcontentloaded")
        elif a.kind == "back":
            await p.go_back(wait_until="domcontentloaded")
        elif a.kind == "reload":
            await p.reload(wait_until="domcontentloaded")
        elif a.kind == "scroll":
            if loc is not None:
                await loc.hover()
            await p.mouse.wheel(a.x or 0, a.y or 0)
        elif a.kind == "drag":
            await p.mouse.move(a.x, a.y)
            await p.mouse.down()
            await p.mouse.move(a.end_x, a.end_y, steps=15)
            await p.mouse.up()
        elif a.kind == "upload":
            await loc.set_input_files(a.value)
        elif a.kind == "wait":
            await asyncio.sleep(min(float(a.value or 0.3), 5))
        await self.settle()

    async def settle(self):
        if self.fixture:
            # Plane applies optimistic updates and debounced editor saves. Replay
            # and model-driven recording must sample after the same settling gate.
            await self.page.wait_for_timeout(750)
            await self.page.wait_for_load_state("networkidle", timeout=15000)
            await self.fixture.snapshot()
        else:
            await self.page.wait_for_timeout(180)

    @traced("Replay action sequence")
    async def replay(self, path, annotate=False):
        rename(f"Replay prefix · {len(path)} actions")
        summary(
            inputs={
                "steps": [
                    {"step": i + 1, "action": a.label or a.kind}
                    for i, a in enumerate(path)
                    if isinstance(a, Action)
                ]
            }
        )
        for i, raw in enumerate(path):
            a = raw if isinstance(raw, Action) else Action.model_validate(raw)
            if annotate:
                await self.page.evaluate(
                    """([step, total, label]) => {
                  let el = document.getElementById('__swarmci_evidence');
                  if (!el) {
                    el = document.createElement('div'); el.id='__swarmci_evidence';
                    el.style.cssText='position:fixed;bottom:20px;left:50%;transform:translateX(-50%);z-index:2147483647;pointer-events:none;background:#13291ff2;color:#c7f8dc;border:1px solid #72ac88;padding:12px 24px;border-radius:9px;font:14px system-ui;box-shadow:0 8px 24px #0003';
                    document.documentElement.appendChild(el);
                  }
                  el.textContent='✳ SwarmCI  ·  '+step+' / '+total+'  ·  '+label;
                }""",
                    [i + 1, len(path), a.label or a.kind],
                )
                await self.page.wait_for_timeout(350)
            await self.act(a)

    @traced("Evaluate product assertions")
    async def checks(self, action=None):
        """Only explicit product invariants produce failures; console evidence is secondary."""
        results = []
        for rule in self.target.assertions:
            if rule.kind == "plane_archive":
                if not self.fixture:
                    raise ValueError("Plane archive assertion requires isolated Plane fixture")
                continue
            if rule.after_action and (
                action is None
                or rule.after_action.lower()
                not in (action.label + " " + action.selector + " " + action.value).lower()
            ):
                continue
            loc = self.page.locator(rule.selector) if rule.selector else None
            if rule.kind == "visible":
                ok = await loc.is_visible()
            elif rule.kind == "hidden":
                ok = not await loc.is_visible()
            elif rule.kind == "text":
                ok = await loc.count() > 0 and rule.expected in await loc.inner_text()
            elif rule.kind == "url":
                ok = rule.expected in self.page.url
            else:
                ok = await loc.count() > 0 and await loc.get_attribute(rule.attribute) == rule.expected
            results.append({"name": rule.name, "passed": ok, "rule": rule.model_dump()})
        if self.target.failure_selector:
            results.append(
                {
                    "name": "No application error screen",
                    "passed": not await self.page.locator(self.target.failure_selector).is_visible(),
                    "rule": {
                        "kind": "hidden",
                        "selector": self.target.failure_selector,
                        "name": "No application error screen",
                    },
                }
            )
        if self.fixture:
            results.extend(await self.fixture.checks(self.page))
        attributes(checks_total=len(results), checks_failed=sum(not r["passed"] for r in results))
        failed = sum(not r["passed"] for r in results)
        rename(
            f"Assertions · {failed} failed / {len(results)} checked"
            if failed
            else f"Assertions · {len(results)} passed"
            if results
            else "Assertions · None applicable"
        )
        summary(outputs=[{"name": r["name"], "passed": r["passed"]} for r in results])
        return results
