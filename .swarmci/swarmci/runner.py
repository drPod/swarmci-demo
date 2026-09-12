import asyncio
import json
import time
from pathlib import Path

from swarmci.adapters.tracing import attributes, outcome, rename, span, summary, traced
from swarmci.agents import explore_bu, objectives
from swarmci.browser import BrowserSession
from swarmci.config import settings
from swarmci.models import Action, Assertion, RunConfig, Target
from swarmci.report import write_report
from swarmci.state import digest, observe
from swarmci.store import Store, uid


@traced("Replay regression", kind="workflow")
async def replay_manifest(manifest: Path, output: Path | None = None, target_url: str | None = None):
    data = json.loads(manifest.read_text())
    if data.get("version") != 1:
        raise ValueError("Unsupported replay version")
    target = Target.model_validate(data["target"])
    if target_url:
        old = target.url
        target.url = target_url
        for action in data["actions"]:
            if action["kind"] == "goto" and action["value"].startswith(old):
                action["value"] = target_url + action["value"][len(old) :]
    attributes(target_name=target.name, replay_manifest=str(manifest))
    target.assertions = [Assertion.model_validate(a) for a in data.get("assertions", [])]
    target.failure_selector = ""  # Already included as a serialized assertion.
    actions = [Action.model_validate(a) for a in data["actions"]]
    folder = output or settings.artifact_dir / "replays" / str(time.time_ns())
    checks = []
    status = "error"
    evidence = []
    try:
        async with BrowserSession(target, folder) as session:
            await session.replay(actions, annotate=True)
            await session.page.wait_for_timeout(500)
            checks = await session.checks(actions[-1] if actions else None)
            if not checks:
                raise ValueError("Replay has no product assertions; refusing a false green CI result")
            status = "passed" if all(c["passed"] for c in checks) else "failed"
            evidence = session.errors
            await session.page.screenshot(path=str(folder / "final.png"))
    except Exception as e:
        evidence.append({"kind": "replay-error", "message": str(e)})
    summary(
        outputs={
            "status": status,
            "assertions": [{"name": c["name"], "passed": c["passed"]} for c in checks],
            "artifacts": str(folder),
        }
    )
    rename(
        {
            "passed": "Replay passed · Assertions satisfied",
            "failed": "Replay reproduced failure",
            "error": "Replay interrupted · Infrastructure error",
        }[status]
    )
    outcome(status, error=status == "error", field="replay_status")
    attributes(checks_total=len(checks), checks_failed=sum(not c["passed"] for c in checks))
    write_report(folder, target, actions, checks, status, evidence)
    result = {"status": status, "checks": checks, "evidence": evidence, "artifacts": str(folder)}
    (folder / "result.json").write_text(json.dumps(result, indent=2))
    return result


