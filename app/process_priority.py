"""Lower our process priority while a session is recording so real-time apps
(Teams/Zoom voice encoding) always win the CPU over our Whisper inference
bursts. Windows-only; silently a no-op elsewhere or on failure."""
from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)

_BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
_NORMAL_PRIORITY_CLASS = 0x00000020


def _set_priority(cls: int) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        # Explicit signatures: the pseudo-handle from GetCurrentProcess is a
        # HANDLE (-1); letting ctypes default to c_int truncates it on x64.
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        ok = k32.SetPriorityClass(k32.GetCurrentProcess(), cls)
        return bool(ok)
    except Exception as exc:
        logger.info("SetPriorityClass failed: %s", exc)
        return False


def lower_for_recording() -> None:
    if _set_priority(_BELOW_NORMAL_PRIORITY_CLASS):
        logger.info("process priority: below-normal (recording)")


def restore_normal() -> None:
    if _set_priority(_NORMAL_PRIORITY_CLASS):
        logger.info("process priority: normal")
