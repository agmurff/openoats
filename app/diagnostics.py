"""Capture-everything diagnostics for installed builds.

When PyInstaller bundles a windowed app (`console=False`), stdout/stderr go
nowhere by default and unhandled exceptions in qasync tasks die silently.
Calling `install()` early in main wires:
  - sys.excepthook       — uncaught exceptions on the main thread
  - threading.excepthook — uncaught exceptions on worker threads
  - asyncio loop handler — unhandled task / future exceptions
  - stderr               — redirected to the log file
so the next crash leaves a full traceback in openoats.log.
"""
from __future__ import annotations

import asyncio
import faulthandler
import logging
import sys
import threading
from pathlib import Path

logger = logging.getLogger("openoats.diagnostics")

# Hold onto the faulthandler stream so it isn't GC'd while the process is alive.
_FAULT_STREAM = None


def install(log_path: Path) -> None:
    # 0. Native-code crash handler — the previous crash left no Python traceback
    # because faster-whisper / ctranslate2 / Qt audio code can SIGSEGV/abort
    # without raising a Python exception. faulthandler writes the C-level stack
    # of every thread to this file before the process dies.
    global _FAULT_STREAM
    try:
        _FAULT_STREAM = open(str(log_path), "a", buffering=1, encoding="utf-8", errors="replace")
        faulthandler.enable(file=_FAULT_STREAM, all_threads=True)
    except OSError as exc:
        # Fall back to default (stderr) if file open fails
        logger.warning("faulthandler file open failed: %s", exc)
        faulthandler.enable(all_threads=True)
    # 1. Main-thread excepthook
    def _excepthook(exc_type, exc, tb):
        logger.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
    sys.excepthook = _excepthook

    # 2. Worker-thread excepthook
    def _thread_excepthook(args):
        logger.critical(
            "Unhandled exception in thread %s",
            args.thread.name if args.thread else "<unknown>",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
    threading.excepthook = _thread_excepthook

    # 3. asyncio default exception handler — wired once the loop is created
    def _asyncio_handler(loop, context):
        exc = context.get("exception")
        msg = context.get("message", "unhandled asyncio error")
        if exc:
            logger.critical("asyncio: %s", msg, exc_info=exc)
        else:
            logger.critical("asyncio: %s | context=%s", msg, context)

    # Apply to whatever loop the caller has set
    try:
        loop = asyncio.get_event_loop()
        loop.set_exception_handler(_asyncio_handler)
    except RuntimeError:
        pass

    # 4. Redirect stderr to the log file so native-code crashes leave breadcrumbs.
    # Use a line-buffered append handle alongside the existing log handler.
    try:
        sys.stderr = open(str(log_path), "a", buffering=1, encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Could not redirect stderr to log: %s", exc)

    logger.info("diagnostics: installed")


def attach_to_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Attach the asyncio handler to a specific loop (call after qasync sets it)."""
    def _h(_l, context):
        exc = context.get("exception")
        msg = context.get("message", "unhandled asyncio error")
        if exc:
            logger.critical("asyncio: %s", msg, exc_info=exc)
        else:
            logger.critical("asyncio: %s | context=%s", msg, context)
    loop.set_exception_handler(_h)
