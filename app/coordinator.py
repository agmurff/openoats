import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from PySide6.QtCore import QObject, Signal
from app.settings import AppSettings
from audio.mic_capture import MicCapture
from audio.system_capture import SystemCapture
from transcription.engine import TranscriptionEngine
from intelligence.suggestion_engine import SuggestionEngine
from intelligence.notes_engine import NotesEngine
from intelligence.knowledge_base import KnowledgeBase
from storage.session_store import SessionStore
from storage.transcript_store import TranscriptStore
from models.models import Utterance, Suggestion, SessionIndex

logger = logging.getLogger(__name__)


class AppCoordinator(QObject):
    session_started = Signal()
    session_ended = Signal(object)           # SessionIndex
    system_audio_unavailable = Signal()
    toast_requested = Signal(str, str)       # (message, level)
    suggestion_ready = Signal(object)        # Suggestion
    notes_chunk_ready = Signal(str)          # notes text chunk

    def __init__(self, settings: AppSettings = None, parent=None):
        super().__init__(parent)
        self.settings = settings or AppSettings()
        self._loop = asyncio.get_event_loop()

        self._migrate_legacy_env()

        self.transcript_store = TranscriptStore()
        self._mic = MicCapture(self._loop, device=self.settings.input_device)
        self._sys = SystemCapture(self._loop)
        self._engine: TranscriptionEngine | None = None
        self._session_store: SessionStore | None = None
        self._engine_task: asyncio.Task | None = None
        self._last_session_id: str | None = None
        self._last_title: str = "Meeting"

        self.kb = self._build_kb()
        self.suggestion_engine: SuggestionEngine | None = None
        self.notes_engine: NotesEngine | None = None

    def _build_kb(self) -> KnowledgeBase | None:
        # Fall back to the notes folder so past meeting notes feed live suggestions
        # out of the box (the same .md files we also ingest into MemPalace).
        folder = Path(self.settings.kb_folder) if self.settings.kb_folder else self.settings.notes_dir
        embed_fn = self._get_embed_fn()
        if not embed_fn:
            return None
        return KnowledgeBase(folder=folder, embed_fn=embed_fn)

    def _get_embed_fn(self):
        provider = self.settings.embedding_provider
        if provider == "voyage":
            key = self.settings.get_secret("voyage_api_key") or ""
            if not key:
                return None
            from intelligence.clients.voyage import VoyageClient
            client = VoyageClient(api_key=key, model=self.settings.embedding_model)
            return client.embed
        elif provider == "ollama":
            from intelligence.clients.ollama import OllamaClient
            client = OllamaClient(
                self.settings.ollama_base_url,
                self.settings.ollama_llm_model,
                self.settings.ollama_embedding_model,
            )
            return client.embed
        return None

    def _get_llm_fn(self):
        provider = self.settings.llm_provider
        if provider == "openrouter":
            key = self.settings.get_secret("openrouter_api_key") or ""
            if not key:
                return None
            from intelligence.clients.openrouter import OpenRouterClient
            client = OpenRouterClient(api_key=key, model=self.settings.llm_model)
            return client.complete
        elif provider == "ollama":
            from intelligence.clients.ollama import OllamaClient
            client = OllamaClient(
                self.settings.ollama_base_url,
                self.settings.ollama_llm_model,
                self.settings.ollama_embedding_model,
            )
            return client.complete
        elif provider == "custom":
            from intelligence.clients.openrouter import OpenRouterClient
            client = OpenRouterClient(
                api_key="",
                base_url=self.settings.custom_base_url,
                model=self.settings.custom_llm_model,
            )
            return client.complete
        return None

    def _migrate_legacy_env(self) -> None:
        """One-time import of Notion credentials from the old project's .env
        (C:\\Meeting_Transcript\\.env) into settings + keyring, so the rebuild
        works out of the box without re-entering keys."""
        if self.settings.notion_page_id and (self.settings.get_secret("notion_api_key")):
            return
        env_path = Path(__file__).resolve().parents[2] / ".env"
        if not env_path.exists():
            return
        try:
            values = {}
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
        except OSError:
            return

        api_key = values.get("NOTION_API_KEY")
        page_id = values.get("NOTION_PAGE_ID")
        changed = False
        if page_id and not self.settings.notion_page_id:
            self.settings.notion_page_id = page_id
            changed = True
        if api_key and not self.settings.get_secret("notion_api_key"):
            self.settings.set_secret("notion_api_key", api_key)
            changed = True
        if changed and api_key and page_id:
            self.settings.notion_enabled = True
            self.settings.save()
            logger.info("Migrated Notion credentials from legacy .env; Notion export enabled")

    async def _ingest_to_mempalace(self) -> None:
        """Mine the notes folder into MemPalace so finished meetings become
        searchable memories. Runs the mempalace CLI as a subprocess (the CLI
        lives in a different Python env than this app)."""
        exe = self.settings.mempalace_exe or "mempalace"
        notes_dir = str(self.settings.notes_dir)
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

        def _run():
            import subprocess
            return subprocess.run(
                [exe, "mine", notes_dir],
                capture_output=True, text=True, env=env, timeout=600,
            )

        result = await asyncio.to_thread(_run)
        if result.returncode == 0:
            logger.info("MemPalace ingest OK (%s)", notes_dir)
            self.toast_requested.emit("Notes filed into MemPalace", "success")
        else:
            logger.warning("MemPalace ingest failed (%s): %s", result.returncode, result.stderr[:500])

    async def start_session(self) -> None:
        # Breadcrumbs at every milestone — if start_session crashes natively,
        # the last log line tells us exactly which step died.
        logger.info("start_session: begin (model=%s, transcript backend=base)",
                    self.settings.transcription_model)
        self.transcript_store.clear()

        self._session_store = SessionStore(
            session_dir=self.settings.session_dir,
            notes_dir=self.settings.notes_dir,
        )
        logger.info("start_session: session store opened (id=%s)", self._session_store.session_id)

        self.toast_requested.emit("Loading transcription model…", "info")
        logger.info("start_session: loading TranscriptionEngine (this can download the model)")
        self._engine = await asyncio.to_thread(
            TranscriptionEngine,
            model_dir=self.settings.model_dir,
            model_size=self.settings.transcription_model,
        )
        logger.info("start_session: TranscriptionEngine loaded")

        logger.info("start_session: starting mic capture")
        self._mic.start()
        if not self._mic.available:
            logger.warning("start_session: mic unavailable")
            self.toast_requested.emit(
                "Microphone access denied — check Windows Privacy Settings > Microphone", "error"
            )
            return
        logger.info("start_session: mic OK")

        logger.info("start_session: starting WASAPI loopback (system audio)")
        self._sys.start()
        if not self._sys.available:
            logger.warning("start_session: system audio unavailable")
            self.system_audio_unavailable.emit()
        else:
            logger.info("start_session: system audio OK")
        self._engine.on_utterance = self._on_utterance
        self._engine.on_partial = self.transcript_store.update_partial

        llm_fn = self._get_llm_fn()
        if self.kb and llm_fn:
            self.suggestion_engine = SuggestionEngine(
                kb=self.kb,
                llm_complete=llm_fn,
                on_suggestion=self._on_suggestion,
            )
        from intelligence.templates import get_prompt
        self.notes_engine = NotesEngine(
            llm_complete=llm_fn,
            system_prompt=get_prompt(self.settings.notes_template),
        )

        logger.info("start_session: launching transcription task")
        self._engine_task = asyncio.create_task(
            self._engine.start(self._mic.stream(), self._sys.stream())
        )
        self.session_started.emit()
        logger.info("start_session: complete — recording")

    async def end_session(self) -> None:
        if self._engine_task:
            self._engine_task.cancel()
            try:
                await self._engine_task
            except asyncio.CancelledError:
                pass

        self._mic.stop()
        self._sys.stop()

        if self._session_store:
            topic = self.transcript_store.conversation_state.topic or "Meeting"
            self._last_session_id = self._session_store.session_id
            self._last_title = topic
            index = self._session_store.finalize(title=topic, template_id=None)
            self.session_ended.emit(index)

        if self.notes_engine:
            asyncio.ensure_future(self._generate_notes())

    async def _generate_notes(self) -> None:
        utterances = list(self.transcript_store.utterances)
        if not utterances:
            return
        try:
            chunk = await self.notes_engine.generate(utterances)
        except Exception as exc:
            logger.warning("Notes generation failed: %s", exc)
            return
        if not chunk:
            return
        self.notes_chunk_ready.emit(chunk)
        await self._persist_and_export(chunk, utterances)

    async def _persist_and_export(self, notes_text: str, utterances=None) -> None:
        """Write notes to a .md file (upstream never did this), then push to Notion
        and ingest into MemPalace if those integrations are enabled."""
        session_id = self._last_session_id
        if not session_id:
            return

        # Ask the LLM for a meeting-specific title; fall back to the topic/state title.
        title_root = self._last_title
        llm_fn = self._get_llm_fn()
        if llm_fn:
            from intelligence.notes_engine import generate_title
            generated = await generate_title(llm_fn, notes_text)
            if generated:
                title_root = generated
        date_str = datetime.now().strftime("%Y-%m-%d")
        page_title = f"{title_root} ({date_str})"

        notes_path = self.settings.notes_dir / f"{session_id}.md"
        try:
            await asyncio.to_thread(
                notes_path.write_text,
                f"# {page_title}\n\n{notes_text}\n",
                "utf-8",
            )
            logger.info("Notes written: %s", notes_path)
        except OSError as exc:
            logger.warning("Could not write notes file: %s", exc)

        if self.settings.notion_enabled:
            try:
                # Notes only — the full transcript stays local (session .jsonl) and
                # cluttered the Notion page without adding recall value.
                await self._export_to_notion(page_title, notes_text, utterances=None)
                self.toast_requested.emit("Notes saved to Notion", "success")
            except Exception as exc:
                logger.warning("Notion export failed: %s", exc)
                self.toast_requested.emit(f"Notion export failed: {exc}", "error")

        if self.settings.mempalace_enabled:
            try:
                await self._ingest_to_mempalace()
            except Exception as exc:
                logger.warning("MemPalace ingest failed: %s", exc)

    async def _export_to_notion(self, title: str, notes_text: str, utterances=None) -> None:
        api_key = self.settings.get_secret("notion_api_key") or ""
        page_id = self.settings.notion_page_id
        if not api_key or not page_id:
            logger.info("Notion enabled but API key or page id missing — skipping export")
            return
        from integrations.notion import NotionExporter
        exporter = NotionExporter(api_key=api_key, parent_page_id=page_id)
        await exporter.create_page(title, notes_text, utterances=utterances)

    def _on_utterance(self, utterance: Utterance) -> None:
        self.transcript_store.append(utterance)
        if self._session_store:
            self._session_store.write_utterance(utterance)
        # Respect the live Ideas toggle — skip the LLM-gated suggestion pipeline when off
        if self.suggestion_engine and self.settings.suggestions_enabled:
            asyncio.create_task(self.suggestion_engine.on_utterance(utterance))

    def set_suggestions_enabled(self, enabled: bool) -> None:
        """Toggle live suggestions on/off mid-session. Persists the preference."""
        self.settings.suggestions_enabled = bool(enabled)
        self.settings.save()

    def _on_suggestion(self, suggestion: Suggestion) -> None:
        if self._session_store:
            self._session_store.write_suggestion(suggestion)
        self.suggestion_ready.emit(suggestion)

    def record_feedback(self, suggestion_id: str, polarity: str) -> None:
        if self._session_store:
            self._session_store.write_feedback(suggestion_id, polarity)
