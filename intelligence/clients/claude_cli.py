"""LLM backend that shells out to the Claude Code CLI (`claude -p`).

Reuses the user's existing Claude Code login — no API key, no separate billing.
Prompts are fed via stdin (command-line length limits make args unsafe for full
transcripts). Runs in a neutral temp cwd so it doesn't pick up a stray CLAUDE.md,
and with CREATE_NO_WINDOW so the windowed app never flashes a console.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

from intelligence.clients.base import BaseLLMClient, LLMUnavailableError

logger = logging.getLogger(__name__)

_CREATE_NO_WINDOW = 0x08000000  # Windows: don't pop a console for the child


def _resolve_claude(explicit: str | None) -> str | None:
    candidates = []
    if explicit:
        candidates.append(explicit)
    candidates.append("claude")
    home = Path.home()
    for name in ("claude.exe", "claude.cmd", "claude"):
        candidates.append(str(home / ".local" / "bin" / name))
    for c in candidates:
        if os.path.isabs(c) and os.path.exists(c):
            return c
        found = shutil.which(c)
        if found:
            return found
    return None


def _flatten(messages: list[dict]) -> str:
    """OpenAI-style messages -> a single prompt for `claude -p` over stdin."""
    parts = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            parts.append(f"<instructions>\n{content}\n</instructions>")
        elif role == "assistant":
            parts.append(f"<previous_assistant_reply>\n{content}\n</previous_assistant_reply>")
        else:
            parts.append(content)
    return "\n\n".join(parts)


class ClaudeCLIClient(BaseLLMClient):
    def __init__(self, claude_bin: str | None = None, model: str | None = None,
                 timeout: float = 600.0):
        self._bin = _resolve_claude(claude_bin)
        self._model = model or None
        self._timeout = timeout

    @property
    def available(self) -> bool:
        return self._bin is not None

    async def complete(self, messages: list[dict], stream: bool = False) -> str:
        if not self._bin:
            raise LLMUnavailableError(
                "Claude CLI not found. Install Claude Code and run `claude` once to log in."
            )
        prompt = _flatten(messages)

        cmd = [self._bin, "-p", "--output-format", "text"]
        if self._model:
            cmd += ["--model", self._model]

        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = _CREATE_NO_WINDOW

        # Neutral cwd so the CLI doesn't load some unrelated project's CLAUDE.md.
        with tempfile.TemporaryDirectory(prefix="openoats-claude-") as cwd:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    **kwargs,
                )
            except FileNotFoundError:
                raise LLMUnavailableError(f"Claude CLI not runnable at {self._bin}")

            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(prompt.encode("utf-8")), timeout=self._timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                raise LLMUnavailableError(f"Claude CLI timed out after {self._timeout:.0f}s")

        if proc.returncode != 0:
            msg = (err or b"").decode("utf-8", "replace")[:300]
            raise LLMUnavailableError(f"Claude CLI exit {proc.returncode}: {msg}")
        return (out or b"").decode("utf-8", "replace").strip()
