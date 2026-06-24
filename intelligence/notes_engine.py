import asyncio
from typing import Callable
from models.models import Utterance

MAX_CHARS = 60000
KEEP_CHARS = 20000


def format_transcript(utterances: list[Utterance]) -> str:
    lines = []
    for u in utterances:
        speaker = "You" if u.speaker == "you" else "Them"
        ts = u.timestamp.strftime("%H:%M")
        lines.append(f"[{ts}] {speaker}: {u.text}")
    text = "\n".join(lines)
    if len(text) > MAX_CHARS:
        text = text[:KEEP_CHARS] + "\n\n[...middle truncated...]\n\n" + text[-KEEP_CHARS:]
    return text


class NotesEngine:
    def __init__(self, llm_complete, system_prompt: str):
        self._llm = llm_complete
        self._system_prompt = system_prompt
        self.is_generating: bool = False
        self.on_chunk: Callable[[str], None] = lambda c: None
        self._task: asyncio.Task | None = None

    async def generate_text(self, transcript: str) -> str:
        """Generate notes from an already-formatted (e.g. cleaned) transcript string."""
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": transcript},
        ]
        return await self._llm(messages)

    async def generate(self, utterances: list[Utterance]) -> str:
        if self.is_generating:
            return ""
        self.is_generating = True
        transcript = format_transcript(utterances)
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": transcript},
        ]

        async def _run():
            try:
                result = await self._llm(messages)
                self.on_chunk(result)
                return result
            finally:
                self.is_generating = False

        self._task = asyncio.ensure_future(_run())
        return await self._task

    def cancel(self):
        if self._task:
            self._task.cancel()
        self.is_generating = False


async def generate_title(llm_complete, notes_text: str) -> str:
    """Ask the LLM for a short, specific meeting title based on the notes.
    Returns a cleaned single-line title, or '' on any failure (caller falls
    back to a generic title)."""
    messages = [
        {"role": "system", "content": (
            "You name meetings. Reply with ONLY a title for this meeting: "
            "3-7 words, no dates, no quotes, no trailing punctuation. "
            "Use the concrete project, product, or customer names that appear "
            "in the notes — never generic words like 'Meeting', 'Discussion', "
            "'Decisions', or 'Update' on their own. Example good titles: "
            "'Selwyn LR9 Rollout Planning', 'Q3 Pricing Model Review'."
        )},
        {"role": "user", "content": notes_text[:4000]},
    ]
    try:
        raw = await llm_complete(messages)
    except Exception:
        return ""
    title = (raw or "").strip().strip('"').strip("'").splitlines()[0].strip()
    # Guard against the model rambling — a real title is short.
    if not title or len(title) > 80:
        return ""
    return title.rstrip(".!")
