"""First-run / every-run prereq detection for the local AI stack.

The PyInstaller bundle can't ship Ollama or MemPalace (separate Windows-level
installs / different Python env), so on startup we detect missing pieces and
show a friendly dialog with copy-paste commands.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

import httpx


@dataclass
class PrereqStatus:
    ollama_running: bool
    ollama_models: list[str]
    llm_model_present: bool
    embed_model_present: bool
    mempalace_installed: bool

    @property
    def all_ok(self) -> bool:
        return (self.ollama_running and self.llm_model_present
                and self.embed_model_present and self.mempalace_installed)


def check_prereqs(ollama_url: str, llm_model: str, embed_model: str,
                  mempalace_exe: str = "mempalace") -> PrereqStatus:
    """Probe Ollama + MemPalace without raising. Quick (~200ms when things are OK)."""
    ollama_running = False
    models: list[str] = []
    try:
        r = httpx.get(f"{ollama_url.rstrip('/')}/api/tags", timeout=2.0)
        if r.status_code == 200:
            ollama_running = True
            models = [m.get("name", "") for m in r.json().get("models", [])]
    except Exception:
        pass

    def _have(name: str) -> bool:
        # `ollama list` reports "qwen2.5:3b"; tags endpoint returns "qwen2.5:3b" too
        if not name:
            return True
        return any(m == name or m.startswith(name + ":") or m == name + ":latest"
                   for m in models)

    return PrereqStatus(
        ollama_running=ollama_running,
        ollama_models=models,
        llm_model_present=_have(llm_model),
        embed_model_present=_have(embed_model),
        mempalace_installed=_mempalace_available(mempalace_exe),
    )


def _mempalace_available(exe: str) -> bool:
    """Resolve and probe the mempalace CLI without spawning a long process."""
    if shutil.which(exe) is None:
        return False
    try:
        # Extend os.environ — overwriting it strips PATH and the CLI can't find its own python.
        r = subprocess.run([exe, "--version"], capture_output=True,
                           text=True, timeout=5,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        return r.returncode == 0
    except Exception:
        return False


def build_message(status: PrereqStatus, llm_model: str, embed_model: str) -> str:
    """Markdown-ish bulleted message for the dialog."""
    lines = []
    if not status.ollama_running:
        lines.append(
            "Ollama isn't running. Install it from https://ollama.com/download, "
            "then it auto-starts as a tray app."
        )
    elif not status.llm_model_present or not status.embed_model_present:
        lines.append("Open a PowerShell and pull the missing local models:")
        if not status.llm_model_present:
            lines.append(f"    ollama pull {llm_model}")
        if not status.embed_model_present:
            lines.append(f"    ollama pull {embed_model}")
    if not status.mempalace_installed:
        lines.append("MemPalace isn't on PATH. Install it once with:")
        lines.append("    pip install mempalace")
    if not lines:
        return "All prerequisites detected."
    return "\n".join(lines)
