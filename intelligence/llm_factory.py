"""Single place that turns settings.llm_provider into an async complete() fn,
so the coordinator, offline processor, and recovery all agree on the backend.
Embeddings stay separate (Claude can't embed — see _embed paths / Ollama)."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def make_llm_complete(settings):
    """Return an async complete(messages)->str for the configured provider, or None."""
    provider = settings.llm_provider
    if provider == "claude_cli":
        from intelligence.clients.claude_cli import ClaudeCLIClient
        client = ClaudeCLIClient(getattr(settings, "claude_bin", "") or None)
        if not client.available:
            logger.warning("claude_cli selected but the claude CLI was not found")
            return None
        return client.complete
    if provider == "ollama":
        from intelligence.clients.ollama import OllamaClient
        return OllamaClient(settings.ollama_base_url, settings.ollama_llm_model,
                            settings.ollama_embedding_model).complete
    if provider == "openrouter":
        key = settings.get_secret("openrouter_api_key") or ""
        if not key:
            return None
        from intelligence.clients.openrouter import OpenRouterClient
        return OpenRouterClient(api_key=key, model=settings.llm_model).complete
    if provider == "custom":
        from intelligence.clients.openrouter import OpenRouterClient
        return OpenRouterClient(api_key="", base_url=settings.custom_base_url,
                                model=settings.custom_llm_model).complete
    return None
