"""Render observed Playwright results for GitHub and the explanation layer."""

import json
import os
from pathlib import Path

folder = Path(os.environ.get("PENPOT_EVIDENCE", "artifacts/penpot-verified"))
results = [json.loads(p.read_text()) for p in sorted(folder.glob("*/result.json"))]
rows = ["| Interaction path | Attempt | Result | Observed behavior |", "|---|---:|---|---|"]
for r in results:
    rows.append(
        f"| {r['variant']} | {r['repetition']} | {r['status'].upper()} | {r.get('observed', r.get('error', 'Runner did not complete'))[:180].replace(chr(10), ' ')} |"
    )
run_url = f"https://github.com/{os.environ.get('GITHUB_REPOSITORY', 'drPod/penpot')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
body = (
    """## ✳ SwarmCI — browser regression review

**Reset overrides can leave the wrong nested component in place.**

Related: penpot/penpot#11656 · proposed fix: penpot/penpot#11604.

This check runs the real **Penpot 2.17.2 release**, imports a fresh native document for every attempt, and uses a new browser session for each replay. It verifies behavior on that release; it does **not** claim to validate the proposed fix commit.

"""
    + "\n".join(rows)
    + f"""

### What matters

Both paths duplicate a parent, swap its nested Blue Button to Orange Button, and choose Reset overrides. The prepared grouped document has **two intermediate groups**. The control has a direct child. The assertion checks that Blue Button returns.

The functional failure is related to the reported issue. The reported error page has not been reproduced in the completed local checks. Novelty and root-cause equivalence are not established.

[Open this CI run and download the recordings, before/after screenshots, Playwright traces, and JUnit results]({run_url}).

<details><summary>Evidence and limits</summary>

The document was prepared through Penpot's actual UI and exported in native `.penpot` format. These are deterministic Playwright verification runs, not a claim of autonomous swarm discovery. Assertion failures are kept distinct from browser/setup errors. Every new PR update reruns the check and updates this review.

</details>
"""
)
folder.mkdir(parents=True, exist_ok=True)
(folder / "review.md").write_text(body)
if os.environ.get("GITHUB_STEP_SUMMARY"):
    Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a").write(body)
(folder / "review-data.json").write_text(
    json.dumps(
        {
            "schema_version": 1,
            "repository": "penpot/penpot",
            "issue": 11656,
            "proposed_fix_pr": 11604,
            "tested_version": "2.17.2",
            "execution": "Playwright verification",
            "novelty": "not established",
            "fix_validation": "not tested",
            "attempts": results,
        },
        indent=2,
    )
)
