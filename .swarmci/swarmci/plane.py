"""Plane session isolation, portable entity references, and product oracles."""

import json
import re
from pathlib import Path

import httpx

from swarmci.adapters.tracing import summary, traced
from swarmci.models import Action

TOKEN = re.compile(r"\{\{plane:([a-z0-9_]+)\}\}")


class PlaneFixture:
    def __init__(self, base_url):
        import os

        self.base_url = base_url.rstrip("/")
        self.broker = os.environ.get("PLANE_FIXTURE_URL", "http://127.0.0.1:8092")
        token = os.environ.get("PLANE_FIXTURE_TOKEN", "")
        if not token:
            token = Path(".secrets/plane-fixtures-token").read_text().strip()
        self.client = httpx.AsyncClient(
            base_url=self.broker, headers={"Authorization": "Bearer " + token}, timeout=70
        )
        self.id = None
        self.aliases = {}

    async def request(self, operation):
        response = await self.client.post("/sessions", json={"operation": operation, "id": self.id or ""})
        response.raise_for_status()
        return response.json()

    @traced("Plane · Create fresh user, workspace and project")
    async def create(self, context):
        data = await self.request("create")
        self.id = data["id"]
        self.aliases = data["aliases"]
        await context.add_cookies(
            [{**data["cookie"], "url": self.base_url, "httpOnly": True, "sameSite": "Lax"}]
        )
        summary(outputs={"isolation": "fresh user and workspace", "seed_work_items": 3})
        return self.expand(self.base_url + "/{{plane:workspace_slug}}/projects/{{plane:project}}/issues/")

    def normalize(self, value):
        if isinstance(value, str):
            for name, actual in sorted(self.aliases.items(), key=lambda item: len(item[1]), reverse=True):
                value = value.replace(actual, "{{plane:" + name + "}}")
            return value
        if isinstance(value, list):
            return [self.normalize(v) for v in value]
        if isinstance(value, dict):
            return {self.normalize(k): self.normalize(v) for k, v in value.items()}
        return value

    def expand(self, value):
        def replace(match):
            key = match.group(1)
            if key not in self.aliases:
                raise ValueError("Unknown Plane replay reference: " + key)
            return self.aliases[key]

        return TOKEN.sub(replace, value)

    def action(self, action, restore=False):
        transform = self.expand if restore else self.normalize
        return Action.model_validate(
            {k: transform(v) if isinstance(v, str) else v for k, v in action.model_dump().items()}
        )

    @traced("Plane · Read backend work-item state")
    async def snapshot(self):
        data = await self.request("snapshot")
        known = set(self.aliases.values())
        for category, rows in data.items():
            for index, row in enumerate(rows):
                for field in ("description_html", "comment_html"):
                    if isinstance(row.get(field), str):
                        row[field] = re.sub(r' data-id="[a-f0-9-]{36}"', '', row[field])
                actual = str(row.get("id", ""))
                if actual and actual not in known:
                    alias = f"{category.lower()}_{index + 1}"
                    if alias in self.aliases and self.aliases[alias] != actual:
                        # A delete/create can reuse a list position, but not identity.
                        alias += f"_generation_{len(self.aliases)}"
                    self.aliases[alias] = actual
                    known.add(actual)
        # Dates represent archive membership here, not incidental wall-clock identity.
        for issue in data["issues"]:
            issue["archived_at"] = bool(issue["archived_at"])
            issue["deleted_at"] = bool(issue["deleted_at"])
            # Tiptap assigns new paragraph UUIDs when opening identical seed content.
            # Keep markup, text, order and links; omit only those generated block IDs.
            issue["description_html"] = re.sub(r' data-id="[a-f0-9-]{36}"', "", issue["description_html"])
        return self.normalize(data)

    @traced("Plane · Check archived work items remain retrievable")
    async def checks(self, page):
        # Compare individual archive retrieval to the archive listing, with no UI filters.
        snapshot = await self.snapshot()
        archived = [i for i in snapshot["issues"] if i["archived_at"] and not i["deleted_at"]]
        if not archived:
            return []
        endpoint = self.expand(
            "/api/workspaces/{{plane:workspace_slug}}/projects/{{plane:project}}/archived-issues/"
        )
        response = await page.request.get(self.base_url + endpoint)
        if not response.ok:
            raise RuntimeError("Plane archive listing unavailable: " + str(response.status))
        body = await response.json()
        serialized = json.dumps(body)
        # Small seeded project (3 items); pagination cannot hide them on this first page.
        checks = []
        for issue in archived:
            actual = self.expand(issue["id"])
            present = actual in serialized
            checks.append(
                {
                    "name": "Archived work item remains listed: " + issue["name"],
                    "passed": present,
                    "rule": {
                        "name": "Archived work item remains listed: " + issue["name"],
                        "kind": "plane_archive",
                        "expected": issue["id"],
                    },
                }
            )
        return checks

    async def close(self):
        try:
            if self.id:
                await self.request("delete")
        finally:
            await self.client.aclose()
