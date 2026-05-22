"""Pick the right typing backend for the current platform."""
from __future__ import annotations

import sys

from .base import TypingBackend


def get_backend() -> TypingBackend:
    if sys.platform.startswith("linux"):
        from .linux import LinuxBackend
        return LinuxBackend()
    if sys.platform == "darwin":
        from .macos import MacOSBackend
        return MacOSBackend()
    if sys.platform == "win32":
        from .windows import WindowsBackend
        return WindowsBackend()
    raise RuntimeError(f"Unsupported platform: {sys.platform}")


__all__ = ["get_backend", "TypingBackend"]
