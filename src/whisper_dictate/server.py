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
    model_name: str = "large-v3",
    device: str = "auto",
    compute_type: str = "auto",
) -> None:
    """Daemon body: preload the model, then serve transcription requests until
    killed. Models are cached by (model, device, compute_type) so a request for a
    different model loads it on demand without dropping the warm default."""
    from whisper_dictate.transcriber import load_model, transcribe_with

    if not hasattr(socket, "AF_UNIX"):
        raise RuntimeError(
            "The warm-model daemon needs Unix-domain sockets, which this Python "
            "build doesn't expose (older Windows). Dictation still works without "
            "it — transcription just loads the model per call."
        )

    cache: dict[tuple[str, str, str], object] = {}

    def get_model(m: str, d: str, c: str):
        key = (m, d, c)
        if key not in cache:
            print(f"whisper-dictate server: loading {m} ({d})...", file=sys.stderr, flush=True)
            cache[key] = load_model(m, d, c)
        return cache[key]

    sp = socket_path()
    sp.unlink(missing_ok=True)

    # Warm the default model up front so the first real request is fast too.
    get_model(model_name, device, compute_type)
    print(f"whisper-dictate server: ready, listening on {sp}", file=sys.stderr, flush=True)

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
                    model = get_model(
                        req.get("model") or model_name,
                        req.get("device") or device,
                        req.get("compute_type") or compute_type,
                    )
                    text = transcribe_with(
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
