"""Persistent user settings.

Dictation is triggered by a short-lived CLI process bound to a hotkey, so there's
nowhere to "hold" preferences in memory between presses — they live in a JSON file
that every invocation reads. The settings GUI (and, later, a tray app) edit this
file; the CLI loads it as the *defaults* for its flags, so an explicit `--flag`
on the command line still wins over the saved value.

This is distinct from `recorder._state_dir()`, which is transient runtime state
(`/tmp`-ish, wiped on reboot). Config must survive reboots, so it goes in the
platform's real config location.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from whisper_dictate.polish import DEFAULT_MODEL as DEFAULT_OLLAMA_MODEL

# The full settings schema with its defaults. Keys map 1:1 to CLI flag dests, so
# the CLI can splat saved values straight in as argparse defaults. Empty string
# means "unset / use built-in default" for the optional fields (translate_to,
# style, ollama_host) and "auto-detect" for `language`.
DEFAULTS: dict[str, object] = {
    "engine": "auto",
    "model": "large-v3",
    "device": "auto",
    "compute_type": "auto",
    "language": "en",        # "" -> auto-detect
    "vad": False,
    "input_device": "",      # "" -> system default mic; otherwise sounddevice device name

    "translate_to": "",      # "" -> no translation
    "style": "",             # "" -> no restyle
    "ollama_model": DEFAULT_OLLAMA_MODEL,
    "ollama_host": "",       # "" -> $OLLAMA_HOST or http://localhost:11434
}


def config_dir() -> Path:
    """Persistent per-user config directory, created if missing.

    Linux: $XDG_CONFIG_HOME or ~/.config; macOS: ~/Library/Application Support;
    Windows: %APPDATA%."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / "whisper-dictate"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_file() -> Path:
    return config_dir() / "config.json"


def load_config() -> dict:
    """Return saved settings merged over DEFAULTS. Unknown keys are dropped and
    missing keys fall back to their default, so an old or partial file still
    yields a complete, valid config. A malformed file degrades to defaults."""
    cfg = dict(DEFAULTS)
    path = config_file()
    if path.exists():
        try:
            saved = json.loads(path.read_text())
            if isinstance(saved, dict):
                cfg.update({k: saved[k] for k in DEFAULTS if k in saved})
        except (ValueError, OSError):
            pass  # corrupt/unreadable -> defaults
    return cfg


def save_config(values: dict) -> Path:
    """Write settings to disk, keeping only known keys. Returns the path written."""
    cfg = {k: values[k] for k in DEFAULTS if k in values}
    path = config_file()
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    return path
