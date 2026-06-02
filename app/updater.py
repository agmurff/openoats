"""GitHub Releases update check.

Non-blocking: queries the GitHub Releases API for the latest tag, compares it
to the bundled version, and (if newer) emits a single signal so the UI can
show a dialog with a link to the release page. The user downloads + runs the
new installer themselves — silent self-replacement under Program Files would
need elevation, which is awkward to handle correctly.
"""
from __future__ import annotations

import asyncio
import logging
import re

import httpx

from app import __version__ as CURRENT_VERSION

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
_VERSION_RE = re.compile(r"\d+")


def _parse_version(tag: str) -> tuple[int, ...]:
    """Loose semver parse: 'v0.2.1' -> (0,2,1). Non-numeric suffixes ignored."""
    nums = _VERSION_RE.findall(tag)
    return tuple(int(n) for n in nums[:4]) if nums else (0,)


def is_newer(remote: str, local: str = CURRENT_VERSION) -> bool:
    return _parse_version(remote) > _parse_version(local)


async def check_for_update(owner: str, repo: str, timeout: float = 8.0) -> dict | None:
    """Returns {tag, name, url, body, asset_url?} if a newer release exists, else None."""
    if not owner or not repo:
        return None
    url = f"{GITHUB_API}/repos/{owner}/{repo}/releases/latest"
    try:
        async with httpx.AsyncClient(timeout=timeout, headers={"Accept": "application/vnd.github+json"}) as client:
            r = await client.get(url)
        if r.status_code == 404:
            logger.info("No releases yet for %s/%s", owner, repo)
            return None
        if r.status_code != 200:
            logger.warning("GitHub releases HTTP %s", r.status_code)
            return None
    except Exception as exc:
        logger.info("Update check failed: %s", exc)
        return None

    data = r.json()
    tag = data.get("tag_name") or ""
    if not is_newer(tag):
        return None

    # Prefer a .exe asset if one is attached (installer)
    asset_url = None
    for asset in data.get("assets", []):
        name = asset.get("name", "")
        if name.lower().endswith(".exe"):
            asset_url = asset.get("browser_download_url")
            break

    return {
        "tag": tag,
        "name": data.get("name") or tag,
        "url": data.get("html_url") or "",
        "body": data.get("body") or "",
        "asset_url": asset_url,
    }


def schedule_check(owner: str, repo: str, on_available) -> asyncio.Task:
    """Fire the check on the current event loop. `on_available(release_dict)` is
    invoked from the loop thread if a newer release is found."""
    async def _runner():
        info = await check_for_update(owner, repo)
        if info:
            on_available(info)
    return asyncio.ensure_future(_runner())
