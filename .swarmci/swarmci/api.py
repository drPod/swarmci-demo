import asyncio
import io
import json
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from swarmci.adapters.issues import import_github
from swarmci.adapters.tracing import initialize, shutdown, traced
from swarmci.adapters.tracing import status as tracing_status
from swarmci.config import settings
from swarmci.models import RunConfig
from swarmci.runner import Coordinator
from swarmci.store import Store

ROOT = Path(__file__).resolve().parent
settings.artifact_dir.mkdir(parents=True, exist_ok=True)
store = Store(settings.data_dir / "swarmci.db")
coordinator = Coordinator(store)


@asynccontextmanager
async def lifespan(app):
    initialize()
    store.recover()
    yield
    tasks = list(coordinator.tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.to_thread(shutdown)


app = FastAPI(title="SwarmCI", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
app.mount("/artifacts", StaticFiles(directory=settings.artifact_dir), name="artifacts")


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/fixture")
async def fixture():
    return FileResponse(ROOT / "static" / "fixture.html")


@app.get("/api/health")
async def health():
    async def model(url, key):
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                r = await client.get(url.rstrip("/") + "/models", headers={"Authorization": "Bearer " + key})
                return "connected" if r.is_success else "unavailable"
        except Exception:
            return "offline"

    bu, gemma = await asyncio.gather(
        model(settings.bu_base_url, settings.bu_api_key),
        model(settings.gemma_base_url, settings.gemma_api_key),
    )
    return {
        "status": "ok",
        "integrations": {
            "Browser Use": bu,
            "Gemma": gemma,
            "Lambda": "key configured" if settings.lambda_api_key else "not configured",
            "Nango": "configured" if settings.nango_secret_key else "not configured",
            "Respan": tracing_status(),
            "AgentMail": "deferred" if not settings.agentmail_api_key else "configured",
        },
    }


@app.get("/api/targets")
async def targets():
    return {
        p.stem: json.loads(p.read_text())
        for p in Path("targets").glob("*.json")
        if not p.name.endswith(".issue.json")
    }


@app.get("/api/runs")
async def runs():
    return store.runs()


@app.post("/api/runs", status_code=201)
@traced("API: start exploration")
async def start(config: RunConfig):
    if not config.target.seed_ready:
        raise HTTPException(422, config.target.notes)
    if not config.target.assertions and not config.target.failure_selector:
        raise HTTPException(422, "Configure at least one product assertion before exploring.")
    if config.cloud_browser and not settings.browser_use_api_key:
        raise HTTPException(422, "BROWSER_USE_API_KEY is required for hosted browsers")
    if config.engine == "fixture" and config.target.isolation != "fixture":
        raise HTTPException(422, "Fixture exploration requires a fixture target")
    if settings.fixture_url and config.target.isolation == "fixture":
        config.target.url = settings.fixture_url
    if config.engine in ("browser-use", "gemma"):
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                r = await client.get(
                    (settings.gemma_base_url if config.engine == "gemma" else settings.bu_base_url).rstrip(
                        "/"
                    )
                    + "/models",
                    headers={
                        "Authorization": "Bearer "
                        + (settings.gemma_api_key if config.engine == "gemma" else settings.bu_api_key)
                    },
                )
                r.raise_for_status()
        except Exception:
            raise HTTPException(
                422,
                "Selected model endpoint is offline. Start local inference or use the controlled fixture.",
            )
    return {"id": coordinator.start(config)}


@app.get("/api/runs/{run}")
async def snapshot(run: str):
    try:
        return store.snapshot(run)
    except KeyError:
        raise HTTPException(404, "Run not found")


@app.post("/api/runs/{run}/cancel")
@traced("API: cancel exploration")
async def cancel(run: str):
    if task := coordinator.tasks.get(run):
        task.cancel()
        return {"status": "cancelling"}
    raise HTTPException(409, "Run is not active")


@app.get("/api/runs/{run}/events")
async def events(run: str, request: Request, after: int = 0):
    await snapshot(run)
    after = max(after, int(request.headers.get("last-event-id", "0")))

    async def stream():
        nonlocal after
        while not await request.is_disconnected():
            rows = store.events(run, after)
            for row in rows:
                after = row["seq"]
                yield f"id: {after}\ndata: {json.dumps(row)}\n\n"
            if not rows:
                yield ": heartbeat\n\n"
            await asyncio.sleep(0.6)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


class ImportRequest(BaseModel):
    url: str


@app.post("/api/issues/import")
@traced("API: import issue")
async def import_issue(body: ImportRequest):
    try:
        return await import_github(body.url)
    except (ValueError, httpx.HTTPError) as e:
        raise HTTPException(422, str(e))


@app.get("/api/runs/{run}/bugs/{bug}/bundle")
@traced("API: download regression")
async def bundle(run: str, bug: str):
    snap = await snapshot(run)
    finding = next((b for b in snap["bugs"] if b["id"] == bug), None)
    if not finding:
        raise HTTPException(404, "Verified finding not found")
    data = io.BytesIO()
    replay = Path(finding["replay"])
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(replay, "regressions/replay.json")
        z.write(replay.parent / "test_regression.py", "regressions/test_regression.py")
        for p in ROOT.rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts:
                z.write(p, "swarmci/" + str(p.relative_to(ROOT)))
        for filename in ("pyproject.toml", "uv.lock", ".python-version"):
            z.write(filename, filename)
        z.write("infra/regression.workflow.yml", ".github/workflows/regression.yml")
        z.writestr(
            "README.md",
            "Run `uv sync --frozen`, `uv run playwright install chromium`, then `uv run swarmci replay regressions/replay.json --target-url YOUR_TEST_URL`. Exit 0 = pass, 1 = product failure, 2 = replay/infrastructure error. Supply isolated target setup and storage_state for authenticated apps. Evidence includes private browser data; review before publishing.",
        )
    return Response(
        data.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="swarmci-{run}-regression.zip"'},
    )
