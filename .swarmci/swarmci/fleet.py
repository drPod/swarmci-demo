"""Ray integration glue. Ray supplies placement, task transport, actors and object storage."""

import asyncio
import io
import zipfile
from pathlib import Path

import ray
from opentelemetry import context, propagate
from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy

from swarmci.config import settings
from swarmci.models import RunConfig
from swarmci.store import Store


@ray.remote(num_cpus=0)
class GraphStore:
    def __init__(self, path):
        self.store = Store(Path(path))

    def call(self, method, args):
        if method not in {"node", "edge", "event", "enqueue", "job_count", "has_bug", "bug"}:
            raise ValueError("Unsupported graph operation")
        return getattr(self.store, method)(*args)


class SharedStore:
    def __init__(self, actor):
        self.actor = actor

    def __getattr__(self, name):
        def call(*args):
            return ray.get(self.actor.call.remote(name, args))

        return call


@ray.remote(num_cpus=0.5, resources={"browser": 1}, max_retries=0)
def browser_job(graph, run, job, config, worker, trace_headers):
    from swarmci.runner import Coordinator

    coordinator = Coordinator(SharedStore(graph))
    cfg = RunConfig.model_validate(config)
    token = context.attach(propagate.extract(trace_headers))
    try:
        asyncio.run(coordinator.execute(run, job, cfg, worker))
    finally:
        context.detach(token)
    buffer = io.BytesIO()
    root = settings.artifact_dir.resolve()
    folders = [settings.artifact_dir / run / job["id"], *coordinator.result_dirs]
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for folder in folders:
            for path in folder.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    z.write(path, path.resolve().relative_to(root))
    return {"archive": buffer.getvalue(), "host": ray.util.get_node_ip_address()}


class RayExecutor:
    def __init__(self, store):
        if not ray.is_initialized():
            ray.init(address=settings.ray_address, namespace="swarmci", log_to_driver=False)
        node = ray.get_runtime_context().get_node_id()
        db = store.db.execute("PRAGMA database_list").fetchone()["file"]
        self.graph = GraphStore.options(
            scheduling_strategy=NodeAffinitySchedulingStrategy(node, soft=False)
        ).remote(db)
        self.store = store

    async def execute(self, run, job, config, worker):
        trace_headers = {}
        propagate.inject(trace_headers)
        ref = browser_job.remote(self.graph, run, job, config.model_dump(), worker, trace_headers)
        try:
            result = await ref
        except asyncio.CancelledError:
            ray.cancel(ref, force=True)
            raise
        root = settings.artifact_dir.resolve()
        with zipfile.ZipFile(io.BytesIO(result["archive"])) as z:
            for member in z.infolist():
                dest = (root / member.filename).resolve()
                if not dest.is_relative_to(root):
                    raise ValueError("Invalid artifact path")
                if member.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(z.read(member))
        self.store.event(
            run, "worker.completed", {"worker": worker, "host": result["host"], "job": job["id"]}
        )
