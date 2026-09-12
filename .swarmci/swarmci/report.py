import html
import json
from pathlib import Path
from xml.etree.ElementTree import Element, ElementTree, SubElement

from swarmci.adapters.tracing import traced


@traced("Write regression evidence")
def write_report(folder: Path, target, path, checks, status, evidence=None):
    folder.mkdir(parents=True, exist_ok=True)
    replay = {
        "version": 1,
        "target": target.model_dump(),
        "actions": [a.model_dump() for a in path],
        "assertions": [c["rule"] for c in checks],
        "recorded_status": status,
    }
    (folder / "replay.json").write_text(json.dumps(replay, indent=2))
    # A checked-in manifest + this small test is the executable CI regression.
    (folder / "test_regression.py").write_text("""from pathlib import Path
import asyncio
from swarmci.runner import replay_manifest


def test_regression():
    result = asyncio.run(replay_manifest(Path(__file__).with_name("replay.json")))
    assert result["status"] == "passed", result
""")
    failed = [c for c in checks if not c["passed"]]
    suite = Element(
        "testsuite",
        name="SwarmCI",
        tests=str(max(1, len(checks))),
        failures=str(len(failed)),
        errors="1" if status == "error" else "0",
    )
    for check in checks or [{"name": "Replay completed", "passed": status == "passed"}]:
        case = SubElement(suite, "testcase", name=check["name"], classname=target.name)
        if status == "error":
            SubElement(case, "error", message="Replay could not complete").text = json.dumps(evidence)
        elif not check["passed"]:
            SubElement(case, "failure", message=check["name"]).text = json.dumps(check)
    ElementTree(suite).write(folder / "junit.xml", encoding="unicode", xml_declaration=True)
    steps = "\n".join(
        f"{i + 1}. {a.label or a.kind}" + (f" (`{a.selector}`)" if a.selector else "")
        for i, a in enumerate(path)
    )
    md = (
        f"# {target.name}\n\nStatus: **{status}**\n\nSource: {target.issue_url or target.url}\n\n## Reproduction\n\n{steps}\n\n## Assertions\n\n"
        + "\n".join(f"- {'PASS' if c['passed'] else 'FAIL'}: {c['name']}" for c in checks)
    )
    (folder / "report.md").write_text(
        md
        + "\n\nRun `uv run swarmci replay replay.json` to check this regression. Video and trace are attached.\n"
    )
    videos = list(folder.glob("*.webm"))
    video = f'<video controls src="{html.escape(videos[0].name)}"></video>' if videos else ""
    (folder / "report.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>SwarmCI evidence</title><style>body{background:#11151b;color:#edf1f5;font:16px system-ui;max-width:1100px;margin:50px auto}video{width:100%;border-radius:12px}pre{white-space:pre-wrap;padding:24px;background:#1b222c;border-radius:12px}a{color:#94ebc1}</style><h1>SwarmCI · '
        + html.escape(target.name)
        + "</h1>"
        + video
        + '<p><a href="replay.json">Replay manifest</a> · <a href="trace.zip">Browser trace</a> · <a href="junit.xml">JUnit</a></p><pre>'
        + html.escape(md)
        + "</pre>"
    )
