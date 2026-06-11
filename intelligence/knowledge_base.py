import asyncio
import hashlib
import json
import logging
import os
import numpy as np
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Awaitable

SUPPORTED_EXTENSIONS = {".md", ".txt"}

logger = logging.getLogger(__name__)


def _cache_dir_for(folder: Path) -> Path:
    """Cache lives under %LOCALAPPDATA% (always user-writable) keyed by the
    KB folder's absolute path hash. Storing it inside the KB folder broke
    silently when kb_folder pointed at a read-only location (Program Files)."""
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "OpenOats" / "kb_cache"
    digest = hashlib.md5(str(folder.resolve()).encode("utf-8")).hexdigest()[:12]
    safe_name = folder.name or "root"
    return base / f"{safe_name}-{digest}"


@dataclass
class KBResult:
    text: str
    source_file: str
    header_context: str
    relevance_score: float


def chunk_text(text: str, max_chars: int = 500) -> list[str]:
    sentences = text.replace("\n", " ").split(". ")
    chunks, current = [], ""
    for s in sentences:
        if len(current) + len(s) > max_chars and current:
            chunks.append(current.strip())
            current = s + ". "
        else:
            current += s + ". "
    if current.strip():
        chunks.append(current.strip())
    # Tables (e.g. converted spreadsheets) have no '. ' separators, so a whole
    # sheet can land in one chunk that blows past the embedding model's context
    # window and 500s. Hard-split anything oversize.
    hard_max = max_chars * 3
    final: list[str] = []
    for c in chunks or [text[:max_chars]]:
        if len(c) <= hard_max:
            final.append(c)
        else:
            final.extend(c[i:i + hard_max] for i in range(0, len(c), hard_max))
    return final


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


class KnowledgeBase:
    def __init__(self, folder: Path, embed_fn: Callable[[list[str]], Awaitable[list[list[float]]]]):
        self._folder = folder
        self._embed = embed_fn
        self._cache_dir = _cache_dir_for(folder)
        logger.info("KB folder=%s cache=%s", folder, self._cache_dir)
        self._chunks: list[dict] = []
        self._embeddings: np.ndarray | None = None

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)

    async def index(self, progress_cb=None) -> None:
        """Index the knowledge base folder. Blocking I/O is offloaded to a thread."""
        # parents=True — cache lives under %LOCALAPPDATA% now, may not exist yet
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = self._cache_dir / "manifest.json"
        emb_path = self._cache_dir / "embeddings.npy"

        def _load_cache():
            existing = {}
            cached_emb_by_hash: dict[str, list[float]] = {}
            if manifest_path.exists() and emb_path.exists():
                try:
                    stored_embs = np.load(str(emb_path))
                    idx = 0
                    for entry in json.loads(manifest_path.read_text()):
                        for chunk_entry in entry["chunks"]:
                            h = chunk_entry.get("hash", "")
                            if idx < len(stored_embs):
                                cached_emb_by_hash[h] = stored_embs[idx].tolist()
                            idx += 1
                        existing[entry["file"]] = entry
                except Exception:
                    pass
            return existing, cached_emb_by_hash

        existing, cached_emb_by_hash = await asyncio.to_thread(_load_cache)

        files = await asyncio.to_thread(
            lambda: [f for f in self._folder.rglob("*") if f.suffix in SUPPORTED_EXTENSIONS]
        )
        new_manifest, all_chunks = [], []
        failed = 0

        def _save_cache():
            if all_chunks:
                embs = np.array([c["embedding"] for c in all_chunks], dtype=np.float32)
                np.save(str(emb_path), embs)
            manifest_path.write_text(json.dumps(new_manifest, indent=2))

        for i, f in enumerate(files):
            if progress_cb:
                progress_cb(i + 1, len(files))
            try:
                mtime = str(f.stat().st_mtime)
                key = str(f)

                # Fast path: unchanged file with fully-cached embeddings —
                # rebuild from the manifest without reading or re-chunking it.
                if key in existing and existing[key]["mtime"] == mtime:
                    entry = existing[key]
                    cached = [cached_emb_by_hash.get(c.get("hash", "")) for c in entry["chunks"]]
                    if entry["chunks"] and None not in cached:
                        for c, e in zip(entry["chunks"], cached):
                            all_chunks.append({**c, "embedding": e})
                        new_manifest.append(entry)
                        continue

                text = await asyncio.to_thread(f.read_text, errors="ignore")
                header = ""
                chunks = [c for c in chunk_text(text) if c.strip()]
                if not chunks:
                    continue
                chunk_hashes = [hashlib.md5(c.encode()).hexdigest() for c in chunks]
                embeddings = await self._embed(chunks)
            except Exception as exc:
                # One bad file (or a transient Ollama error that survived retries)
                # must not kill the whole index run — skip it; absent from the
                # manifest, it will be retried on the next index.
                failed += 1
                logger.warning("KB index: skipped %s (%s)", f.name, exc)
                continue

            file_chunks = []
            for chunk, emb, h in zip(chunks, embeddings, chunk_hashes):
                chunk_entry = {"text": chunk, "file": f.name, "header": header,
                               "hash": h, "embedding": emb}
                all_chunks.append(chunk_entry)
                file_chunks.append({"text": chunk, "file": f.name, "header": header, "hash": h})
            new_manifest.append({"file": key, "mtime": mtime, "chunks": file_chunks})

            # Persist progress every 10 files so an interrupted first index
            # resumes from the cache instead of starting over.
            if (i + 1) % 10 == 0:
                await asyncio.to_thread(_save_cache)

        self._chunks = all_chunks
        if all_chunks:
            self._embeddings = np.array(
                [c["embedding"] for c in all_chunks], dtype=np.float32
            )
        await asyncio.to_thread(_save_cache)
        logger.info("KB index complete: %d files, %d chunks, %d skipped",
                    len(new_manifest), len(all_chunks), failed)

    async def search(self, query: str, top_k: int = 5) -> list[KBResult]:
        if self._embeddings is None or len(self._chunks) == 0:
            return []
        q_emb_list = await self._embed([query])
        q_emb = np.array(q_emb_list[0], dtype=np.float32)
        scores = np.array([
            cosine_similarity(q_emb, np.array(c.get("embedding", q_emb)))
            for c in self._chunks
        ])
        top_indices = np.argsort(scores)[::-1][:top_k]
        return [
            KBResult(
                text=self._chunks[i]["text"],
                source_file=self._chunks[i].get("file", ""),
                header_context=self._chunks[i].get("header", ""),
                relevance_score=float(scores[i]),
            )
            for i in top_indices
        ]
