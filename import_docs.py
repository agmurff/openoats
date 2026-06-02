"""Import documents (PDF/Word/Excel/CSV/PPTX/HTML/...) into the OpenOats notes
folder as Markdown so they feed the local .md knowledge base AND get ingested
into MemPalace on the next session-end (or manual mine).

Usage:
    .venv\\Scripts\\python.exe import_docs.py [--inbox PATH] [--keep] [--mine]

Defaults:
    --inbox  C:\\Meeting_Transcript\\Inbox
    Output    Documents\\OpenOats\\notes\\imports\\<sanitized-original-name>.md
    Originals are deleted after a successful convert unless --keep is given.
    --mine   Run `mempalace mine <notes_dir>` after conversion.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from markitdown import MarkItDown

from app.settings import AppSettings

logger = logging.getLogger("import_docs")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DEFAULT_INBOX = Path(r"C:\Meeting_Transcript\Inbox")

# Files MarkItDown handles well. Anything else is skipped (and left in place).
SUPPORTED = {
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
    ".csv", ".tsv", ".html", ".htm", ".xml", ".json",
    ".txt", ".rtf", ".epub", ".msg",
    ".jpg", ".jpeg", ".png",  # OCR-capable
    ".mp3", ".wav", ".m4a",   # transcription via markitdown's audio plugin
    ".zip",
}


def _slugify(name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return base or "import"


def _unique_path(target: Path) -> Path:
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    i = 2
    while True:
        candidate = target.with_name(f"{stem}_{i}{suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def convert_one(src: Path, out_dir: Path, md: MarkItDown) -> Path | None:
    try:
        result = md.convert(str(src))
    except Exception as exc:
        logger.warning("Skipped %s: %s", src.name, exc)
        return None

    text = (result.text_content or "").strip()
    if not text:
        logger.warning("Skipped %s: no extractable content", src.name)
        return None

    title = result.title or src.stem
    out_name = _slugify(src.stem) + ".md"
    out_path = _unique_path(out_dir / out_name)

    header = (
        f"# {title}\n\n"
        f"> Imported from `{src.name}` on {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
    )
    out_path.write_text(header + text + "\n", encoding="utf-8")
    logger.info("Converted %s -> %s", src.name, out_path)
    return out_path


def run_mempalace_mine(notes_dir: Path, exe: str = "mempalace") -> None:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        result = subprocess.run(
            [exe, "mine", str(notes_dir)],
            capture_output=True, text=True, env=env, timeout=600,
        )
    except FileNotFoundError:
        logger.warning("mempalace CLI not found on PATH; skipped mine")
        return
    if result.returncode == 0:
        logger.info("MemPalace mine OK")
    else:
        logger.warning("MemPalace mine failed (%s): %s", result.returncode, result.stderr[:300])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inbox", type=Path, default=DEFAULT_INBOX)
    parser.add_argument("--keep", action="store_true",
                        help="Don't delete originals after successful conversion")
    parser.add_argument("--mine", action="store_true",
                        help="Run `mempalace mine` over the notes folder afterwards")
    args = parser.parse_args()

    if not args.inbox.exists():
        logger.error("Inbox does not exist: %s", args.inbox)
        return 1

    settings = AppSettings()
    notes_dir: Path = settings.notes_dir
    imports_dir = notes_dir / "imports"
    imports_dir.mkdir(parents=True, exist_ok=True)

    md = MarkItDown()
    sources = [p for p in args.inbox.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED]
    if not sources:
        logger.info("Nothing to import in %s", args.inbox)
        return 0

    converted = 0
    for src in sources:
        out = convert_one(src, imports_dir, md)
        if out is None:
            continue
        converted += 1
        if not args.keep:
            try:
                src.unlink()
            except OSError as exc:
                logger.warning("Could not delete %s: %s", src.name, exc)

    logger.info("Done. Converted %d/%d files into %s", converted, len(sources), imports_dir)

    if args.mine and converted:
        run_mempalace_mine(notes_dir, exe=settings.mempalace_exe or "mempalace")

    return 0


if __name__ == "__main__":
    sys.exit(main())
