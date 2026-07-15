import httpx
from intelligence.clients.base import BaseLLMClient, BaseEmbeddingClient, LLMUnavailableError


class OllamaClient(BaseLLMClient, BaseEmbeddingClient):
    def __init__(self, base_url: str, llm_model: str, embedding_model: str):
        self._base = base_url.rstrip("/")
        self._llm = llm_model
        self._emb = embedding_model

    async def complete(self, messages: list[dict], stream: bool = False) -> str:
        payload = {
            "model": self._llm,
            "messages": messages,
            "stream": False,
            # CRITICAL: Ollama defaults to a 2048-token context, which silently
            # truncates the transcript to the last ~1500 words — the model never
            # sees most of the meeting. Give it room to read the whole thing.
            "options": {"num_ctx": 16384},
        }
        # 10 min — long-meeting notes on a small CPU Qwen can easily run past 60s.
        async with httpx.AsyncClient(timeout=600) as client:
            try:
                resp = await client.post(f"{self._base}/api/chat", json=payload)
            except httpx.ConnectError:
                raise LLMUnavailableError(f"Ollama not running at {self._base}")
            if not (200 <= resp.status_code < 300):
                raise LLMUnavailableError(f"Ollama HTTP {resp.status_code}")
            return resp.json()["message"]["content"]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import asyncio
        # Ollama chokes on empty input; keep list length stable for the caller.
        safe = [t if t and t.strip() else " " for t in texts]
        async with httpx.AsyncClient(timeout=120) as client:
            # Batch endpoint first (Ollama >= 0.1.45): one request per file
            # instead of one per chunk — far fewer chances to hit a transient 500.
            try:
                resp = await client.post(
                    f"{self._base}/api/embed",
                    json={"model": self._emb, "input": safe},
                )
                if 200 <= resp.status_code < 300:
                    embs = resp.json().get("embeddings") or []
                    if len(embs) == len(safe):
                        return embs
            except httpx.ConnectError:
                raise LLMUnavailableError(f"Ollama not running at {self._base}")
            except httpx.HTTPError:
                pass  # fall back to per-text

            results = []
            for text in safe:
                last_err = None
                for attempt in range(3):  # transient 500s happen under rapid fire
                    try:
                        resp = await client.post(
                            f"{self._base}/api/embeddings",
                            json={"model": self._emb, "prompt": text},
                        )
                    except httpx.ConnectError:
                        raise LLMUnavailableError(f"Ollama not running at {self._base}")
                    except httpx.HTTPError as exc:
                        last_err = f"{type(exc).__name__}"
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    if 200 <= resp.status_code < 300:
                        results.append(resp.json()["embedding"])
                        break
                    last_err = f"HTTP {resp.status_code}"
                    await asyncio.sleep(0.5 * (attempt + 1))
                else:
                    raise LLMUnavailableError(f"Ollama embed failed after retries: {last_err}")
            return results
