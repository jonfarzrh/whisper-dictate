"""Transcription via faster-whisper or MLX-Whisper. Lazy-loads the model.

The engine layer (``resolve_engine`` + ``load_model_for_engine`` +
``transcribe_with_engine``) is a thin abstraction over two backends:

- ``faster_whisper`` — the cross-platform default; CPU and CUDA, via CTranslate2.
- ``mlx`` — Apple Silicon only, runs Whisper on the Metal GPU via mlx-whisper.

Only the model loader and inference call vary, so this lives in-module rather
than a subpackage. Heavy imports (``faster_whisper``, ``mlx_whisper``) stay
lazy: they happen inside the function that needs them, never at module top
level, so importing this module is cheap on any platform.
"""
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


def _is_mlx_available() -> bool:
    """True only on Apple Silicon (darwin arm64) when ``mlx_whisper`` is
    importable. Uses ``importlib.util.find_spec`` so ``mlx_whisper`` is NOT
    actually imported — calling this is cheap and safe on Linux/Windows."""
    try:
        if sys.platform != "darwin":
            return False
        import platform
        if platform.machine() != "arm64":
            return False
        import importlib.util
        return importlib.util.find_spec("mlx_whisper") is not None
    except Exception:  # noqa: BLE001 - probing must never raise
        return False


def _mlx_repo(model_name: str) -> str:
    """Map a short Whisper model name to the corresponding mlx-community HF
    repo. If ``model_name`` already contains a ``/`` it's treated as a full
    repo path and returned unchanged, so a user who's mirrored or quantized a
    model can plug it straight into settings."""
    if "/" in model_name:
        return model_name
    return f"mlx-community/whisper-{model_name}-mlx"


class _MlxModelHandle:
    """Lightweight cache slot for the MLX engine.

    Participates in the daemon's one-model cache so engine-or-model switches
    trigger the same eviction logic as faster-whisper. Carries only the
    resolved HF repo; the actual MLX model isn't held here — mlx-whisper
    manages its own model lifecycle keyed on ``path_or_hf_repo``. NOT a
    dataclass: plain class with ``__slots__`` to keep the footprint minimal."""

    __slots__ = ("repo",)

    def __init__(self, repo: str) -> None:
        self.repo = repo


def resolve_engine(engine: str) -> str:
    """Resolve ``"auto"`` to a concrete engine name.

    ``"auto"`` -> ``"mlx"`` on Apple Silicon when mlx-whisper is installed,
    ``"faster_whisper"`` everywhere else. Explicit ``"mlx"`` /
    ``"faster_whisper"`` is returned unchanged so the user can force a
    specific backend regardless of detection."""
    if engine == "auto":
        return "mlx" if _is_mlx_available() else "faster_whisper"
    return engine


def load_model_for_engine(
    engine: str,
    model_name: str = "large-v3",
    device: str = "auto",
    compute_type: str = "auto",
) -> object:
    """Build a model handle for the chosen engine.

    For ``faster_whisper`` this delegates to ``load_model`` (the existing
    CTranslate2 path, unchanged). For ``mlx`` this returns a lightweight
    ``_MlxModelHandle`` carrying the resolved HF repo — no ``mlx_whisper``
    import happens here. ``device`` / ``compute_type`` are ignored on the
    MLX branch because MLX manages its own Metal-side execution."""
    if engine == "mlx":
        return _MlxModelHandle(repo=_mlx_repo(model_name))
    return load_model(model_name, device, compute_type)


def transcribe_with_engine(
    engine: str,
    model: object,
    audio_path: Path,
    language: str | None = "en",
    vad: bool = False,
) -> str:
    """Run inference with an already-loaded model under the chosen engine.

    For ``faster_whisper`` this delegates to ``transcribe_with`` (unchanged).
    For ``mlx`` this lazily imports ``mlx_whisper`` and calls
    ``mlx_whisper.transcribe``. VAD isn't a feature of mlx-whisper, so when
    requested under MLX we emit a one-line stderr warning and proceed without
    it — the alternative (raising) would silently lose the user's words."""
    if engine == "mlx":
        if vad:
            print(
                "whisper-dictate: VAD not supported under MLX engine; ignoring.",
                file=sys.stderr,
                flush=True,
            )
        import mlx_whisper  # type: ignore[import-not-found]

        assert isinstance(model, _MlxModelHandle)
        result = mlx_whisper.transcribe(
            str(audio_path),
            path_or_hf_repo=model.repo,
            language=language,
        )
        return str(result["text"]).strip()
    return transcribe_with(model, audio_path, language=language, vad=vad)


def transcribe(
    audio_path: Path,
    model_name: str = "large-v3",
    device: str = "auto",
    compute_type: str = "auto",
    language: str | None = "en",
    vad: bool = False,
    *,
    engine: str = "auto",
) -> str:
    """One-shot: load a model and transcribe. Pays the model-load cost each call;
    use the warm-model daemon (server.py) to avoid that on every invocation."""
    engine = resolve_engine(engine)
    model = load_model_for_engine(engine, model_name, device, compute_type)
    return transcribe_with_engine(engine, model, audio_path, language=language, vad=vad)


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
    """Diagnose what engine/device/compute_type ``load_model`` will pick on
    this system, why, and what (if anything) the user could install to do
    better. Used by the ``check`` CLI and ``init`` to surface GPU status to
    the user instead of silently falling back to CPU."""
    device = _detect_device()
    compute_type = "float16" if device == "cuda" else "int8"
    wheels = [d.split(os.sep)[-1] for d in _nvidia_wheel_dirs()]

    hints: list[str] = []
    engine = "faster_whisper"
    if device == "cuda":
        if wheels:
            reason = f"CUDA runtime found via pip nvidia wheels ({', '.join(wheels)})."
        else:
            reason = "CUDA runtime found on system (libcudart present on the loader path)."
    elif sys.platform == "darwin":
        if _is_mlx_available():
            engine = "mlx"
            reason = ("Apple Silicon detected and mlx-whisper is installed — "
                      "MLX (Metal GPU) will be used for transcription.")
        else:
            import platform as _platform
            if _platform.machine() == "arm64":
                reason = ("Apple Silicon detected but mlx-whisper is not installed. "
                          "Install it for GPU acceleration.")
                hints.append("Install MLX support: `uv add mlx-whisper` "
                             "(or `uv tool install 'whisper-dictate[apple]'`)")
            else:
                reason = ("Intel Mac: faster-whisper on CPU. "
                          "No Metal or CUDA backend is available on Intel macOS.")
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
        "engine": engine,
        "device": device,
        "compute_type": compute_type,
        "platform": sys.platform,
        "wheels": wheels,
        "reason": reason,
        "hints": hints,
    }
