"""Manually recover a saved session whose notes never generated (e.g. the app
crashed after Stop, during summarization). The app also does this automatically
on startup — this script is for forcing a specific session or running headless.

Usage:
    .venv\\Scripts\\python.exe recover_session.py <session_id>
    .venv\\Scripts\\python.exe recover_session.py --all   # every orphaned session
"""
from __future__ import annotations

import os
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import argparse
import asyncio
import logging
import sys

from app.settings import AppSettings
from app.recovery import find_orphans, recover_one

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("recover_session")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_id", nargs="?", help="Session UUID (stem of the .jsonl)")
    parser.add_argument("--all", action="store_true", help="Recover every orphaned session")
    args = parser.parse_args()

    settings = AppSettings()

    if args.all:
        sids = find_orphans(settings)
        if not sids:
            log.info("No orphaned sessions found.")
            return 0
        log.info("Recovering %d orphaned session(s)", len(sids))
    elif args.session_id:
        sids = [args.session_id]
    else:
        sids = find_orphans(settings)
        if not sids:
            log.info("No orphaned sessions found.")
            return 0
        log.info("Recovering latest orphan: %s", sids[0])
        sids = sids[:1]

    ok = 0
    for sid in sids:
        title = await recover_one(settings, sid)
        if title:
            ok += 1
            log.info("recovered: %s", title)
        else:
            log.warning("could not recover: %s", sid)
    log.info("done — %d/%d recovered", ok, len(sids))
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
