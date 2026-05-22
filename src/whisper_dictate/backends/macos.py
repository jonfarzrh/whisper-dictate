"""macOS backend using pynput. Requires Accessibility permission."""
from __future__ import annotations

from .base import TypingBackend


class MacOSBackend(TypingBackend):
    @property
    def name(self) -> str:
        return "macos:pynput"

    def type_text(self, text: str) -> None:
        try:
            from pynput.keyboard import Controller
        except ImportError as e:
            raise RuntimeError(
                "pynput not installed. Reinstall with the macos extra:\n"
                "  uv tool install 'whisper-dictate[macos]'"
            ) from e
        Controller().type(text)

    def check(self) -> tuple[bool, str]:
        try:
            import pynput  # noqa: F401
        except ImportError:
            return False, (
                "pynput missing. Install with: uv tool install 'whisper-dictate[macos]'"
            )
        return True, (
            "OK. On first use, grant the terminal (or whichever app launches "
            "whisper-dictate) Accessibility permission in "
            "System Settings → Privacy & Security → Accessibility."
        )
