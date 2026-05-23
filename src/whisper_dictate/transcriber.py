"""Transcription via faster-whisper. Lazy-loads the model."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _nvidia_wheel_dirs() -> list[str]:
    """site-packages/nvidia/{cublas,cudnn} package dirs, if installed."""
    import importlib.util

    out: list[str] = []
    for pkg in ("nvidia.cublas", "nvidia.cudnn"):
        try:
            spec = importlib.util.find_spec(pkg)
        except ModuleNotFoundError:
            spec = None
        if spec is None or not spec.submodule_search_locations:
            continue
        out.append(list(spec.submodule_search_locations)[0])
    return out


def _preload_cuda_libs() -> None:
    """Make pip-installed nvidia cuDNN/cuBLAS libs loadable by CTranslate2.

    Those wheels drop their shared libs under site-packages/nvidia/*/{lib,bin},
    which is not on the dynamic loader's search path — so CTranslate2's lookup
    by soname/DLL name can't find them. On Linux we ctypes-load every .so with
    RTLD_GLOBAL so the later dlopen reuses the already-loaded copy; two passes
    resolve inter-lib ordering. On Windows we add each bin/ dir to the DLL
    search path so LoadLibrary resolves the same names. No-ops if the wheels
    aren't installed (e.g. system CUDA is used) or on unsupported platforms.
    """
    import ctypes
    import glob

    wheel_dirs = _nvidia_wheel_dirs()

    if sys.platform == "win32":
        for base in wheel_dirs:
            bindir = os.path.join(base, "bin")
            if os.path.isdir(bindir):
                try:
                    os.add_dll_directory(bindir)
                except OSError:
                    pass
        return

    libs: list[str] = []
    for base in wheel_dirs:
        libdir = os.path.join(base, "lib")
        libs.extend(sorted(glob.glob(os.path.join(libdir, "*.so*"))))
    for _ in range(2):
        for so in libs:
            try:
                ctypes.CDLL(so, mode=ctypes.RTLD_GLOBAL)
            except OSError:
                pass


def load_model(
    model_name: str = "large-v3",
    device: str = "auto",
    compute_type: str = "auto",
):
    """Construct a WhisperModel, resolving auto device/compute_type and making
    the bundled cuDNN/cuBLAS libs loadable when running on CUDA. The returned
    model can be reused across many transcriptions (see server.py)."""
    from faster_whisper import WhisperModel

    if device == "auto":
        device = _detect_device()
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"

    if device == "cuda":
        _preload_cuda_libs()

    return WhisperModel(model_name, device=device, compute_type=compute_type)


def transcribe_with(model, audio_path: Path, language: str | None = "en", vad: bool = False) -> str:
    """Transcribe a file with an already-loaded model."""
    # VAD is off by default: in toggle-dictation the user marks the start/stop,
    # and Silero's default threshold silently drops quiet speech (yielding an
    # empty transcript). When enabled, use a lower threshold so soft mics still
    # register as speech.
    vad_kwargs = {"vad_filter": True, "vad_parameters": {"threshold": 0.2}} if vad else {"vad_filter": False}

    segments, _info = model.transcribe(
        str(audio_path),
        language=language,
        beam_size=5,
        **vad_kwargs,
    )
    return "".join(seg.text for seg in segments).strip()


def transcribe(
    audio_path: Path,
    model_name: str = "large-v3",
    device: str = "auto",
    compute_type: str = "auto",
    language: str | None = "en",
    vad: bool = False,
) -> str:
    """One-shot: load a model and transcribe. Pays the model-load cost each call;
    use the warm-model daemon (server.py) to avoid that on every invocation."""
    model = load_model(model_name, device, compute_type)
    return transcribe_with(model, audio_path, language=language, vad=vad)


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


def detect_acceleration() -> dict:
    """Diagnose what device/compute_type ``load_model`` will pick on this system,
    why, and what (if anything) the user could install to do better. Used by the
    ``check`` CLI and ``init`` to surface GPU status to the user instead of
    silently falling back to CPU."""
    device = _detect_device()
    compute_type = "float16" if device == "cuda" else "int8"
    wheels = [d.split(os.sep)[-1] for d in _nvidia_wheel_dirs()]

    hints: list[str] = []
    if device == "cuda":
        if wheels:
            reason = f"CUDA runtime found via pip nvidia wheels ({', '.join(wheels)})."
        else:
            reason = "CUDA runtime found on system (libcudart present on the loader path)."
    elif sys.platform == "darwin":
        reason = ("macOS: no CUDA on Apple platforms, and faster-whisper / CTranslate2 "
                  "has no Metal backend — CPU is the only supported device here.")
    elif sys.platform == "win32":
        reason = ("No CUDA runtime found (no cudart64_*.dll on the loader path, "
                  "no nvidia pip wheels installed).")
        hints.append("If you have an NVIDIA GPU: install the CUDA Toolkit from "
                     "NVIDIA so cudart64_12.dll and cudnn_*.dll are on PATH. "
                     "(The nvidia-cublas-cu12 / nvidia-cudnn-cu12 pip wheels are "
                     "Linux-only — Windows needs the system install.)")
    elif sys.platform.startswith("linux"):
        reason = ("No CUDA runtime found (no libcudart.so on the loader path, "
                  "no nvidia pip wheels installed).")
        hints.append("If you have an NVIDIA GPU: install the runtime wheels with "
                     "`uv add nvidia-cublas-cu12 nvidia-cudnn-cu12`, or install "
                     "your distro's CUDA package so libcudart.so.12 is loadable.")
    else:
        reason = f"Unknown platform '{sys.platform}'; defaulting to CPU."

    return {
        "device": device,
        "compute_type": compute_type,
        "platform": sys.platform,
        "wheels": wheels,
        "reason": reason,
        "hints": hints,
    }
