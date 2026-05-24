"""Optional warm-model daemon.

Loading large-v3 into VRAM costs ~3s and happens on *every* CLI invocation. This
daemon loads the model once and keeps it resident, serving transcription requests
over a Unix domain socket so each dictation is near-instant (~0.3s inference only).

The client (`transcribe_via_server`) sends just the audio *path* — daemon and
client are the same user on the same machine, so there's no need to ship bytes.
If the daemon isn't running, the client returns None and the CLI falls back to
one-shot in-process transcription.
"""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

from whisper_dictate.recorder import _state_dir


def socket_path() -> Path:
    return _state_dir() / "server.sock"


def transcribe_via_server(
    audio_path: Path,
    model_name: str,
    device: str,
    compute_type: str,
    language: str | None,
    vad: bool,
    engine: str,
    timeout: float = 300.0,
) -> str | None:
    """Transcribe via the warm daemon. Returns the text (possibly "") on success,
    or None if the daemon is unreachable / errored — signalling the caller to fall
    back to in-process transcription."""
    if not hasattr(socket, "AF_UNIX"):
        return None  # no Unix sockets here (older Windows) -> caller falls back

    sp = socket_path()
    if not sp.exists():
        return None

    req = json.dumps({
        "path": str(audio_path),
        "model": model_name,
        "device": device,
        "compute_type": compute_type,
        "language": language,
        "vad": vad,
        "engine": engine,
    }).encode() + b"\n"

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(sp))
            s.sendall(req)
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except (OSError, socket.timeout):
        return None

    try:
        resp = json.loads(buf.split(b"\n", 1)[0].decode())
    except (ValueError, IndexError):
        return None
    return resp.get("text") if "error" not in resp else None


def run_server(
    model_name: str | None = None,
    device: str = "auto",
    compute_type: str = "auto",
    engine_name: str | None = None,
) -> None:
    """Daemon body: preload the model, then serve transcription requests until
    killed. The model isn't pinned at launch — each request carries the model the
    client read from saved settings, so changing the model in the settings window
    takes effect on the very next press with no need to restart the daemon. Only
    one model is kept resident: a request for a different (engine, model, device,
    compute_type) combination evicts the previous one (freeing its VRAM) and
    loads the new one. When ``model_name`` / ``engine_name`` are None they're
    read from the saved config so the daemon and the GUI share a single source
    of truth.

    A background thread watches the config file and pre-loads the new model as
    soon as the settings window saves a change, so even the *first* press after
    switching models is fast — not just the ones after it."""
    import threading
    import time

    from whisper_dictate.transcriber import (
        load_model_for_engine,
        resolve_engine,
        transcribe_with_engine,
    )

    if not hasattr(socket, "AF_UNIX"):
        raise RuntimeError(
            "The warm-model daemon needs Unix-domain sockets, which this Python "
            "build doesn't expose (older Windows). Dictation still works without "
            "it — transcription just loads the model per call."
        )

    if not model_name or not engine_name:
        from whisper_dictate.config import load_config
        cfg = load_config()
        if not model_name:
            model_name = str(cfg["model"])
        if not engine_name:
            engine_name = str(cfg.get("engine", "auto"))

    # Keep exactly one model resident. The model is a global setting (translation
    # and tone are the per-hotkey knobs, not the model), so caching more than one
    # only ever pins stale VRAM after the user switches models in settings. The
    # lock guards the cache against the config-watcher thread racing the serve loop.
    # Key is (engine, model, device, compute_type) so switching ENGINE also evicts.
    cache: dict[tuple[str, str, str, str], object] = {}
    cache_lock = threading.Lock()

    def get_model(e: str, m: str, d: str, c: str):
        key = (e, m, d, c)
        with cache_lock:
            if key not in cache:
                if cache:  # a different model was resident — drop it and free its VRAM
                    cache.clear()
                    import gc
                    gc.collect()
                print(f"whisper-dictate server: loading {m} ({e}/{d})...",
                      file=sys.stderr, flush=True)
                cache[key] = load_model_for_engine(e, m, d, c)
            return cache[key]

    sp = socket_path()
    sp.unlink(missing_ok=True)

    # Warm the default model up front so the first real request is fast too.
    startup_engine = resolve_engine(engine_name)
    get_model(startup_engine, model_name, device, compute_type)
    print(f"whisper-dictate server: ready, listening on {sp}", file=sys.stderr, flush=True)

    def watch_config() -> None:
        """Poll the config file; when it changes, pre-warm the model it now names
        so the next press is instant instead of paying a one-time load. Best-effort
        — any error here must never take the daemon down."""
        from whisper_dictate.config import config_file, load_config

        cf = config_file()
        last_mtime = cf.stat().st_mtime if cf.exists() else 0.0
        while True:
            time.sleep(2.0)
            try:
                mtime = cf.stat().st_mtime if cf.exists() else 0.0
                if mtime == last_mtime:
                    continue
                last_mtime = mtime
                cfg = load_config()
                key = (
                    resolve_engine(str(cfg.get("engine", "auto"))),
                    str(cfg["model"]),
                    str(cfg["device"]),
                    str(cfg["compute_type"]),
                )
                if key not in cache:
                    print("whisper-dictate server: settings changed, pre-warming "
                          f"{key[1]} ({key[0]})...", file=sys.stderr, flush=True)
                    get_model(*key)
            except Exception:  # noqa: BLE001 - the watcher must not crash the daemon
                pass

    threading.Thread(target=watch_config, daemon=True).start()

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sp))
    srv.listen(4)
    try:
        while True:
            conn, _ = srv.accept()
            with conn:
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                if not buf:
                    continue
                try:
                    req = json.loads(buf.split(b"\n", 1)[0].decode())
                    # Old clients that predate the engine field still work:
                    # they get the daemon's startup default.
                    req_engine = resolve_engine(req.get("engine") or engine_name)
                    model = get_model(
                        req_engine,
                        req.get("model") or model_name,
                        req.get("device") or device,
                        req.get("compute_type") or compute_type,
                    )
                    text = transcribe_with_engine(
                        req_engine,
                        model,
                        Path(req["path"]),
                        language=req.get("language", "en"),
                        vad=bool(req.get("vad")),
                    )
                    resp = {"text": text}
                except Exception as e:  # noqa: BLE001 - report any failure to the client
                    resp = {"error": str(e)}
                conn.sendall(json.dumps(resp).encode() + b"\n")
    finally:
        sp.unlink(missing_ok=True)
