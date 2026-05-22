# whisper-dictate: Build Specification

Build a cross-platform Python package called `whisper-dictate` that provides local Whisper-powered system-wide dictation. Press a hotkey, speak, press again — the transcribed text is typed into whatever application is focused.

## Goals

- **Cross-platform wheel** (`py3-none-any`) that works on Linux (X11 + Wayland), macOS, and Windows.
- **GPU-accelerated** transcription via faster-whisper (CTranslate2).
- **Toggle model**: first invocation starts recording, second invocation stops, transcribes, and types.
- **Installable via `uv tool install`** so the `whisper-dictate` command is on PATH globally.
- **Platform-specific input injection** via a backend dispatcher: ydotool/xdotool on Linux, pynput on macOS/Windows.

## Target environment for the primary user

Pop!_OS 24.04 with COSMIC desktop (Wayland), NVIDIA GPU, 128 GB RAM. The CLI must work cleanly on Wayland — that means ydotool (which uses `/dev/uinput`) rather than xdotool, because Wayland's security model blocks synthetic input from xdotool into native Wayland windows.

## Project structure

Create exactly this layout:

```
whisper-dictate/
├── pyproject.toml
├── README.md
└── src/
    └── whisper_dictate/
        ├── __init__.py
        ├── __main__.py
        ├── cli.py
        ├── recorder.py
        ├── transcriber.py
        └── backends/
            ├── __init__.py
            ├── base.py
            ├── linux.py
            ├── macos.py
            └── windows.py
```

## File contents

### `pyproject.toml`

Uses hatchling. Defines a `whisper-dictate` console script entry point. Platform-specific extras for pynput.

```toml
[project]
name = "whisper-dictate"
version = "0.1.0"
description = "Local Whisper dictation that types into any application"
readme = "README.md"
requires-python = ">=3.11"
license = { text = "MIT" }
dependencies = [
    "faster-whisper>=1.0",
    "sounddevice>=0.4",
    "numpy>=1.24",
    "soundfile>=0.12",
]

[project.optional-dependencies]
macos = ["pynput>=1.7"]
windows = ["pynput>=1.7"]

[project.scripts]
whisper-dictate = "whisper_dictate.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/whisper_dictate"]
```

### `src/whisper_dictate/__init__.py`

```python
"""whisper-dictate: local Whisper-powered system-wide dictation."""
__version__ = "0.1.0"
```

### `src/whisper_dictate/__main__.py`

Allow `python -m whisper_dictate`.

```python
from whisper_dictate.cli import main

if __name__ == "__main__":
    main()
```

### `src/whisper_dictate/recorder.py`

Audio recording with a **toggle model**. Key design: the CLI re-execs itself with a hidden `--record-worker PATH` flag to spawn a detached worker process. The worker's PID is stored in a state file. A second invocation reads the PID, sends SIGTERM, and the worker flushes the WAV and exits.

This avoids needing a long-running daemon or IPC.

Requirements:
- Cross-platform state directory: `$XDG_RUNTIME_DIR/whisper-dictate/` on Linux/macOS, `%LOCALAPPDATA%\whisper-dictate\` on Windows.
- `is_recording()` checks PID liveness (`os.kill(pid, 0)` on POSIX; `OpenProcess` on Windows).
- `start_recording()` spawns `[sys.executable, "-m", "whisper_dictate", "--record-worker", str(audio_path)]` with `start_new_session=True`, stdin/stdout/stderr to DEVNULL. Writes PID to state file.
- `stop_recording()` reads PID, sends SIGTERM, waits up to 5s for the process to exit, returns path to WAV.
- `run_record_worker(out_path)` is the worker body: installs SIGTERM/SIGINT handlers that set a stop flag, opens a `sounddevice.InputStream` with a callback that appends chunks to a list, sleeps in 50ms increments until stop flag is set, then concatenates with numpy and writes via soundfile at 16 kHz mono PCM_16.

```python
"""Audio recording. Cross-platform via sounddevice + soundfile.

Toggle model: a small worker process records to a WAV file. The PID of that
worker is stored so a second invocation of the CLI can signal it to stop.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

SAMPLE_RATE = 16000
CHANNELS = 1


def _state_dir() -> Path:
    """XDG-ish runtime dir that works on all platforms."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
    d = base / "whisper-dictate"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pid_file() -> Path:
    return _state_dir() / "recorder.pid"


