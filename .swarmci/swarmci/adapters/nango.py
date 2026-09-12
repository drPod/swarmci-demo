"""Nango owns account authorization, credential refresh, and provider proxying."""

import asyncio

from swarmci.config import settings


class NangoError(ValueError):
    pass


class Nango:
    def __init__(self, transport=None):
        self.transport = transport

    async def sdk(self, operation, **args):
        if not settings.nango_secret_key:
            raise NangoError("Set NANGO_SECRET_KEY to connect accounts.")
        import json
        import os
        from pathlib import Path

        bridge = Path(__file__).resolve().parents[2] / "integrations" / "nango.mjs"
        process = await asyncio.create_subprocess_exec(
            "node",
            str(bridge),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "NANGO_SECRET_KEY": settings.nango_secret_key},
        )
        output, error = await process.communicate(json.dumps({"operation": operation, "args": args}).encode())
        if process.returncode:
            raise NangoError(error.decode()[-500:])
        return json.loads(output)

    async def request(self, method, path, *, connection=None, json=None, params=None):
        if connection:
            return await self.sdk(
                "proxy",
                method=method,
                endpoint=path,
                providerConfigKey=connection["provider_config_key"],
                connectionId=connection["connection_id"],
                data=json,
                params=params or {},
                retries=3 if method == "GET" else 0,
            )
        operations = {
            "/connections": "connections",
            "/integrations": "integrations",
            "/providers": "providers",
        }
        if method == "GET" and path in operations:
            return await self.sdk(operations[path])
        if method == "POST" and path == "/connect/sessions":
            return await self.sdk("connect", **json)
        raise NangoError("Use a supported Nango SDK operation.")

    async def action(self, connection, name, data):
        return await self.sdk(
            "action",
            integration=connection["provider_config_key"],
            connection=connection["connection_id"],
            name=name,
            input=data,
        )

    async def catalog(self):
        integrations, connections, providers = await asyncio.gather(
            self.request("GET", "/integrations"),
            self.request("GET", "/connections"),
            self.request("GET", "/providers"),
        )
        return {
            "integrations": [
                {k: x.get(k) for k in ("unique_key", "provider", "display_name")}
                for x in integrations.get("data", [])
            ],
            "connections": [
                {k: x.get(k) for k in ("connection_id", "provider_config_key", "provider", "errors")}
                for x in connections.get("connections", [])
            ],
            "providers": [
                {k: x.get(k) for k in ("name", "display_name", "categories", "auth_mode")}
                for x in providers.get("data", [])
            ],
        }

    async def connection(self, integration="", connection_id="", provider=""):
        rows = (await self.request("GET", "/connections")).get("connections", [])
        matches = [
            x
            for x in rows
            if (not integration or x["provider_config_key"] == integration)
            and (not connection_id or x["connection_id"] == connection_id)
            and (not provider or x.get("provider", "").startswith(provider))
        ]
        if len(matches) != 1:
            raise NangoError(
                "Choose one connected account in Integrations."
                if matches
                else "Connect this account through Nango first."
            )
        if matches[0].get("errors"):
            raise NangoError("This Nango connection needs reauthorization.")
        return matches[0]

    async def connect(self, integration):
        result = await self.request(
            "POST",
            "/connect/sessions",
            json={"tags": {"end_user_id": "swarmci-demo"}, "allowed_integrations": [integration]},
        )
        return {"connect_link": result.get("data", result)["connect_link"]}

    async def destinations(self, connection):
        provider = connection.get("provider", "")
        if provider.startswith("github"):
            repos = await self.request(
                "GET", "/user/repos", connection=connection, params={"per_page": 100, "sort": "updated"}
            )
            return [
                {"id": x["full_name"], "name": x["full_name"]}
                for x in repos
                if x.get("permissions", {}).get("push")
            ]
        if provider.startswith("linear"):
            return await self.action(connection, "list-teams", {})
        return []

    async def create_ticket(self, connection, destination, title, body, issue_type=""):
        provider = connection.get("provider", "")
        if provider.startswith("linear"):
            return await self.action(
                connection, "create-issue", {"teamId": destination, "title": title, "description": body}
            )
        raise NangoError(
            "Enable this provider's prebuilt ticket action in Nango; use the template input form in Integrations."
        )
