"""Reserved for the next product surface: inbox lifecycle and read-only message retrieval."""

from urllib.parse import quote

import httpx

from swarmci.adapters.tracing import traced
from swarmci.config import settings


class AgentMail:
    @traced("AgentMail request")
    async def request(self, method, path, **kwargs):
        if not settings.agentmail_api_key:
            raise ValueError("AGENTMAIL_API_KEY is not configured")
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.request(
                method,
                "https://api.agentmail.to/v0" + path,
                headers={"Authorization": "Bearer " + settings.agentmail_api_key},
                **kwargs,
            )
            response.raise_for_status()
            return response.json()

    async def create_inbox(self, client_id):
        return await self.request("POST", "/inboxes", json={"client_id": client_id})

    async def messages(self, inbox_id):
        return await self.request("GET", "/inboxes/" + quote(inbox_id, safe="") + "/messages")

    async def message(self, inbox_id, message_id):
        return await self.request(
            "GET", "/inboxes/" + quote(inbox_id, safe="") + "/messages/" + quote(message_id, safe="")
        )
