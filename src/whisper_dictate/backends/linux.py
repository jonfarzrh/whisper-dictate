"""Linux backend: prefers ydotool (works on Wayland + X11), falls back to xdotool."""
from __future__ import annotations

import os
import shutil
import subprocess

from .base import TypingBackend


class LinuxBackend(TypingBackend):
    @property
    def name(self) -> str:
        return f"linux:{self._tool()}"

    def _tool(self) -> str | None:
        if shutil.which("ydotool"):
            return "ydotool"
        if shutil.which("xdotool"):
            return "xdotool"
        return None

    def _session_type(self) -> str:
        return os.environ.get("XDG_SESSION_TYPE", "unknown")

    def type_text(self, text: str) -> None:
        tool = self._tool()
        if tool is None:
            raise RuntimeError(
                "Neither ydotool nor xdotool found. "
                "Install with: sudo apt install ydotool"
            )
        if tool == "ydotool":
            subprocess.run(
                ["ydotool", "type", "--key-delay", "3", "--", text],
                check=True,
                env={**os.environ},
            )
        else:
            subprocess.run(
                ["xdotool", "type", "--delay", "3", "--", text],
                check=True,
            )

    def check(self) -> tuple[bool, str]:
        session = self._session_type()
        tool = self._tool()

        if tool is None:
            return False, (
                "No input tool installed. On Wayland (e.g. COSMIC, GNOME 4x, KDE 6) "
                "install ydotool:\n"
                "  sudo apt install ydotool\n"
                "Then enable the daemon: see README for systemd setup."
            )

        if tool == "xdotool" and session == "wayland":
            return False, (
                "xdotool is installed but you're on Wayland — it won't work in "
                "native Wayland apps. Install ydotool: sudo apt install ydotool"
            )

        if tool == "ydotool":
            socket = os.environ.get("YDOTOOL_SOCKET")
            if not socket:
                return True, (
                    "ydotool found, but YDOTOOL_SOCKET is not set. If typing fails, "
                    "export YDOTOOL_SOCKET=$HOME/.ydotool_socket and ensure the "
                    "ydotoold daemon is running."
                )

        return True, f"OK ({tool} on {session})"
