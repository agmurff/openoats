"""Post-meeting transcript cleanup. Whisper ASR makes word-level mistakes
(names, jargon, homophones) and no punctuation structure; a strong LLM can
repair the text using whole-meeting context before notes are written.

Returns corrected, readable markdown that preserves speaker turns. Falls back
to the raw formatted transcript on any failure so a meeting is never lost.
"""
from __future__ import annotations

import logging
from models.models import Utterance

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You clean up raw speech-to-text meeting transcripts. The input is an "
    "automatic transcript with likely errors: mis-heard words, wrong names, "
    "missing punctuation, broken sentences. Produce a corrected, readable "
    "transcript.\n"
    "RULES:\n"
    "- Fix obvious mishearings, punctuation, and capitalisation using context.\n"
    "- Correct names/jargon to the most plausible intended term (use the "
    "attendee list / agenda if given).\n"
    "- Keep speaker turns; merge a speaker's fragmented lines into coherent sentences.\n"
    "- Do NOT summarise, omit, invent, or editorialise — only repair what's there.\n"
    "- Output plain markdown: one line per turn as 'Speaker: text'.\n"
    "- Output ONLY the corrected transcript — no preamble, heading, or commentary.\n"
    "- If a passage is truly unintelligible, leave it close to verbatim rather than guessing wildly."
)

# Headroom: clean in slices so even multi-hour meetings stay within one call each.
_SLICE_CHARS = 24000


def _format(utterances: list[Utterance]) -> str:
    lines = []
    for u in utterances:
        who = "You" if u.speaker == "you" else "Them"
        lines.append(f"{who}: {u.text}")
    return "\n".join(lines)


async def clean_transcript(llm_complete, utterances: list[Utterance],
                           context: str = "") -> str:
    raw = _format(utterances)
    if not raw.strip():
        return raw

    # Split on line boundaries into slices under the char budget.
    slices, current = [], ""
    for line in raw.splitlines():
        if len(current) + len(line) + 1 > _SLICE_CHARS and current:
            slices.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        slices.append(current)

    ctx_block = f"\n\nMeeting context (for correcting names/terms):\n{context}" if context else ""
    cleaned_parts = []
    for i, sl in enumerate(slices):
        user = (f"Transcript part {i+1}/{len(slices)}:{ctx_block}\n\n{sl}"
                if len(slices) > 1 else f"{ctx_block}\n\nTranscript:\n{sl}")
        try:
            out = await llm_complete([
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user},
            ])
        except Exception as exc:
            logger.warning("transcript cleanup failed on slice %d: %s — using raw", i, exc)
            return raw
        cleaned_parts.append((out or "").strip())
    cleaned = "\n".join(p for p in cleaned_parts if p)
    return cleaned or raw
