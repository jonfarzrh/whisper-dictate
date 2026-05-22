"""Windows backend using pynput."""
from __future__ import annotations

from .base import TypingBackend


class WindowsBackend(TypingBackend):
    @property
    def name(self) -> str:
        return "windows:pynput"

    def type_text(self, text: str) -> None:
        try:
            from pynput.keyboard import Controller
        except ImportError as e:
            raise RuntimeError(
                "pynput not installed. Reinstall with the windows extra:\n"
                "  uv tool install 'whisper-dictate[windows]'"
            ) from e
        Controller().type(text)

    def check(self) -> tuple[bool, str]:
        try:
            import pynput  # noqa: F401
        except ImportError:
            return False, (
                "pynput missing. Install with: "
                "uv tool install 'whisper-dictate[windows]'"
            )
        return True, "OK"