def audio_file() -> Path:
    return _state_dir() / "recording.wav"


def is_recording() -> bool:
    pf = pid_file()
    if not pf.exists():
        return False
    try:
        pid = int(pf.read_text())
        if sys.platform == "win32":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        pf.unlink(missing_ok=True)
        return False


def start_recording() -> int:
    """Spawn a detached worker that records until signaled. Returns PID."""
    af = audio_file()
    af.unlink(missing_ok=True)

    proc = subprocess.Popen(
        [sys.executable, "-m", "whisper_dictate", "--record-worker", str(af)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    pid_file().write_text(str(proc.pid))
    return proc.pid


def stop_recording() -> Path | None:
    """Signal the worker to stop. Returns path to completed WAV file."""
    pf = pid_file()
    if not pf.exists():
        return None
    try:
        pid = int(pf.read_text())
    except ValueError:
        pf.unlink(missing_ok=True)
        return None

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    af = audio_file()
    for _ in range(50):  # up to 5s
        if not _pid_alive(pid):
            break
        time.sleep(0.1)

    pf.unlink(missing_ok=True)
    return af if af.exists() else None


def _pid_alive(pid: int) -> bool:
    try:
        if sys.platform == "win32":
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def run_record_worker(out_path: Path) -> None:
    """Worker entry: record audio until SIGTERM, write WAV, exit."""
    import numpy as np
    import sounddevice as sd
    import soundfile as sf

    chunks: list[np.ndarray] = []
    stop_flag = {"stop": False}

    def handle_stop(signum, frame):
        stop_flag["stop"] = True

    signal.signal(signal.SIGTERM, handle_stop)
    if sys.platform != "win32":
        signal.signal(signal.SIGINT, handle_stop)

    def callback(indata, frames, time_info, status):
        chunks.append(indata.copy())

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="float32",
        callback=callback,
    ):
        while not stop_flag["stop"]:
            sd.sleep(50)

    if chunks:
        audio = np.concatenate(chunks, axis=0)
        sf.write(str(out_path), audio, SAMPLE_RATE, subtype="PCM_16")
```

### `src/whisper_dictate/transcriber.py`

Thin wrapper over faster-whisper. Auto-detects CUDA by attempting to dlopen the CUDA runtime — avoids a torch dependency just for device detection.

```python
"""Transcription via faster-whisper. Lazy-loads the model."""
from __future__ import annotations

from pathlib import Path


def transcribe(
    audio_path: Path,
    model_name: str = "large-v3",
    device: str = "auto",
    compute_type: str = "auto",
    language: str | None = "en",
) -> str:
    from faster_whisper import WhisperModel

    if device == "auto":
        device = _detect_device()
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"

    model = WhisperModel(model_name, device=device, compute_type=compute_type)
    segments, _info = model.transcribe(
        str(audio_path),
        language=language,
        vad_filter=True,
        beam_size=5,
    )
    return "".join(seg.text for seg in segments).strip()


def _detect_device() -> str:
    try:
        import ctypes
        for name in ("libcudart.so.12", "libcudart.so.11", "cudart64_12.dll", "cudart64_110.dll"):
            try:
                ctypes.CDLL(name)
                return "cuda"
            except OSError:
                continue
    except Exception:
        pass
    return "cpu"
```

### `src/whisper_dictate/backends/base.py`

```python
"""Abstract typing backend."""
from __future__ import annotations

from abc import ABC, abstractmethod


class TypingBackend(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def type_text(self, text: str) -> None:
        """Type text into the currently focused application."""

    @abstractmethod
    def check(self) -> tuple[bool, str]:
        """Return (ok, message). If not ok, message explains how to fix it."""
```

### `src/whisper_dictate/backends/__init__.py`

```python
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
```

### `src/whisper_dictate/backends/linux.py`

Prefers ydotool when present (works on both X11 and Wayland), falls back to xdotool. The `check()` method specifically warns when xdotool is the only option on a Wayland session, because that combo silently fails in native Wayland windows.

```python
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
```

### `src/whisper_dictate/backends/macos.py`

```python
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
```

### `src/whisper_dictate/backends/windows.py`

```python
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
```

### `src/whisper_dictate/cli.py`

The argparse-based CLI. Subcommands: `toggle` (default), `start`, `stop`, `check`, `transcribe FILE`. Hidden `--record-worker PATH` flag triggers the worker mode used by `recorder.start_recording()`. If no subcommand is given, defaults to `toggle`.

Best-effort desktop notifications via `notify-send` when available, else stderr.

```python
"""CLI for whisper-dictate."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from whisper_dictate import __version__
from whisper_dictate.backends import get_backend
from whisper_dictate.recorder import (
    audio_file,
    is_recording,
    pid_file,
    run_record_worker,
    start_recording,
    stop_recording,
)


def _notify(msg: str) -> None:
    """Best-effort desktop notification."""
    if shutil.which("notify-send"):
        subprocess.run(
            ["notify-send", "-t", "1500", "whisper-dictate", msg],
            check=False,
        )
    else:
        print(msg, file=sys.stderr)


def cmd_toggle(args: argparse.Namespace) -> int:
    """Start recording if idle; stop + transcribe + type if recording."""
    if is_recording():
        _notify("⏳ Transcribing...")
        wav = stop_recording()
        if wav is None or not wav.exists():
            _notify("❌ No audio captured")
            return 1

        from whisper_dictate.transcriber import transcribe
        text = transcribe(
            wav,
            model_name=args.model,
            device=args.device,
            compute_type=args.compute_type,
            language=args.language,
        )

        if not text:
            _notify("❌ No speech detected")
            return 1

        backend = get_backend()
        backend.type_text(text)
        _notify(f"✓ Typed {len(text)} chars")
        return 0
    else:
        start_recording()
        _notify("🎙️ Recording... (run again to stop)")
        return 0


def cmd_start(args: argparse.Namespace) -> int:
    if is_recording():
        print("Already recording.", file=sys.stderr)
        return 1
    start_recording()
    _notify("🎙️ Recording...")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    if not is_recording():
        print("Not recording.", file=sys.stderr)
        return 1
    _notify("⏳ Transcribing...")
    wav = stop_recording()
    if wav is None or not wav.exists():
        _notify("❌ No audio")
        return 1

    from whisper_dictate.transcriber import transcribe
    text = transcribe(
        wav,
        model_name=args.model,
        device=args.device,
        compute_type=args.compute_type,
        language=args.language,
    )
    if not text:
        _notify("❌ No speech detected")
        return 1

    backend = get_backend()
    backend.type_text(text)
    _notify(f"✓ Typed {len(text)} chars")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    backend = get_backend()
    ok, msg = backend.check()
    print(f"Backend: {backend.name}")
    print(f"Status:  {'OK' if ok else 'NOT OK'}")
    print(msg)
    return 0 if ok else 1


def cmd_transcribe_file(args: argparse.Namespace) -> int:
    """Transcribe an existing audio file (for testing without recording)."""
    from whisper_dictate.transcriber import transcribe
    text = transcribe(
        Path(args.path),
        model_name=args.model,
        device=args.device,
        compute_type=args.compute_type,
        language=args.language,
    )
    print(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="whisper-dictate", description=__doc__)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    # Hidden flag used internally when the CLI re-execs itself as a recorder worker
    p.add_argument("--record-worker", metavar="PATH", help=argparse.SUPPRESS)

    def add_model_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--model", default="large-v3",
                        help="Whisper model (tiny, base, small, medium, large-v3). Default: large-v3")
        sp.add_argument("--device", default="auto",
                        choices=["auto", "cuda", "cpu"],
                        help="Inference device. Default: auto")
        sp.add_argument("--compute-type", default="auto",
                        help="float16, int8, int8_float16, etc. Default: auto")
        sp.add_argument("--language", default="en",
                        help="Language code, or empty string to auto-detect. Default: en")

    sub = p.add_subparsers(dest="command")

    sp_toggle = sub.add_parser("toggle", help="Toggle recording (default action)")
    add_model_opts(sp_toggle)
    sp_toggle.set_defaults(func=cmd_toggle)

    sp_start = sub.add_parser("start", help="Start recording")
    add_model_opts(sp_start)
    sp_start.set_defaults(func=cmd_start)

    sp_stop = sub.add_parser("stop", help="Stop recording, transcribe, and type")
    add_model_opts(sp_stop)
    sp_stop.set_defaults(func=cmd_stop)

    sp_check = sub.add_parser("check", help="Check platform setup")
    sp_check.set_defaults(func=cmd_check)

    sp_tf = sub.add_parser("transcribe", help="Transcribe an audio file to stdout")
    sp_tf.add_argument("path", help="Path to audio file")
    add_model_opts(sp_tf)
    sp_tf.set_defaults(func=cmd_transcribe_file)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Internal: this process is a recorder worker, not a user invocation
    if args.record_worker:
        run_record_worker(Path(args.record_worker))
        return 0

    # Default to toggle if no subcommand given
    if not getattr(args, "func", None):
        args = parser.parse_args(["toggle", *(argv or sys.argv[1:])])

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
```

### `README.md`

```markdown
# whisper-dictate

Local Whisper-powered dictation that types into any application. Cross-platform (Linux/macOS/Windows), GPU-accelerated via faster-whisper.

## Install

With [uv](https://docs.astral.sh/uv/) (recommended):

    # Linux
    uv tool install whisper-dictate

    # macOS
    uv tool install 'whisper-dictate[macos]'

    # Windows
    uv tool install 'whisper-dictate[windows]'

Or from a local wheel:

    uv build
    uv tool install dist/whisper_dictate-*.whl

## Usage

    whisper-dictate              # toggle: start, then stop+type
    whisper-dictate start        # explicit start
    whisper-dictate stop         # explicit stop+transcribe+type
    whisper-dictate check        # diagnose platform setup
    whisper-dictate transcribe FILE.wav   # transcribe a file to stdout

Bind `whisper-dictate` (no args = toggle) to a global hotkey in your OS settings.

### Options

    --model large-v3        # tiny | base | small | medium | large-v3
    --device auto           # auto | cuda | cpu
    --compute-type auto     # float16 | int8 | int8_float16 | ...
    --language en           # ISO code, or "" to auto-detect

## Platform setup

### Linux (Wayland — COSMIC, GNOME, KDE Plasma 6)

    sudo apt install ydotool
    sudo usermod -aG input $USER   # log out / back in after

Run the ydotool daemon as a user service:

    mkdir -p ~/.config/systemd/user
    cat > ~/.config/systemd/user/ydotoold.service << 'EOF'
    [Unit]
    Description=ydotool daemon
    After=graphical-session.target

    [Service]
    ExecStart=/usr/bin/ydotoold --socket-path=%h/.ydotool_socket --socket-own=%U:%U
    Restart=on-failure

    [Install]
    WantedBy=default.target
    EOF

    systemctl --user daemon-reload
    systemctl --user enable --now ydotoold

Add to `~/.bashrc` / `~/.zshrc`:

    export YDOTOOL_SOCKET="$HOME/.ydotool_socket"

**COSMIC keybinding:** Settings → Input → Keyboard → Custom Shortcuts → add `whisper-dictate` bound to (e.g.) `Super+Space`.

### Linux (X11)

    sudo apt install xdotool

Bind `whisper-dictate` to a hotkey in your DE.

### macOS

    uv tool install 'whisper-dictate[macos]'

On first run, grant Accessibility permission to the launcher (Terminal, iTerm, or whatever invokes the CLI) in **System Settings → Privacy & Security → Accessibility**.

Bind a hotkey with Raycast, Hammerspoon, or Shortcuts.app.

### Windows

    uv tool install 'whisper-dictate[windows]'

Bind a hotkey with PowerToys, AutoHotkey, or a `.lnk` shortcut.

## GPU notes

faster-whisper uses CTranslate2. For CUDA, install the cuDNN/cuBLAS libs:

    # Ubuntu / Pop!_OS
    sudo apt install libcudnn9-cuda-12

Then `whisper-dictate check` should report cuda-capable. Models live in `~/.cache/huggingface/`.

## Troubleshooting

    whisper-dictate check

Common issues:

- **"Permission denied" on /dev/uinput (Linux):** log out after `usermod -aG input`.
- **Text doesn't appear on Wayland:** xdotool is being used instead of ydotool. Run `whisper-dictate check`.
- **Garbled / dropped characters in web apps:** increase the key delay (currently 3ms).
- **CUDA OOM with large-v3:** drop to `--model medium` or `--compute-type int8_float16`.

## License

MIT
```

## Build and verify

After creating all the files:

```bash
cd whisper-dictate
uv build
# Produces dist/whisper_dictate-0.1.0-py3-none-any.whl

# Test install in an isolated env
uv tool install --reinstall ./dist/whisper_dictate-0.1.0-py3-none-any.whl

# Smoke tests
whisper-dictate --help
whisper-dictate --version
whisper-dictate check
```

`whisper-dictate check` on a fresh Pop!_OS COSMIC system without ydotool installed should report **NOT OK** with the instruction to `sudo apt install ydotool`. After installing ydotool and starting the daemon, it should report **OK (ydotool on wayland)**.

## Design notes

A few non-obvious decisions to preserve when implementing:

1. **The recorder uses self-respawn, not a separate worker script.** The CLI re-execs itself with `--record-worker PATH` (hidden from `--help`). This keeps everything in one package and avoids needing a second entry point or IPC mechanism.

2. **The `--record-worker` flag is checked in `main()` before subcommand dispatch.** If present, the process becomes a recorder worker and exits when SIGTERM arrives — it never reaches the subcommand router.

3. **No torch dependency.** CUDA detection uses `ctypes.CDLL` to try loading the CUDA runtime library directly. faster-whisper / CTranslate2 doesn't need torch.

4. **The wheel is `py3-none-any` — one wheel, all platforms.** Platform-specific behavior happens at runtime in the backend dispatcher. Platform extras (`[macos]`, `[windows]`) pull pynput only where needed; Linux uses the ydotool CLI so no extra Python dep.

5. **Default model is `large-v3`** because the primary user has GPU + 128 GB RAM. CPU users should override with `--model base.en` or similar.

6. **Auto-defaults for compute_type:** `float16` on CUDA, `int8` on CPU. Override with `--compute-type int8_float16` for a memory/speed compromise on smaller GPUs.

## Out of scope (don't implement these unless asked)

- Push-to-talk (hold-to-record) — would require a background daemon listening for key release events; toggle model is intentionally simpler.
- Tray icon or GUI.
- Streaming / partial transcription.
- Custom vocabulary or post-processing.
- Per-app text formatting rules.
- Publishing to PyPI (the user can do `uv publish` themselves).

Build the project exactly as specified above, then run the build and verification steps. Report any errors and the output of `whisper-dictate check`.