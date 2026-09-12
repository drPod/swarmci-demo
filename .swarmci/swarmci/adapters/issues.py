import re

import httpx

from swarmci.adapters.tracing import traced
from swarmci.config import settings


@traced("Import GitHub issue")
async def import_github(url: str):
    match = re.fullmatch(r"https://github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)/?", url)
    if not match:
        raise ValueError("Enter a GitHub issue URL: https://github.com/owner/repo/issues/123")
    owner, repo, number = match.groups()
    path = f"/repos/{owner}/{repo}/issues/{number}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "SwarmCI"}
    use_nango = bool(settings.nango_secret_key and settings.nango_connection_id)
    if use_nango:
        from swarmci.adapters.nango import Nango
        nango = Nango()
        connection = await nango.connection(settings.nango_provider_config_key, settings.nango_connection_id)
        issue = await nango.action(connection, "get-issue", {"owner": owner, "repo": repo, "issue_number": int(number)})
    else:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get("https://api.github.com" + path, headers=headers)
            response.raise_for_status()
            issue = response.json()
    return {
        "url": url,
        "title": issue["title"],
        "body": issue.get("body") or "",
        "state": issue["state"],
        "provider": "nango" if use_nango else "github-public",
        "number": int(number),
        "repository": f"{owner}/{repo}",
    }


@traced("Export GitHub report")
async def export_github(repository: str, title: str, body: str):
    """Caller must be an explicit user publish action; exploration never invokes this."""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise ValueError("Invalid repository")
    if not settings.nango_secret_key or not settings.nango_connection_id:
        raise ValueError("Nango is not configured")
    from swarmci.adapters.nango import Nango
    nango = Nango()
    connection = await nango.connection(settings.nango_provider_config_key, settings.nango_connection_id)
    owner, repo = repository.split("/")
    return await nango.action(connection, "create-issue", {"owner": owner, "repo": repo, "title": title, "body": body})
