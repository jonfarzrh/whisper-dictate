"""Transcription via faster-whisper. Lazy-loads the model."""
from __future__ import annotations

import os
from pathlib import Path


def _preload_cuda_libs() -> None:
    """Make pip-installed nvidia cuDNN/cuBLAS wheels loadable by CTranslate2.

    Those wheels drop their .so files under site-packages/nvidia/*/lib, which is
    not on the dynamic loader's search path — so CTranslate2's dlopen-by-soname
    can't find them. Preloading every lib there with RTLD_GLOBAL makes them
    resident in the process, so the later dlopen reuses the already-loaded copy.
    cuBLAS first (cuDNN depends on it); two passes resolve inter-lib ordering.
    Silently no-ops if the wheels aren't installed (e.g. system cuDNN is used).
    """
    import ctypes
    import glob
    import importlib.util

    libs: list[str] = []
    for pkg in ("nvidia.cublas", "nvidia.cudnn"):
        try:
            spec = importlib.util.find_spec(pkg)
        except ModuleNotFoundError:
            spec = None
        if spec is None or not spec.submodule_search_locations:
            continue
        libdir = os.path.join(list(spec.submodule_search_locations)[0], "lib")
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