class Coordinator:
    def __init__(self, store: Store):
        self.store = store
        self.tasks = {}
        self.result_dirs = []
        self.executor = None

    def start(self, config: RunConfig):
        run = self.store.create_run(config)
        task = asyncio.create_task(self.run(run, config))
        self.tasks[run] = task
        task.add_done_callback(lambda _: self.tasks.pop(run, None))
        return run

    @traced(
        "SwarmCI Exploration",
        kind="workflow",
        root=True,
        metadata=lambda self, run, config: {
            "run_id": run,
            "engine": config.engine,
            "workers": config.workers,
            "max_jobs": config.max_jobs,
            "budget_seconds": config.budget_seconds,
        },
    )
    async def run(self, run, config):
        rename(f"SwarmCI: {config.target.name}")
        attributes(target_name=config.target.name, trace_layout="workers-v2", execution=config.execution)
        summary(
            inputs={
                "target": config.target.name,
                "goal": config.target.objective,
                "engine": config.engine,
                "workers": config.workers,
                "limits": {
                    "jobs": config.max_jobs,
                    "seconds": config.budget_seconds,
                    "depth": config.max_depth,
                },
            }
        )
        self.store.status(run, "running")
        self.store.enqueue(
            run, "root", {"path": [], "checkpoint": None, "objective": config.target.objective, "owner": None}
        )
        if config.execution == "ray":
            from swarmci.fleet import RayExecutor

            try:
                self.executor = RayExecutor(self.store)
            except Exception as e:
                self.store.status(run, "error", "Ray fleet unavailable: " + str(e))
                return
        started = time.monotonic()
        completed = 0
        lock = asyncio.Lock()

        @traced("Browser worker", kind="agent", metadata=lambda index: {"worker_id": f"worker-{index + 1}"})
        async def worker(index):
            nonlocal completed
            worker_id = f"worker-{index + 1}"
            rename(f"Worker {index + 1:02d} · Browser explorer")
            worker_jobs = 0
            worker_errors = 0
            try:
                while time.monotonic() - started < config.budget_seconds:
                    async with lock:
                        if completed >= config.max_jobs:
                            return
                        job = self.store.claim(run, worker_id)
                        if job:
                            completed += 1
                            worker_jobs += 1
                    if not job:
                        pending = self.store.db.execute(
                            "SELECT count(*) FROM jobs WHERE run=? AND status='running'", (run,)
                        ).fetchone()[0]
                        if not pending:
                            return
                        await asyncio.sleep(0.15)
                        continue
                    try:
                        remaining = config.budget_seconds - (time.monotonic() - started)
                        with span(
                            "Explore checkpoint",
                            run_id=run,
                            job_id=job["id"],
                            worker_id=worker_id,
                            checkpoint_id=job["payload"].get("checkpoint") or "root",
                            inherited=job["payload"].get("owner") not in (None, worker_id),
                        ):
                            rename(
                                f"Branch {worker_jobs:02d} · {job['payload'].get('objective', 'Explore UI')[:90]}"
                            )
                            summary(
                                inputs={
                                    "objective": job["payload"].get("objective", ""),
                                    "checkpoint": job["payload"].get("checkpoint") or "Starting page",
                                    "arrival_depth": len(job["payload"]["path"]),
                                    "discovered_by": job["payload"].get("owner") or "Initial seed",
                                }
                            )
                            await asyncio.wait_for(
                                (
                                    self.executor.execute(run, job, config, worker_id)
                                    if self.executor
                                    else self.execute(run, job, config, worker_id)
                                ),
                                timeout=max(0.1, remaining),
                            )
                        self.store.finish(job["id"])
                    except asyncio.CancelledError:
                        self.store.finish(job["id"], "Cancelled")
                        raise
                    except Exception as e:
                        worker_errors += 1
                        self.store.finish(job["id"], str(e))
                        self.store.event(run, "job.error", {"id": job["id"], "message": str(e)})
            finally:
                attributes(branches_attempted=worker_jobs, branches_failed=worker_errors)
                summary(outputs={"branches_attempted": worker_jobs, "branches_failed": worker_errors})

        try:
            if not config.target.seed_ready:
                raise ValueError(
                    "Target needs an authenticated test account and prepared file. Configure target URL/setup first."
                )
            async with asyncio.TaskGroup() as group:
                for i in range(config.workers):
                    group.create_task(worker(i))
            snap = self.store.snapshot(run)
            errors = snap["jobs"].get("error", 0)
            status = "completed" if not errors else "completed_with_errors"
            if not snap["nodes"] and errors:
                status = "error"
            if time.monotonic() - started >= config.budget_seconds:
                status = "budget_exhausted"
            self.store.db.execute("UPDATE jobs SET status='skipped' WHERE run=? AND status='queued'", (run,))
            rename(
                f"Explore · {config.target.name} · {snap['metrics']['verified_bugs']} verified bug{'s' if snap['metrics']['verified_bugs'] != 1 else ''}"
            )
            summary(
                outputs={
                    "result": f"{snap['metrics']['states']} states explored; {snap['metrics']['verified_bugs']} bug{'s' if snap['metrics']['verified_bugs'] != 1 else ''} confirmed by clean-session replay",
                    "status": status,
                    "coverage": snap["metrics"],
                    "jobs": snap["jobs"],
                    "findings": [
                        {"title": bug["title"], "steps": bug["steps"], "replay": bug["replay"]}
                        for bug in snap["bugs"]
                    ],
                }
            )
            attributes(**snap["metrics"])
            outcome(status, error=status in ("error", "completed_with_errors"), field="run_status")
            self.store.status(run, status)
        except asyncio.CancelledError:
            self.store.db.execute(
                "UPDATE jobs SET status='cancelled' WHERE run=? AND status IN ('queued','running')", (run,)
            )
            outcome("cancelled", field="run_status")
            self.store.status(run, "cancelled")
        except Exception as e:
            outcome("error", error=True, field="run_status")
            self.store.status(run, "error", str(e))

    async def execute(self, run, job, config, worker):
        target = config.target.model_copy(deep=True)
        payload = job["payload"]
        path = [Action.model_validate(a) for a in payload["path"]]
        folder = settings.artifact_dir / run / job["id"]
        async with BrowserSession(target, folder, config.cloud_browser) as session:
            with span(
                "01 · Restore starting state", checkpoint_id=payload["checkpoint"] or "root", depth=len(path)
            ):
                await session.replay(target.setup + path)
                state = await observe(session.page, target, path)
                # Never continue from a checkpoint that failed to restore.
                if payload["checkpoint"] and state["id"] != payload["checkpoint"]:
                    before = payload.get("fingerprint_parts", {})
                    self.store.event(
                        run,
                        "checkpoint.drift",
                        {
                            "worker": worker,
                            "changed": [
                                k for k, v in state.get("fingerprint_parts", {}).items() if before.get(k) != v
                            ],
                        },
                    )
                    raise ValueError("Checkpoint drift: restored state differs from recorded state")
                self.store.event(
                    run,
                    "checkpoint.restored",
                    {
                        "worker": worker,
                        "depth": len(path),
                        "inherited": payload.get("owner") not in (None, worker),
                    },
                )
                self.store.node(run, state)
                summary(
                    outputs={
                        "restored": True,
                        "state": state["label"],
                        "depth": len(path),
                        "inherited_from": payload.get("owner"),
                    }
                )
            previous = state

            @traced("Record suspected UX issue")
            async def candidate(finding):
                evidence_folder = folder / ("candidate-" + uid())
                evidence_folder.mkdir(parents=True, exist_ok=True)
                await session.page.screenshot(path=str(evidence_folder / "screenshot.png"))
                full_path = target.setup + path
                write_report(evidence_folder, target, full_path, [], "candidate", [finding])
                finding = {
                    **finding,
                    "worker": worker,
                    "status": "needs_review",
                    "steps": len(full_path),
                    "screenshot": str(evidence_folder / "screenshot.png"),
                    "replay": str(evidence_folder / "replay.json"),
                    "state": previous["id"],
                }
                (evidence_folder / "finding.json").write_text(json.dumps(finding, indent=2))
                self.store.event(run, "ux.candidate", finding)
                summary(outputs={"status": "needs_review", "title": finding["title"]})

            @traced("Record transition")
            async def record(action):
                nonlocal previous
                if session.fixture:
                    await session.settle()
                    action = session.fixture.action(action)
                path.append(action)
                state = await observe(session.page, target, path)
                screenshot = folder / f"step-{len(path)}.png"
                await session.page.screenshot(path=str(screenshot))
                state["screenshot"] = str(screenshot)
                new_state = self.store.node(run, state)
                rename(f"{'New state' if new_state else 'Known state'} · {state['label'][:90]}")
                summary(
                    outputs={
                        "source": previous["id"],
                        "destination": state["id"],
                        "new_state": new_state,
                        "depth": len(path),
                        "action": action.label or action.kind,
                        "screen": state["label"],
                    }
                )
                self.store.edge(
                    run,
                    previous["id"],
                    state["id"],
                    {
                        "action": action.model_dump(),
                        "path": [a.model_dump() for a in path],
                        "worker": worker,
                        "job": job["id"],
                        "inherited": payload.get("owner") not in (None, worker),
                        "screenshot": str(screenshot),
                    },
                )
                attributes(
                    state_id=state["id"],
                    screen_key=state["screen_key"],
                    depth=len(path),
                    action_kind=action.kind,
                )
                previous = state
                checks = await session.checks(action)
                if any(not c["passed"] for c in checks):
                    self.store.event(run, "failure.candidate", {"state": state["id"], "worker": worker})
                    await self.verify(run, target, path, checks, state, session.errors)
                    return False
                await self.branch(run, config, state, path, worker)
                return len(path) < config.max_depth

            if config.engine == "fixture":
                action = payload.get("action")
                if action:
                    act = Action.model_validate(action)
                    with span(f"02 · Explore · {act.label or act.kind}", kind="tool"):
                        await session.act(act)
                        await record(act)
                else:
                    await self.branch(run, config, state, path, worker)
            else:
                await explore_bu(
                    session,
                    target,
                    payload["objective"],
                    min(config.branch_steps, config.max_depth - len(path)),
                    record,
                    folder,
                    model_role="gemma" if config.engine == "gemma" else "bu",
                    on_candidate=candidate,
                )

        summary(
            outputs={
                "starting_depth": len(payload["path"]),
                "ending_depth": len(path),
                "actions_explored": len(path) - len(payload["path"]),
                "final_state": previous["label"],
                "browser_errors_observed": len(session.errors),
                "artifacts": str(folder),
            }
        )

    @traced("Plan next branches")
    async def branch(self, run, config, state, path, worker):
        if len(path) >= config.max_depth:
            summary(outputs={"scheduled": 0, "reason": "Depth limit reached"})
            return
        count = self.store.job_count(run)
        if count >= config.max_jobs:
            summary(outputs={"scheduled": 0, "reason": "Job limit reached"})
            return
        if config.engine == "fixture":
            variants = [
                {
                    "action": Action(kind="click", selector=c["selector"], label=c["label"]).model_dump(),
                    "objective": c["label"],
                }
                for c in state["controls"]
                if c["tag"] == "button" and not c["disabled"]
            ]
        else:
            try:
                options = await objectives(state, config.target, gemma=config.use_gemma)
            except Exception as e:
                self.store.event(run, "model.error", {"model": "gemma", "message": str(e)})
                options = await objectives(state, config.target)
            variants = [{"objective": o} for o in options]
        planned = []
        for variant in variants[: max(0, config.max_jobs - count)]:
            # Keep arrival history in the exploration signature even if states converge.
            signature = digest(
                {"state": state["id"], "history": [a.model_dump() for a in path], "variant": variant}
            )
            added = self.store.enqueue(
                run,
                signature,
                {
                    "path": [a.model_dump() for a in path],
                    "checkpoint": state["id"],
                    "fingerprint_parts": state.get("fingerprint_parts", {}),
                    "owner": worker,
                    **variant,
                },
            )
            if added:
                planned.append(variant["objective"])
        rename(f"Plan next branches · {len(planned)} queued")
        summary(outputs={"scheduled": len(planned), "objectives": planned})

    @traced("03 · Verify suspected bug")
    async def verify(self, run, target, path, checks, state, errors):
        failed = sorted(c["name"] for c in checks if not c["passed"])
        signature = digest({"issue": target.issue_url, "failures": failed, "screen": state["screen_key"]})
        if self.store.has_bug(run, signature):
            rename("Already verified · Skip duplicate report")
            summary(outputs={"verification": "already_recorded", "assertions": failed})
            return
        folder = settings.artifact_dir / run / "bugs" / (signature + "-" + uid())
        # Full clean-session replay, including setup, before calling anything verified.
        attributes(state_id=state["id"], failed_assertions=failed)
        full_path = target.setup + path
        write_report(folder, target, full_path, checks, "candidate", errors)
        result = await replay_manifest(folder / "replay.json", folder / "verification")
        repeated = sorted(c["name"] for c in result["checks"] if not c["passed"])
        if result["status"] == "failed" and failed == repeated:
            write_report(folder, target, full_path, checks, "verified", errors)
            report = {
                "title": ("Application error after " + (path[-1].label or path[-1].kind))
                if failed == ["No application error screen"]
                else "Invariant failed: " + failed[0],
                "status": "verified",
                "state": state["id"],
                "steps": len(full_path),
                "path": [a.model_dump() for a in full_path],
                "checks": checks,
                "evidence": errors,
                "report": str(folder / "verification" / "report.html"),
                "replay": str(folder / "replay.json"),
                "screenshot": str(folder / "verification" / "final.png"),
                "videos": [str(p) for p in (folder / "verification").glob("*.webm")],
            }
            rename(f"Confirmed bug · {report['title'][:100]}")
            attributes(verified=True, state_id=state["id"], reproduction_steps=len(full_path))
            bug_id = self.store.bug(run, signature, report)
            summary(
                outputs={
                    "verified": True,
                    "bug_id": bug_id,
                    "failed_assertions": failed,
                    "steps": len(full_path),
                    "report": report["report"],
                    "replay": report["replay"],
                }
            )
        else:
            rename("Not reproduced · Candidate retained")
            attributes(verified=False, state_id=state["id"])
            summary(outputs={"verified": False, "replay_status": result["status"]})
            self.store.event(run, "failure.unverified", {"state": state["id"], "result": result})
