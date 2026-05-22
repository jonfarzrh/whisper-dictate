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
