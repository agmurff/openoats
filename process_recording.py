"""Run a recorded audio file through the same pipeline the live app uses:
faster-whisper -> Qwen via NotesEngine -> Notion -> MemPalace.

For long meetings the GUI live-capture path was blocked; this script reproduces
the same outputs (notes .md, Notion child page with transcript, MemPalace
drawer) without needing a real-time mic stream.

Usage:
    .venv\\Scripts\\python.exe process_recording.py <audio_file> [--title "..."]
"""
from __future__ import annotations

# MUST be set before any torch / ctranslate2 import (libiomp5md.dll collision)
import os
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.settings import AppSettings
from intelligence.notes_engine import NotesEngine
from intelligence.templates import get_prompt
from intelligence.clients.ollama import OllamaClient
from integrations.notion import NotionExporter
from models.models import Utterance

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("process_recording")


def transcribe(audio_path: Path, model_dir: Path, model_size: str = "base.en") -> list[Utterance]:
    """Return a list of Utterance objects covering the whole recording.
    No diarization — everything is labeled 'you'."""
    from faster_whisper import WhisperModel
    cache_path = model_dir / f"models--Systran--faster-whisper-{model_size}" / "refs" / "main"
    log.info("loading WhisperModel(%s, int8 CPU)", model_size)
    model = WhisperModel(
        model_size, device="cpu", compute_type="int8",
        download_root=str(model_dir), local_files_only=cache_path.exists(),
    )
    log.info("transcribing %s ...", audio_path.name)

    segments, info = model.transcribe(
        str(audio_path), vad_filter=True, beam_size=1, language="en",
    )
    duration = info.duration or 0
    log.info("audio duration: %.0f s  | language: %s  | starting...", duration, info.language)

    utterances: list[Utterance] = []
    start_wall = time.monotonic()
    last_report = start_wall
    base_time = datetime.now(timezone.utc)
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        ts = base_time.fromtimestamp(base_time.timestamp() + seg.start)
        utterances.append(Utterance(speaker="you", text=text, timestamp=ts))

        now = time.monotonic()
        if now - last_report > 15:
            elapsed = now - start_wall
            ratio = (seg.end / elapsed) if elapsed else 0
            pct = (seg.end / duration * 100) if duration else 0
            log.info("  %.1f / %.0f s  (%.1f%% , %.2fx realtime, utts=%d)",
                     seg.end, duration, pct, ratio, len(utterances))
            last_report = now

    log.info("transcription complete: %d utterances", len(utterances))
    return utterances


def render_notes_md(title: str, notes_text: str) -> str:
    return f"# {title}\n\n{notes_text}\n"


def render_transcript_md(title: str, utterances: list[Utterance]) -> str:
    lines = [f"# Transcript — {title}", ""]
    for u in utterances:
        ts = u.timestamp.strftime("%H:%M:%S")
        lines.append(f"- **{ts}** {u.text}")
    lines.append("")
    return "\n".join(lines)


async def summarize(utterances: list[Utterance], template_name: str,
                    extra_context: str = "") -> str:
    settings = AppSettings()
    client = OllamaClient(
        settings.ollama_base_url, settings.ollama_llm_model, settings.ollama_embedding_model,
    )
    engine = NotesEngine(
        llm_complete=client.complete,
        system_prompt=get_prompt(template_name) + extra_context,
    )
    log.info("summarizing with %s (template=%s) ...", settings.ollama_llm_model, template_name)
    return await engine.generate(utterances)


