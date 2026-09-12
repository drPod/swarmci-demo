"""Publish verified evidence using GitHub's Git data API through Nango."""

import asyncio
import hashlib
import json
import re
from pathlib import Path

from swarmci.adapters.nango import Nango, NangoError
from swarmci.config import settings


def evidence_path(value):
    path = Path(value).resolve()
    if not path.is_relative_to(settings.artifact_dir.resolve()) or not path.is_file():
        raise ValueError("Evidence must be a recorded artifact in this project.")
    return path


async def prepare_evidence(finding):
    folder = evidence_path(finding["replay"]).parent
    videos = finding.get("videos") or [str(p) for p in (folder / "verification").glob("*.webm")]
    if not videos:
        raise ValueError("This finding has no verification recording.")
    video = evidence_path(videos[0])
    mp4, gif = folder / "replay.mp4", folder / "preview.gif"
    for dest, args in [
        (mp4, ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an"]),
        (gif, ["-t", "24", "-vf", "fps=6,scale=640:-1:flags=lanczos", "-loop", "0"]),
    ]:
        if not dest.exists():
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(video),
                *args,
                str(dest),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, error = await process.communicate()
            if process.returncode:
                raise ValueError("Video conversion failed: " + error.decode()[-300:])
    return folder, mp4, gif


def report_body(snapshot, finding, repository, sha):
    prefix = f"evidence/{snapshot['id']}/{finding['id']}"
    raw = f"https://raw.githubusercontent.com/{repository}/{sha}/{prefix}"
    blob = f"https://github.com/{repository}/blob/{sha}/{prefix}"
    manifest = json.loads(evidence_path(finding["replay"]).read_text())
    steps = "\n".join(f"{i}. {a.get('label') or a['kind']}" for i, a in enumerate(manifest["actions"], 1))
    fixture = snapshot["config"]["target"]["isolation"] == "fixture"
    return (
        f"## Verified browser regression\n\n{finding['title']}\n\n"
        + ("**Controlled Component Lab fixture. This is not a Penpot reproduction.**\n\n" if fixture else "")
        + f"Reproduced in a fresh browser after {finding['steps']} actions. Run `{snapshot['id']}`.\n\n"
        + f"[![Recorded replay preview]({raw}/preview.gif)]({blob}/replay.mp4)\n\n"
        + f"[▶ Watch/download the full MP4 recording]({raw}/replay.mp4) · [Replay manifest]({blob}/replay.json) · [JUnit evidence]({blob}/junit.xml)\n\n"
        + f"### Reproduction\n\n{steps}\n\n### CI\n\n"
        + "The regression test asserts healthy product behavior and fails while the bug is present. "
        + "The fixture demo also checks its fixed variant independently. CI uploads fresh video, trace, and JUnit artifacts.\n\n"
        + "Published through **Nango** using the connected GitHub account. Nango handles authorization and provider requests; no provider token is stored in this repository.\n"
        + f"\n<!-- swarmci:{snapshot['id']}:{finding['id']} -->"
    )


class Publisher:
    def __init__(self, nango=None):
        self.nango = nango or Nango()

    async def github(self, snapshot, finding, connection, repository):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
            raise ValueError("Use owner/repository.")
        folder, mp4, gif = await prepare_evidence(finding)
        marker = hashlib.sha256(repository.encode()).hexdigest()[:12]
        receipt_path = folder / f"github-{marker}.json"
        receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}

        def save():
            receipt_path.write_text(json.dumps(receipt, indent=2))

        async def request(method, path, **kwargs):
            return await self.nango.request(
                method, f"/repos/{repository}" + path, connection=connection, **kwargs
            )

        branch = f"swarmci/{snapshot['id']}-{finding['id']}"
        meta = await request("GET", "")
        base = meta["default_branch"]
        prefix = f"evidence/{snapshot['id']}/{finding['id']}"
        if not receipt.get("sha"):
            files = {
                f"{prefix}/replay.mp4": mp4.read_bytes(),
                f"{prefix}/preview.gif": gif.read_bytes(),
                f"{prefix}/replay.json": (folder / "replay.json").read_bytes(),
                f"regressions/{finding['id']}.json": (folder / "replay.json").read_bytes(),
            }
            junit = folder / "verification" / "junit.xml"
            if junit.exists():
                files[f"{prefix}/junit.xml"] = junit.read_bytes()
            # Isolate the runner beneath its own directory; don't overwrite a destination project's files.
            for path in Path("swarmci").rglob("*"):
                if (
                    path.is_file()
                    and path.suffix in {".py", ".html", ".css", ".js"}
                    and "__pycache__" not in path.parts
                ):
                    files[f".swarmci/{path}"] = path.read_bytes()
            for name in ("pyproject.toml", "uv.lock", ".python-version"):
                files[f".swarmci/{name}"] = Path(name).read_bytes()
            fixture = snapshot["config"]["target"]["isolation"] == "fixture"
            workflow = Path(
                "infra/nango-demo.workflow.yml" if fixture else "infra/regression.workflow.yml"
            ).read_text()
            if not fixture:
                workflow = workflow.replace(
                    "jobs:\n", "defaults:\n  run:\n    working-directory: .swarmci\njobs:\n"
                )
                workflow = workflow.replace("regressions/*.json", "../regressions/*.json").replace(
                    "path: artifacts/ci/", "path: .swarmci/artifacts/ci/"
                )
            files[".github/workflows/swarmci-regression.yml"] = workflow.encode()
            checkout = settings.data_dir / "publishing" / marker
            checkout.parent.mkdir(parents=True, exist_ok=True)

            async def command(*args, cwd=None):
                proc = await asyncio.create_subprocess_exec(
                    *args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                out, err = await proc.communicate()
                if proc.returncode:
                    raise ValueError("Git publish failed: " + err.decode()[-500:])
                return out.decode().strip()

            if not (checkout / ".git").exists():
                await command("gh", "repo", "clone", repository, str(checkout))
            await command("git", "fetch", "origin", cwd=checkout)
            await command("git", "checkout", "-B", branch, "origin/" + base, cwd=checkout)
            for name, content in files.items():
                dest = checkout / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(content)
            await command(
                "git",
                "add",
                "--",
                ".swarmci",
                ".github/workflows/swarmci-regression.yml",
                "regressions",
                "evidence",
                cwd=checkout,
            )
            await command(
                "git",
                "-c",
                "user.name=SwarmCI",
                "-c",
                "user.email=swarmci@users.noreply.github.com",
                "commit",
                "-m",
                "Add verified browser regression and CI evidence",
                cwd=checkout,
            )
            await command("git", "push", "origin", branch, cwd=checkout)
            receipt["sha"] = await command("git", "rev-parse", "HEAD", cwd=checkout)
            receipt["branch"] = branch
            save()
        body = report_body(snapshot, finding, repository, receipt["sha"])
        marker_text = f"<!-- swarmci:{snapshot['id']}:{finding['id']} -->"
        if not receipt.get("issue"):
            issues = await request("GET", "/issues", params={"state": "all", "per_page": 100})
            issue = next(
                (x for x in issues if "pull_request" not in x and marker_text in (x.get("body") or "")), None
            )
            if not issue:
                issue = await self.nango.action(
                    connection,
                    "create-issue",
                    {
                        "owner": repository.split("/")[0],
                        "repo": repository.split("/")[1],
                        "title": "[SwarmCI] " + finding["title"],
                        "body": body,
                    },
                )
            receipt["issue"] = {"url": issue["html_url"], "number": issue["number"]}
            save()
        if not receipt.get("pr"):
            prs = await request(
                "GET", "/pulls", params={"state": "all", "head": repository.split("/")[0] + ":" + branch}
            )
            pr = next(iter(prs), None)
            if not pr:
                pr = await self.nango.action(
                    connection,
                    "create-pull-request",
                    {
                        "owner": repository.split("/")[0],
                        "repo": repository.split("/")[1],
                        "title": "Add regression coverage: " + finding["title"],
                        "body": body
                        + f"\nRelated issue: #{receipt['issue']['number']}. This PR adds coverage; it does not fix the product bug.\n",
                        "head": branch,
                        "base": base,
                        "draft": True,
                    },
                )
            receipt["pr"] = {"url": pr.get("html_url", pr.get("url")), "number": pr["number"]}
            save()
        receipt["body"] = body
        receipt["repository"] = repository
        save()
        return receipt

    async def ticket(self, finding, connection, destination, github_receipt, issue_type=""):
        folder = evidence_path(finding["replay"]).parent
        key = hashlib.sha256((connection["connection_id"] + destination).encode()).hexdigest()[:16]
        path = folder / f"ticket-{key}.json"
        if path.exists():
            return json.loads(path.read_text())
        # An in-flight receipt prevents accidental duplicate creation after a timeout.
        pending = folder / f"ticket-{key}.pending"
        if pending.exists():
            raise NangoError("Previous delivery is unresolved. Check Nango logs before retrying this ticket.")
        pending.write_text("Publishing via Nango")
        result = await self.nango.create_ticket(
            connection,
            destination,
            finding["title"],
            github_receipt["body"] + "\nGitHub PR: " + github_receipt["pr"]["url"],
            issue_type,
        )
        path.write_text(json.dumps(result))
        pending.unlink()
        return result
