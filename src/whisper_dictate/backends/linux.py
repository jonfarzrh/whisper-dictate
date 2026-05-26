"""Linux backend: prefers ydotool (works on Wayland + X11), falls back to xdotool.

Typing non-ASCII text (i.e. any translation that isn't plain English) is the
tricky part: ydotool's `type` synthesises key *codes* from the active keyboard
layout, so accented Latin characters get dropped and non-Latin scripts (Korean,
Japanese, Arabic, …) produce nothing at all. To type arbitrary Unicode reliably
we instead put the text on the clipboard and paste it (Ctrl+V). Plain-ASCII text
still goes through fast keystroke typing, so English dictation is unchanged and
doesn't touch the clipboard.
"""
from __future__ import annotations

import os
import shutil
import subprocess

from .base import TypingBackend

# ydotool's `key` syntax changed incompatibly between releases:
#   * 0.1.x  expects key *names* joined with '+', e.g.  `ctrl+v`
#   * 1.0+   expects <keycode>:<state> pairs, e.g.       `29:1 47:1 47:0 29:0`
# Feeding one syntax to the other version doesn't error — it types garbage (the
# 1.0 codes 29/47 land as the digits "2442" on 0.1.x). So we detect which the
# installed binary wants by inspecting its help text, and cache the answer.
_PASTE_KEYS_NAME = ["ctrl+v"]                       # ydotool 0.1.x
_PASTE_KEYS_CODE = ["29:1", "47:1", "47:0", "29:0"]  # ydotool 1.0+ (29=LEFTCTRL, 47=V)
_paste_keys_cache: list[str] | None = None


def _ydotool_paste_keys() -> list[str]:
    """The argv tail for a Ctrl+V via `ydotool key`, matching the installed
    version's syntax. 0.1.x help describes sequences 'separated by plus (+)';
    1.0+ does not — so the presence of that phrasing selects the name syntax."""
    global _paste_keys_cache
    if _paste_keys_cache is None:
        try:
            r = subprocess.run(["ydotool", "key", "--help"],
                               capture_output=True, text=True)
            help_txt = (r.stdout + r.stderr).lower()
        except Exception:  # noqa: BLE001 - if we can't probe, assume the legacy syntax
            help_txt = "plus"
        _paste_keys_cache = _PASTE_KEYS_NAME if "plus" in help_txt else _PASTE_KEYS_CODE
    return _paste_keys_cache


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

    def _ydotool_socket(self) -> str | None:
        """Resolve the ydotoold socket. The hotkey-launched process may not have
        YDOTOOL_SOCKET in its environment, so probe the locations ydotoold uses
        across versions and return the first that exists."""
        candidates = [
            os.environ.get("YDOTOOL_SOCKET"),
            "/tmp/.ydotool_socket",
            os.path.join(os.path.expanduser("~"), ".ydotool_socket"),
        ]
        xrd = os.environ.get("XDG_RUNTIME_DIR")
        if xrd:
            candidates.append(os.path.join(xrd, ".ydotool_socket"))
        for c in candidates:
            if c and os.path.exists(c):
                return c
        return None

    def _ydotool_env(self) -> dict:
        env = {**os.environ}
        sock = self._ydotool_socket()
        if sock:
            env["YDOTOOL_SOCKET"] = sock
        return env

    def _key_delay_ms(self) -> int:
        """Inter-keystroke delay (ms) from saved settings, clamped to a sane range.
        Too small (<3ms) and slow consumers like JetBrains/Electron terminals drop
        keys; too large and dictation feels sluggish."""
        from whisper_dictate.config import DEFAULTS, load_config
        try:
            v = int(load_config().get("type_key_delay_ms", DEFAULTS["type_key_delay_ms"]))
        except (TypeError, ValueError):
            v = int(DEFAULTS["type_key_delay_ms"])  # type: ignore[arg-type]
        return max(1, min(v, 1000))

    def type_text(self, text: str) -> None:
        tool = self._tool()
        if tool is None:
            raise RuntimeError(
                "Neither ydotool nor xdotool found. "
                "Install with: sudo apt install ydotool"
            )
        delay = str(self._key_delay_ms())
        if tool == "ydotool":
            # ydotool can't synthesise non-ASCII characters; paste those instead.
            if text.isascii():
                subprocess.run(
                    ["ydotool", "type", "--key-delay", delay, "--", text],
                    check=True, env=self._ydotool_env(),
                )
            else:
                self._paste_ydotool(text)
        else:
            # xdotool handles Unicode itself, so a plain type is fine on X11.
            subprocess.run(
                ["xdotool", "type", "--delay", delay, "--", text],
                check=True,
            )

    def _paste_ydotool(self, text: str) -> None:
        """Type arbitrary Unicode by routing it through the clipboard: copy with
        wl-copy, then paste with a synthesised Ctrl+V via ydotool. This replaces
        the current clipboard contents (a deliberate, visible side effect)."""
        if not shutil.which("wl-copy"):
            raise RuntimeError(
                "Typing translated/Unicode text on Wayland needs wl-clipboard "
                "(ydotool can't type non-ASCII characters). Install it:\n"
                "  sudo apt install wl-clipboard"
            )
        subprocess.run(["wl-copy"], input=text, text=True, check=True)
        subprocess.run(
            ["ydotool", "key", *_ydotool_paste_keys()],
            check=True, env=self._ydotool_env(),
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
            notes = []
            if self._ydotool_socket() is None:
                notes.append(
                    "ydotoold socket not found — start the daemon (the ydotoold "
                    "user service) or set YDOTOOL_SOCKET."
                )
            if not shutil.which("wl-copy"):
                notes.append(
                    "wl-clipboard missing — translated/Unicode text can't be typed "
                    "until you install it: sudo apt install wl-clipboard"
                )
            if notes:
                return True, "ydotool found, but:\n  - " + "\n  - ".join(notes)

        return True, f"OK ({tool} on {session})"
