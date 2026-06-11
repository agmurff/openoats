import asyncio
import json
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Callable
from models.models import (
    Utterance, Suggestion, SuggestionTrigger, SuggestionEvidence,
    SuggestionDecision, ConversationState,
)
from intelligence.knowledge_base import KnowledgeBase

logger = logging.getLogger(__name__)

COOLDOWN_SECONDS = 90
MIN_WORDS = 8
MIN_CHARS = 30
RELEVANCE_THRESHOLD = 0.72

QUESTION_PATTERNS = re.compile(r'\b(what|how|why|when|where|who|which|could you|can you)\b|\?', re.IGNORECASE)
DECISION_PATTERNS = re.compile(r'\b(should we|which option|decide|go with|choose|pick)\b', re.IGNORECASE)
DISAGREEMENT_PATTERNS = re.compile(r'\b(but|however|disagree|not sure|concern|issue)\b', re.IGNORECASE)
ASSUMPTION_PATTERNS = re.compile(r'\b(i think|i assume|what if|maybe|perhaps)\b', re.IGNORECASE)
DOMAIN_PATTERNS = re.compile(r'\b(customer|mvp|pricing|retention|churn|revenue|metric|kpi)\b', re.IGNORECASE)


def detect_trigger(utterance: Utterance) -> SuggestionTrigger | None:
    text = utterance.text
    if QUESTION_PATTERNS.search(text):
        return SuggestionTrigger(kind="explicit_question", confidence=0.9)
    if DECISION_PATTERNS.search(text):
        return SuggestionTrigger(kind="decision_point", confidence=0.85)
    if DISAGREEMENT_PATTERNS.search(text):
        return SuggestionTrigger(kind="disagreement", confidence=0.8)
    if ASSUMPTION_PATTERNS.search(text):
        return SuggestionTrigger(kind="assumption", confidence=0.75)
    if DOMAIN_PATTERNS.search(text):
        return SuggestionTrigger(kind="domain", confidence=0.7)
    return None


def parse_llm_json(raw: str) -> dict | None:
    """Small local models routinely wrap JSON in ```json fences or add prose.
    Strip fences and extract the first {...} block before parsing."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def pre_filter(utterance: Utterance, last_suggestion_time: datetime | None) -> bool:
    text = utterance.text.strip()
    if len(text.split()) < MIN_WORDS or len(text) < MIN_CHARS:
        return False
    if last_suggestion_time:
        elapsed = (datetime.now(timezone.utc) - last_suggestion_time).total_seconds()
        if elapsed < COOLDOWN_SECONDS:
            return False
    return True


class SuggestionEngine:
    def __init__(self, kb: KnowledgeBase, llm_complete, on_suggestion: Callable[[Suggestion], None],
                 meeting_context: str = ""):
        self._kb = kb
        self._llm = llm_complete
        self._on_suggestion = on_suggestion
        self._meeting_context = meeting_context  # calendar subject/attendees/agenda
        self._state = ConversationState()
        self._last_suggestion: datetime | None = None
        self._recent: list[Utterance] = []

    async def on_utterance(self, utterance: Utterance) -> None:
        self._recent.append(utterance)
        if len(self._recent) > 20:
            self._recent.pop(0)

        trigger = detect_trigger(utterance)
        if not trigger:
            return
        if not pre_filter(utterance, self._last_suggestion):
            return

        try:
            await self._run_pipeline(utterance, trigger)
        except Exception as e:
            logger.warning("Suggestion pipeline error: %s", e)

    async def _run_pipeline(self, utterance: Utterance, trigger: SuggestionTrigger) -> None:
        context = "\n".join(f"{u.speaker}: {u.text}" for u in self._recent[-6:])
        meeting_part = f"{self._meeting_context}\n\n" if self._meeting_context else ""
        state_prompt = [
            {"role": "system", "content": "Update the conversation state as JSON: {topic, summary, open_questions, tensions, decisions, goals}."},
            {"role": "user", "content": f"{meeting_part}Transcript:\n{context}\n\nCurrent state: {json.dumps(self._state.__dict__)}"},
        ]
        try:
            raw = await self._llm(state_prompt)
            new_state = parse_llm_json(raw)
            for k, v in (new_state or {}).items():
                if hasattr(self._state, k):
                    setattr(self._state, k, v)
        except Exception:
            pass

        # The calendar subject names the project — it anchors KB search to the
        # right material even in the first minutes before the topic state warms up.
        subject_line = self._meeting_context.splitlines()[0].removeprefix("Meeting: ") \
            if self._meeting_context else ""
        queries = [utterance.text, self._state.topic, subject_line]
        results = []
        for q in queries:
            if q:
                results.extend(await self._kb.search(q, top_k=5))
        if not results:
            return

        evidence_text = "\n".join(f"[{r.source_file}] {r.text}" for r in results[:3])
        gate_prompt = [
            {"role": "system", "content": (
                "You are a meeting assistant. Given the utterance and evidence, decide whether to surface a suggestion. "
                "Respond as JSON: {relevance, helpfulness, novelty, timing, surfaced, headline, coaching, text}. "
                "All scores 0.0–1.0. Only set surfaced=true if relevance >= 0.72 AND the suggestion is genuinely helpful "
                "for THIS meeting's purpose."
            )},
            {"role": "user", "content": f"{meeting_part}Utterance: {utterance.text}\n\nEvidence:\n{evidence_text}"},
        ]
        try:
            raw = await self._llm(gate_prompt)
        except Exception:
            return
        data = parse_llm_json(raw)
        if data is None:
            logger.debug("suggestion gate returned unparseable output: %.200s", raw)
            return

        decision = SuggestionDecision(
            relevance=data.get("relevance", 0),
            helpfulness=data.get("helpfulness", 0),
            novelty=data.get("novelty", 0),
            timing=data.get("timing", 0),
            surfaced=data.get("surfaced", False),
        )
        if not decision.surfaced:
            return

        suggestion = Suggestion(
            headline=data.get("headline", ""),
            coaching=data.get("coaching", ""),
            text=data.get("text", ""),
            trigger=trigger,
            evidence=[SuggestionEvidence(r.text, r.source_file, r.header_context, r.relevance_score) for r in results[:3]],
            decision=decision,
        )
        self._last_suggestion = datetime.now(timezone.utc)
        self._on_suggestion(suggestion)
