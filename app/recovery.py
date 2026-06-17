"""Recover saved sessions whose notes never generated.

Notes are produced *after* a session is finalized (the transcript is already
safe on disk in the .jsonl). If the app is killed during summarization, the
meeting is left as an "orphan": a session .jsonl with no matching notes .md.
This module regenerates the notes from the saved transcript and re-runs the
exports — used both on app startup and by the standalone recover_session.py.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import os
from datetime import datetime
from pathlib import Path

from app.settings import AppSettings
from intelligence.notes_engine import NotesEngine, generate_title
from intelligence.templates import get_prompt
from intelligence.clients.ollama import OllamaClient
from models.models import Utterance

logger = logging.getLogger(__name__)


def find_orphans(settings: AppSettings) -> list[str]:
    """Session ids with a transcript but no notes .md. Top-level only, so a
    session moved to sessions/deleted/ is intentionally ignored."""
    notes_dir = settings.notes_dir
    orphans = []
    for jsonl in settings.session_dir.glob("*.jsonl"):
        sid = jsonl.stem
        if (notes_dir / f"{sid}.md").exists():
            continue
        # Must contain at least one utterance to be worth recovering.
        try:
            if '"type": "utterance"' in jsonl.read_text(encoding="utf-8"):
                orphans.append(sid)
        except OSError:
            continue
    return orphans


def _load_utterances(jsonl_path: Path) -> list[Utterance]:
    utts: list[Utterance] = []
    try:
        lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return utts
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if o.get("type") != "utterance":
            continue
        try:
            ts = datetime.fromisoformat(o["timestamp"])
        except (KeyError, ValueError):
            ts = datetime.now()
        utts.append(Utterance(speaker=o.get("speaker", "you"), text=o.get("text", ""), timestamp=ts))
    return utts


def _env_secrets() -> dict[str, str]:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    out: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


async def recover_one(settings: AppSettings, sid: str) -> str | None:
    """Regenerate notes for one orphaned session and re-run exports.
    Returns the meeting title on success, None on failure."""
    jsonl = settings.session_dir / f"{sid}.jsonl"
    utterances = _load_utterances(jsonl)
    if not utterances:
        return None
    logger.info("recover: %s — %d utterances", sid, len(utterances))

    client = OllamaClient(settings.ollama_base_url, settings.ollama_llm_model,
                          settings.ollama_embedding_model)

    # Calendar context from when the meeting actually happened.
    meeting_ctx = None
    started = utterances[0].timestamp
    if settings.calendar_enabled:
        try:
            from integrations.outlook_calendar import find_meeting
            local = started.astimezone().replace(tzinfo=None) if started.tzinfo else started
            import asyncio
            meeting_ctx = await asyncio.to_thread(find_meeting, local)
        except Exception as exc:
            logger.info("recover: calendar lookup failed: %s", exc)

    extra = f"\n\nContext for this meeting:\n{meeting_ctx.prompt_block()}" if meeting_ctx else ""
    engine = NotesEngine(llm_complete=client.complete,
                         system_prompt=get_prompt(settings.notes_template) + extra)
    notes_text = await engine.generate(utterances)
    if not notes_text:
        return None

    if meeting_ctx and meeting_ctx.subject:
        title_root = meeting_ctx.subject
    else:
        title_root = (await generate_title(client.complete, notes_text)) or "Recovered Meeting"
    title = f"{title_root} ({started.astimezone():%Y-%m-%d})"

    if meeting_ctx and meeting_ctx.notes_header():
        notes_text = f"{meeting_ctx.notes_header()}\n\n{notes_text}"
    notes_md = f"# {title}\n\n{notes_text}\n"

    # notes .md (clears orphan status), KB copy, Notion, MemPalace.
    (settings.notes_dir / f"{sid}.md").write_text(notes_md, encoding="utf-8")

    if settings.kb_folder:
        kb_dir = Path(settings.kb_folder) / "Meeting Notes"
        kb_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r'[<>:"/\\|?*]', "", title).strip()[:120] or sid
        (kb_dir / f"{safe}.md").write_text(notes_md, encoding="utf-8")

    if settings.notion_enabled:
        api_key = settings.get_secret("notion_api_key") or _env_secrets().get("NOTION_API_KEY", "")
        page_id = settings.notion_page_id or _env_secrets().get("NOTION_PAGE_ID", "")
        if api_key and page_id:
            try:
                from integrations.notion import NotionExporter
                await NotionExporter(api_key, page_id).create_page(title, notes_text, utterances=None)
            except Exception as exc:
                logger.warning("recover: Notion export failed: %s", exc)

    if settings.mempalace_enabled:
        try:
            import asyncio
            await asyncio.to_thread(lambda: subprocess.run(
                [settings.mempalace_exe or "mempalace", "mine", str(settings.notes_dir)],
                capture_output=True, text=True, timeout=600,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"}))
        except Exception as exc:
            logger.warning("recover: MemPalace mine failed: %s", exc)

    logger.info("recover: %s -> %r", sid, title)
    return title
