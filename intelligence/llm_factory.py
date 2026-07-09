"""Single place that turns settings.llm_provider into an async complete() fn,
so the coordinator, offline processor, and recovery all agree on the backend.
Embeddings stay separate (Claude can't embed — see _embed paths / Ollama)."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _ollama_complete(settings):
    from intelligence.clients.ollama import OllamaClient
    return OllamaClient(settings.ollama_base_url, settings.ollama_llm_model,
                        settings.ollama_embedding_model).complete


def _base_complete(settings, provider):
    """The raw complete() for one provider, or None if unconfigured."""
    if provider == "claude_cli":
        from intelligence.clients.claude_cli import ClaudeCLIClient
        client = ClaudeCLIClient(getattr(settings, "claude_bin", "") or None)
        if not client.available:
            logger.warning("claude_cli selected but the claude CLI was not found")
            return None
        return client.complete
    if provider == "ollama":
        return _ollama_complete(settings)
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


def make_llm_complete(settings, on_fallback=None):
    """Return an async complete(messages)->str for the configured provider.

    If the provider is a remote/CLI one (claude_cli) it is wrapped so that ANY
    failure — not logged in, timeout, crash — transparently falls back to local
    Ollama. A meeting must never be lost just because Claude is unavailable.
    `on_fallback(reason)` is called once per fallback (for a UI toast).
    """
    provider = settings.llm_provider
    primary = _base_complete(settings, provider)

    # Only claude_cli / remote providers get the Ollama safety net; if the user
    # explicitly chose ollama there's nothing to fall back to.
    if provider == "claude_cli":
        fallback = _ollama_complete(settings)
        if primary is None:
            logger.warning("claude unavailable at startup — using local Ollama")
            if on_fallback:
                on_fallback("Claude CLI not found — using local model")
            return fallback

        async def _resilient(messages, stream=False):
            try:
                return await primary(messages, stream=stream)
            except Exception as exc:
                logger.warning("Claude failed (%s) — falling back to local Ollama", exc)
                if on_fallback:
                    reason = "Claude logged out" if "logged" in str(exc).lower() or "login" in str(exc).lower() \
                        else "Claude unavailable"
                    on_fallback(f"{reason} — used local model")
                return await fallback(messages, stream=stream)
        return _resilient

    return primary
