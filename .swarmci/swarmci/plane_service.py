"""Private fixture broker for the local Plane deployment; no changes to Plane code."""

import asyncio
import json
import secrets
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from swarmci.adapters.tracing import traced

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = [
    "docker",
    "compose",
    "-p",
    "swarm-plane",
    "--env-file",
    str(ROOT / "data/plane.env"),
    "-f",
    str(ROOT / "infra/plane/compose.upstream.yml"),
    "-f",
    str(ROOT / "infra/plane/compose.local.yml"),
]
app = FastAPI(title="Plane disposable test sessions")
slots = asyncio.Semaphore(4)


class Operation(BaseModel):
    operation: Literal["create", "snapshot", "delete"]
    id: str = Field(default="", pattern=r"^(?:[a-f0-9]{16})?$")


@app.post("/sessions")
@traced("Plane · Provision or inspect isolated workspace")
async def sessions(body: Operation, authorization: str = Header(default="")):
    token_path = ROOT / ".secrets/plane-fixtures-token"
    if not token_path.exists() or not secrets.compare_digest(
        authorization, "Bearer " + token_path.read_text().strip()
    ):
        raise HTTPException(401, "Invalid fixture broker token")
    if body.operation != "create" and not body.id:
        raise HTTPException(422, "Session ID required")
    script = (ROOT / "infra/plane/seed.py").read_text() + "\nprint('SWARM_RESULT='+json.dumps(operate(json.loads(sys.stdin.read())),default=str))"
    # Program text is fixed locally, never supplied by an agent or HTTP request.
    async with slots:
        proc = await asyncio.create_subprocess_exec(
            *COMPOSE,
            "exec",
            "-T",
            "api",
            "python",
            "manage.py",
            "shell",
            "-c",
            script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(body.model_dump_json().encode()), 60)
        except BaseException:
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode:
            # Server-local diagnostics; do not return session cookie output to logs.
            (ROOT / "data/plane-fixture-error.log").write_bytes(err)
            raise HTTPException(503, "Plane fixture operation failed; see data/plane-fixture-error.log")
        for line in out.decode().splitlines():
            if line.startswith("SWARM_RESULT="):
                return json.loads(line.removeprefix("SWARM_RESULT="))
        raise HTTPException(503, "Plane returned no fixture result")
