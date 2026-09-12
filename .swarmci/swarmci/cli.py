import asyncio
import json
from pathlib import Path

import typer

from swarmci.adapters.tracing import initialize, shutdown
from swarmci.runner import replay_manifest

app = typer.Typer(no_args_is_help=True)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8080):
    """Start the local dashboard and coordinator."""
    import uvicorn

    uvicorn.run("swarmci.api:app", host=host, port=port)


@app.command()
def replay(manifest: Path, output: Path = Path("artifacts/ci"), target_url: str | None = None):
    """Execute a recorded test without model inference. Exit 0/pass, 1/bug, 2/runner error."""
    initialize()
    try:
        result = asyncio.run(replay_manifest(manifest, output, target_url))
        typer.echo(json.dumps(result, indent=2))
        raise typer.Exit({"passed": 0, "failed": 1, "error": 2}[result["status"]])
    except (ValueError, OSError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2)
    finally:
        shutdown()


@app.command()
def explore(
    target: Path = Path("targets/fixture.json"),
    engine: str = "fixture",
    workers: int = 3,
    jobs: int = 32,
    seconds: int = 180,
):
    """Explore a running target and save shared graph + verified findings."""
    from swarmci.config import settings
    from swarmci.models import RunConfig, Target
    from swarmci.runner import Coordinator
    from swarmci.store import Store

    config = RunConfig(
        target=Target.model_validate_json(target.read_text()),
        engine=engine,
        workers=workers,
        max_jobs=jobs,
        budget_seconds=seconds,
    )
    store = Store(settings.data_dir / "swarmci.db")

    async def run():
        c = Coordinator(store)
        id = store.create_run(config)
        typer.echo("Run " + id)
        await c.run(id, config)
        snap = store.snapshot(id)
        typer.echo(
            json.dumps(
                {"id": id, "status": snap["status"], "metrics": snap["metrics"], "bugs": snap["bugs"]},
                indent=2,
            )
        )
        return snap

    initialize()
    try:
        snap = asyncio.run(run())
    finally:
        shutdown()
    if snap["status"] != "completed":
        raise typer.Exit(2)


if __name__ == "__main__":
    app()
