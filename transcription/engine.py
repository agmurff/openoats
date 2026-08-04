import asyncio
import torch
from pathlib import Path
from faster_whisper import WhisperModel
from transcription.transcriber import StreamingTranscriber
from transcription.vad import SileroVAD
from typing import Callable
from models.models import Utterance

_PARAKEET_IDS = {"parakeet-tdt-0.6b-v2", "parakeet-tdt-1.1b"}


def _load_model(model_dir: Path, model_id: str, cpu_threads: int = 0):
    if model_id in _PARAKEET_IDS:
        from transcription.parakeet_backend import ParakeetModel
        return ParakeetModel(model_id)
    # faster-whisper
    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute_type = "float16" if device == "cuda" else "int8"
    cache_path = model_dir / f"models--Systran--faster-whisper-{model_id}" / "refs" / "main"
    local_only = cache_path.exists()
    if device == "cpu" and cpu_threads <= 0:
        # Cap inference threads: uncapped ctranslate2 saturates every core on
        # each segment flush, starving real-time apps (Teams voice drops).
        import os
        cpu_threads = max(2, (os.cpu_count() or 8) // 4)
    return WhisperModel(
        model_id,
        device=device,
        compute_type=compute_type,
        download_root=str(model_dir),
        local_files_only=local_only,
        cpu_threads=cpu_threads if device == "cpu" else 0,
        num_workers=1,
    )


class TranscriptionEngine:
    def __init__(self, model_dir: Path, model_size: str = "large-v3-turbo",
                 cpu_threads: int = 0):
        self._model = _load_model(model_dir, model_size, cpu_threads)
        vad_mic = SileroVAD()
        vad_them = SileroVAD()
        self._mic_transcriber = StreamingTranscriber("you", self._model, vad_mic)
        self._them_transcriber = StreamingTranscriber("them", self._model, vad_them)
        self._task: asyncio.Task | None = None

        # Callbacks set by coordinator — plain synchronous callables
        self.on_utterance: Callable[[Utterance], None] = lambda u: None
        self.on_partial: Callable[[str, str], None] = lambda speaker, text: None

    def _wire(self):
        self._mic_transcriber.on_final = self.on_utterance
        self._them_transcriber.on_final = self.on_utterance
        self._mic_transcriber.on_partial = lambda t: self.on_partial("you", t)
        self._them_transcriber.on_partial = lambda t: self.on_partial("them", t)

    async def start(self, mic_stream, system_stream) -> None:
        self._wire()
        self._task = asyncio.ensure_future(asyncio.gather(
            self._mic_transcriber.process(mic_stream),
            self._them_transcriber.process(system_stream),
        ))
        await self._task

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
