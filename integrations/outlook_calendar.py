"""Outlook (classic, COM) calendar lookup — local-first, no Graph API / Azure app.

Finds the calendar event matching a point in time so sessions can be tagged
with the real meeting subject, attendees, and agenda. Queries use DASL with
ISO dates because string restrictions like [Start] >= '06/10/2026' are parsed
with the machine locale and silently return wrong windows on non-US systems.

COM must be initialized per-thread (pythoncom.CoInitialize) — callers run
find_meeting() via asyncio.to_thread, so init/uninit happens inside.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_BODY_MAX = 1500
# Teams/Zoom boilerplate adds nothing for the LLM; strip from body if present.
_BOILERPLATE_RE = re.compile(
    r"_{10,}.*|Microsoft Teams.*|Join on your computer.*|Meeting ID:.*|Passcode:.*|"
    r"Download Teams.*|Learn more.*|https://teams\.microsoft\.com\S+",
    re.IGNORECASE,
)


@dataclass
class MeetingContext:
    subject: str
    organizer: str = ""
    attendees: list[str] = field(default_factory=list)
    body: str = ""
    start: datetime | None = None
    end: datetime | None = None

    def prompt_block(self) -> str:
        """Compact context block for LLM prompts."""
        lines = [f"Meeting: {self.subject}"]
        if self.organizer:
            lines.append(f"Organizer: {self.organizer}")
        if self.attendees:
            lines.append(f"Attendees: {', '.join(self.attendees[:12])}")
        if self.body:
            lines.append(f"Agenda/invite notes: {self.body[:600]}")
        return "\n".join(lines)

    def notes_header(self) -> str:
        """Markdown header lines for the notes file / Notion page."""
        lines = []
        if self.organizer:
            lines.append(f"**Organizer:** {self.organizer}")
        if self.attendees:
            lines.append(f"**Attendees:** {', '.join(self.attendees[:20])}")
        return "\n".join(lines)


def _clean_body(raw: str) -> str:
    text = _BOILERPLATE_RE.sub("", raw or "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:_BODY_MAX]


def _split_attendees(*fields: str) -> list[str]:
    names: list[str] = []
    for f in fields:
        for part in (f or "").split(";"):
            name = part.strip()
            if name and name not in names:
                names.append(name)
    return names


def _naive(dt) -> datetime | None:
    """pywin32 returns local wall-clock times mislabeled as UTC — compare naive."""
    if dt is None:
        return None
    return datetime(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)


def find_meeting(at: datetime | None = None, window_minutes: int = 20) -> MeetingContext | None:
    """Return the calendar event covering `at` (default: now), or the nearest
    event starting/ending within `window_minutes`. None if Outlook is
    unavailable or nothing matches."""
    at = at or datetime.now()
    at = at.replace(tzinfo=None)

    try:
        import pythoncom
        import win32com.client
    except ImportError:
        logger.info("pywin32 not available — calendar lookup disabled")
        return None

    pythoncom.CoInitialize()
    try:
        try:
            outlook = win32com.client.Dispatch("Outlook.Application")
            ns = outlook.GetNamespace("MAPI")
            cal = ns.GetDefaultFolder(9)  # olFolderCalendar
        except Exception as exc:
            logger.info("Outlook COM unavailable: %s", exc)
            return None

        items = cal.Items
        items.IncludeRecurrences = True
        items.Sort("[Start]")

        lo = at - timedelta(hours=12)
        hi = at + timedelta(hours=12)
        dasl = (
            '@SQL="urn:schemas:calendar:dtstart" <= '
            f"'{hi:%Y-%m-%d %H:%M}' AND "
            '"urn:schemas:calendar:dtend" >= '
            f"'{lo:%Y-%m-%d %H:%M}'"
        )
        try:
            candidates = items.Restrict(dasl)
        except Exception as exc:
            logger.warning("calendar DASL restrict failed: %s", exc)
            return None

        best: tuple[float, object] | None = None
        count = 0
        for it in candidates:
            count += 1
            if count > 200:  # recurrence safety valve
                break
            subject = (getattr(it, "Subject", "") or "").strip()
            if not subject or subject.lower().startswith("canceled"):
                continue
            start, end = _naive(getattr(it, "Start", None)), _naive(getattr(it, "End", None))
            if not start or not end:
                continue
            window = timedelta(minutes=window_minutes)
            if start - window <= at <= end + window:
                # Prefer events actually containing `at`, then nearest start;
                # tie-break on shorter duration (more specific than all-day).
                contains = 0 if start <= at <= end else 1
                score = (contains, abs((start - at).total_seconds()),
                         (end - start).total_seconds())
                if best is None or score < best[0]:
                    best = (score, it)

        if best is None:
            return None
        it = best[1]
        ctx = MeetingContext(
            subject=(it.Subject or "").strip(),
            organizer=(getattr(it, "Organizer", "") or "").strip(),
            attendees=_split_attendees(
                getattr(it, "RequiredAttendees", ""), getattr(it, "OptionalAttendees", "")
            ),
            body=_clean_body(getattr(it, "Body", "")),
            start=_naive(it.Start),
            end=_naive(it.End),
        )
        logger.info("calendar match: %r (%s - %s)", ctx.subject, ctx.start, ctx.end)
        return ctx
    finally:
        pythoncom.CoUninitialize()
