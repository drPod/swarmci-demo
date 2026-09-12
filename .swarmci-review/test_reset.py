"""Real Penpot regression. Uses Playwright and a native Penpot-exported document."""

import json
import os
import re
import uuid
import zipfile
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).parent
BASE = os.environ.get("PENPOT_URL", "http://localhost:9001")
OUTPUT = Path(os.environ.get("PENPOT_EVIDENCE", "artifacts/penpot-verified"))


def named_seed(dest, name):
    with zipfile.ZipFile(ROOT / "nested-reset.penpot") as source, zipfile.ZipFile(dest, "w") as output:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "manifest.json":
                value = json.loads(data)
                value["files"][0]["name"] = name
                data = json.dumps(value).encode()
            elif item.filename.startswith("files/") and item.filename.count("/") == 1:
                value = json.loads(data)
                value["name"] = name
                data = json.dumps(value).encode()
            output.writestr(item, data)


@pytest.mark.parametrize("variant", ["direct-child", "nested-groups"])
@pytest.mark.parametrize("repetition", [1, 2])
def test_reset_restores_original_component(variant, repetition, tmp_path):
    email, password = os.environ.get("PENPOT_EMAIL"), os.environ.get("PENPOT_PASSWORD")
    if not email or not password:
        pytest.skip("Set disposable PENPOT_EMAIL and PENPOT_PASSWORD")
    folder = OUTPUT / f"{variant}-{repetition}"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"SwarmCI {variant} {uuid.uuid4().hex[:6]}"
    seed = tmp_path / "seed.penpot"
    named_seed(seed, name)
    result = {
        "variant": variant,
        "repetition": repetition,
        "version": "2.17.2",
        "source_issue": "https://github.com/penpot/penpot/issues/11656",
        "source_pr": "https://github.com/penpot/penpot/pull/11604",
        "status": "error",
        "steps": [],
        "error_page_observed": False,
        "expected": "Reset overrides restores the nested Blue Button component.",
    }
    (folder / "result.json").write_text(json.dumps(result, indent=2))
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        setup = browser.new_context(viewport={"width": 1440, "height": 960})
        page = setup.new_page()
        page.goto(BASE)
        page.get_by_placeholder("Work email").fill(email)
        page.get_by_placeholder("Password", exact=True).fill(password)
        page.get_by_role("button", name="Continue", exact=True).click()
        page.locator('input[type=file][accept=".penpot,.zip"]').first.wait_for(
            state="attached", timeout=30000
        )
        page.locator('input[type=file][accept=".penpot,.zip"]').first.set_input_files(str(seed))
        page.locator("input[value=Continue]").click()
        page.locator("input[value=Accept]").click(timeout=30000)
        page.get_by_role("button", name=name, exact=True).dblclick()
        page.get_by_test_id("layer-row").first.wait_for(timeout=30000)
        file_url = page.url
        state = setup.storage_state()
        setup.close()
        context = browser.new_context(
            storage_state=state,
            viewport={"width": 1440, "height": 960},
            record_video_dir=str(folder),
            record_video_size={"width": 1440, "height": 960},
        )
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = context.new_page()

        def step(label):
            result["steps"].append(label)
            page.evaluate(
                """label => {let e=document.getElementById('swarmci-caption');if(!e){e=document.createElement('div');e.id='swarmci-caption';e.style='position:fixed;bottom:18px;left:355px;right:330px;background:#101820ee;color:#fff;padding:14px 20px;border:1px solid #73e6b5;border-radius:10px;z-index:99999;font:16px system-ui;pointer-events:none';document.body.append(e)}e.textContent=label}""",
                label,
            )
            page.wait_for_timeout(500)
            page.screenshot(path=str(folder / f"step-{len(result['steps']):02}.png"))

        try:
            page.goto(file_url)
            layers = page.locator("span[id^=layer-name]")
            parent = "Card" if variant == "nested-groups" else "Control Card"
            layers.filter(has_text=re.compile("^" + re.escape(parent) + "$")).click(timeout=30000)
            step(
                "1 · Prepared parent component: "
                + ("two intermediate groups" if variant == "nested-groups" else "direct child, no group")
            )
            page.keyboard.press("ControlOrMeta+d")
            page.wait_for_timeout(700)
            for _ in range(15):
                page.keyboard.press("Shift+ArrowDown")
            page.wait_for_timeout(500)
            row = page.locator("[data-testid=layer-row][aria-checked=true]")
            row.get_by_test_id("toggle-content").click()
            if variant == "nested-groups":
                for group in ["Group", "Nested Group"]:
                    page.get_by_test_id("layer-row").filter(
                        has_text=re.compile("^" + group + "$")
                    ).get_by_test_id("toggle-content").click()
            expect(layers.filter(has_text=re.compile("^Blue Button$"))).to_have_count(2)
            layers.filter(has_text=re.compile("^Blue Button$")).first.click()
            step("2 · Duplicate the parent and select its nested Blue Button")
            page.get_by_role("button", name=re.compile("Blue Button")).click()
            page.get_by_text("Orange Button", exact=True).last.click()
            page.wait_for_timeout(600)
            expect(layers.filter(has_text=re.compile("^Orange Button$"))).to_have_count(2)
            step("3 · Swap the nested component to Orange Button")
            page.screenshot(path=str(folder / "before.png"))
            page.locator("[data-testid=layer-row][aria-checked=true]").click(button="right")
            step("4 · Choose Reset overrides — expected: Blue Button returns")
            page.get_by_text("Reset overrides", exact=True).click()
            page.wait_for_timeout(1800)
            names = layers.all_text_contents()
            result["layer_names_after_reset"] = names
            result["error_page_observed"] = (
                page.get_by_text(re.compile("Internal error|Something went wrong")).count() > 0
            )
            restored = names.count("Orange Button") == 1 and names.count("Blue Button") >= 2
            result["status"] = "passed" if restored else "failed"
            result["observed"] = (
                "Original Blue Button restored."
                if restored
                else "Orange Button remains after Reset overrides."
            )
            step(("PASS · " if restored else "FAIL · ") + result["observed"])
            page.screenshot(path=str(folder / "after.png"))
            assert restored, result["observed"]
        except Exception as exc:
            result["error"] = str(exc)[:1500]
            raise
        finally:
            (folder / "result.json").write_text(json.dumps(result, indent=2))
            context.tracing.stop(path=str(folder / "trace.zip"))
            context.close()
            browser.close()