def load_env_secrets() -> dict[str, str]:
    """Pull Notion credentials from the legacy .env if keyring doesn't have them."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    out: dict[str, str] = {}
    if not env_path.exists():
        return out
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


async def push_notion(title: str, notes_text: str, utterances: list[Utterance]) -> str | None:
    settings = AppSettings()
    api_key = settings.get_secret("notion_api_key") or load_env_secrets().get("NOTION_API_KEY", "")
    page_id = settings.notion_page_id or load_env_secrets().get("NOTION_PAGE_ID", "")
    if not api_key or not page_id:
        log.warning("Notion key or page id missing — skipping Notion push")
        return None
    log.info("pushing to Notion ...")
    exporter = NotionExporter(api_key=api_key, parent_page_id=page_id)
    pid = await exporter.create_page(title, notes_text, utterances=utterances)
    log.info("Notion page created: %s", pid)
    return pid


def mempalace_mine(notes_dir: Path) -> None:
    import subprocess
    settings = AppSettings()
    if not settings.mempalace_enabled:
        log.info("MemPalace disabled in settings — skipping mine")
        return
    exe = settings.mempalace_exe or "mempalace"
    log.info("filing notes into MemPalace ...")
    r = subprocess.run(
        [exe, "mine", str(notes_dir)],
        capture_output=True, text=True, timeout=600,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    if r.returncode == 0:
        log.info("MemPalace mine OK")
    else:
        log.warning("MemPalace mine failed (%s): %s", r.returncode, r.stderr[:300])


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--title", default=None,
                        help="Override the page/notes title; default: filename stem.")
    parser.add_argument("--template", default=None,
                        help="Notes template name (default: from settings).")
    args = parser.parse_args()

    if not args.audio.exists():
        log.error("audio file not found: %s", args.audio)
        return 1

    settings = AppSettings()

    # Match the recording to an Outlook calendar event via its filename
    # timestamp (e.g. '2026-06-09 12-13-14.mp3' from the recorder).
    meeting_ctx = None
    import re as _re
    m = _re.match(r"(\d{4}-\d{2}-\d{2}) (\d{2})-(\d{2})-(\d{2})", args.audio.stem)
    rec_time = (datetime.strptime(f"{m.group(1)} {m.group(2)}:{m.group(3)}", "%Y-%m-%d %H:%M")
                if m else None)
    if settings.calendar_enabled:
        try:
            from integrations.outlook_calendar import find_meeting
            meeting_ctx = await asyncio.to_thread(find_meeting, rec_time)
        except Exception as exc:
            log.info("calendar lookup failed: %s", exc)
        if meeting_ctx:
            log.info("calendar match: %s", meeting_ctx.subject)

    utterances = transcribe(args.audio, settings.model_dir, settings.transcription_model)
    if not utterances:
        log.error("no transcribable speech found")
        return 2

    template_prompt_extra = f"\n\nContext for this meeting:\n{meeting_ctx.prompt_block()}" if meeting_ctx else ""
    notes_text = await summarize(utterances, args.template or settings.notes_template,
                                 extra_context=template_prompt_extra)

    # Title priority: calendar subject > LLM-generated > --title/filename.
    title_root = args.title or args.audio.stem
    client = OllamaClient(
        settings.ollama_base_url, settings.ollama_llm_model, settings.ollama_embedding_model,
    )
    if meeting_ctx and meeting_ctx.subject:
        title_root = meeting_ctx.subject
    else:
        from intelligence.notes_engine import generate_title
        generated = await generate_title(client.complete, notes_text)
        if generated:
            title_root = generated
            log.info("generated title: %s", generated)
    # Date the page by when the meeting happened, not when it was processed.
    title_date = rec_time or (meeting_ctx.start if meeting_ctx else None) or datetime.now()
    title = f"{title_root} ({title_date:%Y-%m-%d})"

    if meeting_ctx:
        header = meeting_ctx.notes_header()
        if header:
            notes_text = f"{header}\n\n{notes_text}"

    # Notes .md goes in the KB-indexed notes folder; transcript goes in
    # sessions (kept out of the KB and out of Notion — recall noise).
    session_id = str(uuid4())
    notes_md = render_notes_md(title, notes_text)
    notes_path = settings.notes_dir / f"{session_id}.md"
    notes_path.parent.mkdir(parents=True, exist_ok=True)
    notes_path.write_text(notes_md, encoding="utf-8")
    log.info("notes written: %s", notes_path)

    # File the report into the knowledge base so future meetings can recall it.
    if settings.kb_folder:
        kb_dir = Path(settings.kb_folder) / "Meeting Notes"
        kb_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _re.sub(r'[<>:"/\\|?*]', "", title).strip()[:120] or session_id
        kb_path = kb_dir / f"{safe_name}.md"
        kb_path.write_text(notes_md, encoding="utf-8")
        log.info("notes filed into KB: %s", kb_path)

    transcript_path = settings.session_dir / f"{session_id}.transcript.md"
    transcript_path.write_text(render_transcript_md(title, utterances), encoding="utf-8")
    log.info("transcript written: %s", transcript_path)

    await push_notion(title, notes_text, utterances=None)
    mempalace_mine(settings.notes_dir)
    log.info("done.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
