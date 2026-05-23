"""CLI for whisper-dictate."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from whisper_dictate import __version__
from whisper_dictate.backends import get_backend
from whisper_dictate.recorder import (
    is_recording,
    run_record_worker,
    start_recording,
    stop_recording,
)


def _notify(msg: str) -> None:
    """Best-effort cross-platform desktop notification (Linux/macOS/Windows)."""
    from whisper_dictate.notify import notify
    notify(msg)


def _is_configured() -> bool:
    """True once settings have been saved. `init` guarantees this file exists, so
    its absence means setup hasn't run — the signal for first-run behavior."""
    from whisper_dictate.config import config_file
    return config_file().exists()


def _open_settings_gui() -> int:
    """Open the settings window (blocking), preferring Qt and falling back to the
    Tkinter UI if PySide6 is missing."""
    if _has_pyside6():
        from whisper_dictate.qtgui import run_settings
    else:
        from whisper_dictate.gui import run_settings
    return run_settings()


def _effective_language(args: argparse.Namespace) -> str | None:
    """Whisper language for this run. Translating implies an arbitrary spoken
    language, so we let Whisper auto-detect the source rather than forcing the
    `en` default — the user picks only the *target* (`--translate-to`), never the
    source. An explicit non-default `--language` still wins. Empty string -> auto."""
    lang = args.language
    if getattr(args, "translate_to", None) and lang == "en":
        return None  # auto-detect the spoken language
    return lang or None  # "" -> None (auto-detect)


def _transcribe(args: argparse.Namespace, wav: Path) -> str:
    """Transcribe via the warm-model daemon if it's running, else in-process.
    The daemon keeps the model resident so this avoids the per-call load cost."""
    from whisper_dictate.server import transcribe_via_server

    language = _effective_language(args)
    text = transcribe_via_server(
        wav, args.model, args.device, args.compute_type, language, args.vad
    )
    if text is not None:
        return text

    from whisper_dictate.transcriber import transcribe
    return transcribe(
        wav,
        model_name=args.model,
        device=args.device,
        compute_type=args.compute_type,
        language=language,
        vad=args.vad,
    )


def _postprocess(args: argparse.Namespace, text: str) -> str:
    """Optionally translate and/or restyle the transcript via Ollama. Best-effort:
    if Ollama is unavailable or errors, notify and return the raw transcript so the
    user's words are never lost."""
    translate_to = getattr(args, "translate_to", None)
    style = getattr(args, "style", None)
    if not (translate_to or style):
        return text

    from whisper_dictate import polish

    # Warn clearly for the predictable misconfigurations, rather than letting them
    # surface as an opaque transport error — then fall back to raw text.
    status, detail = polish.diagnose(args.ollama_host)
    if status != "ok":
        first = detail.splitlines()[0] if detail else "Ollama unavailable"
        _notify(f"⚠️ {first} Typed raw transcript (translation/tone skipped).")
        return text
    if not polish.model_installed(args.ollama_model, args.ollama_host):
        _notify(f"⚠️ Model '{args.ollama_model}' not installed — run "
                f"`whisper-dictate settings` to download it. Typed raw transcript.")
        return text

    try:
        return polish.postprocess(
            text,
            translate_to=translate_to,
            style=style,
            model=args.ollama_model,
            host=args.ollama_host,
        )
    except Exception as e:  # noqa: BLE001 - degrade to raw transcript, never drop text
        _notify(f"⚠️ Ollama post-process failed — typing raw transcript ({e})")
        return text


def _transcribe_type_cleanup(args: argparse.Namespace, wav: Path) -> int:
    """Transcribe the WAV, type the result, and clean up. The recording is deleted
    only after a successful, non-empty transcription is typed; an empty result or
    a failure leaves the WAV in place so it can be inspected."""
    text = _transcribe(args, wav)
    if not text:
        _notify("❌ No speech detected")
        return 1  # keep the WAV for debugging

    text = _postprocess(args, text)
    try:
        get_backend().type_text(text)
    except Exception as e:  # noqa: BLE001 - surface typing failures; they're otherwise silent
        _notify(f"❌ Couldn't type the text: {e}")
        return 1  # keep the WAV so the dictation isn't lost
    wav.unlink(missing_ok=True)  # success — discard the recording
    _notify(f"✓ Typed {len(text)} chars")
    return 0


def cmd_toggle(args: argparse.Namespace) -> int:
    """Start recording if idle; stop + transcribe + type if recording."""
    if not _is_configured():
        # A hotkey press is headless, so guide the user via a notification rather
        # than silently recording before the typing backend/daemon are set up.
        _notify("whisper-dictate isn't set up yet — run: whisper-dictate init")
        return 1
    if is_recording():
        _notify("⏳ Transcribing...")
        wav = stop_recording()
        if wav is None or not wav.exists():
            _notify("❌ No audio captured")
            return 1
        return _transcribe_type_cleanup(args, wav)
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
    return _transcribe_type_cleanup(args, wav)


def cmd_check(args: argparse.Namespace) -> int:
    backend = get_backend()
    ok, msg = backend.check()
    print(f"Backend: {backend.name}")
    print(f"Status:  {'OK' if ok else 'NOT OK'}")
    print(msg)

    from whisper_dictate.transcriber import detect_acceleration
    accel = detect_acceleration()
    print("\nTranscription acceleration")
    print(f"  device:        {accel['device']}")
    print(f"  compute_type:  {accel['compute_type']}")
    if accel["reason"]:
        print(f"  {accel['reason']}")
    for hint in accel["hints"]:
        print(f"  hint: {hint}")
    return 0 if ok else 1


def cmd_transcribe_file(args: argparse.Namespace) -> int:
    """Transcribe an existing audio file (for testing without recording)."""
    print(_postprocess(args, _transcribe(args, Path(args.path))))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the warm-model daemon in the foreground (use a systemd user service to
    keep it running). Holds the model in memory so dictation is near-instant."""
    from whisper_dictate.server import run_server
    run_server(
        model_name=args.model,
        device=args.device,
        compute_type=args.compute_type,
    )
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Check all prerequisites for this OS and set up what can be done without root.

    On a fresh install (no saved settings yet) this opens the settings window
    first, so the user picks their model *before* the warm-model daemon starts —
    otherwise the daemon would download and warm the default (large-v3) even if
    they wanted something smaller."""
    from whisper_dictate import config

    if not config.config_file().exists():
        print("No saved settings yet — opening the settings window so you can pick "
              "your model before setup.\n(Close it to accept the defaults.)\n")
        try:
            _open_settings_gui()
        except Exception as e:  # noqa: BLE001 - headless/no-GUI: fall back to defaults
            print(f"(Couldn't open the settings window: {e}; continuing with defaults.)\n")
        # Materialize a config file so the daemon and the first-run hotkey check
        # have a concrete source of truth even if the window was closed unsaved.
        config.save_config(config.load_config())

    from whisper_dictate.init import run_init
    return run_init(model=args.model, with_server=not args.no_server, assume_yes=args.yes)


def cmd_deinit(args: argparse.Namespace) -> int:
    """Tear down the services and runtime state that `init` created."""
    from whisper_dictate.init import run_deinit
    return run_deinit()


def _has_pyside6() -> bool:
    import importlib.util
    return importlib.util.find_spec("PySide6") is not None


def cmd_settings(args: argparse.Namespace) -> int:
    """Open the settings GUI to edit saved defaults (model, translation, tone…).
    Prefers the Qt UI; falls back to the plain Tkinter window if PySide6 isn't
    installed."""
    return _open_settings_gui()


def cmd_tray(args: argparse.Namespace) -> int:
    """Run the system-tray application (the long-running 'app'): a mic icon with
    dictation toggle, settings, and quit. Requires the Qt GUI (`gui` extra)."""
    from whisper_dictate.qtgui import run_tray
    return run_tray()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="whisper-dictate", description=__doc__)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    # Hidden flag used internally when the CLI re-execs itself as a recorder worker
    p.add_argument("--record-worker", metavar="PATH", help=argparse.SUPPRESS)

    # Saved GUI settings become the flag *defaults*, so a bare hotkey press picks
    # them up; an explicit --flag on the command line still overrides them.
    from whisper_dictate.config import load_config
    cfg = load_config()

    def add_model_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--model", default=cfg["model"],
                        help="Whisper model (tiny, base, small, medium, large-v3). Default: large-v3")
        sp.add_argument("--device", default=cfg["device"],
                        choices=["auto", "cuda", "cpu"],
                        help="Inference device. Default: auto")
        sp.add_argument("--compute-type", default=cfg["compute_type"],
                        help="float16, int8, int8_float16, etc. Default: auto")
        sp.add_argument("--language", default=cfg["language"],
                        help="Language code, or empty string to auto-detect. Default: en")
        sp.add_argument("--vad", action=argparse.BooleanOptionalAction, default=bool(cfg["vad"]),
                        help="Voice-activity detection filtering (--vad / --no-vad). Off by "
                             "default (it can drop quiet speech in toggle dictation).")

    def add_polish_opts(sp: argparse.ArgumentParser) -> None:
        """Optional LLM post-processing via a local Ollama server. Inert unless
        --translate-to or --style is given."""
        from whisper_dictate.polish import DEFAULT_MODEL, STYLE_PRESETS
        sp.add_argument("--translate-to", metavar="LANG", default=cfg["translate_to"] or None,
                        help="Translate the transcript into this language (name or code, "
                             "e.g. 'English', 'Spanish', 'ja'). The spoken language is "
                             "auto-detected. Requires Ollama.")
        sp.add_argument("--style", metavar="TONE", default=cfg["style"] or None,
                        help="Rewrite the transcript in this tone. Presets: "
                             + ", ".join(sorted(STYLE_PRESETS))
                             + ". Or pass a free-form instruction, e.g. "
                             + "--style 'as a polite email'. Requires Ollama.")
        sp.add_argument("--ollama-model", default=cfg["ollama_model"] or DEFAULT_MODEL,
                        help=f"Ollama model for translate/restyle. Default: {DEFAULT_MODEL}")
        sp.add_argument("--ollama-host", default=cfg["ollama_host"] or None,
                        help="Ollama base URL. Default: $OLLAMA_HOST or http://localhost:11434")

    sub = p.add_subparsers(dest="command")

    sp_toggle = sub.add_parser("toggle", help="Toggle recording (default action)")
    add_model_opts(sp_toggle)
    add_polish_opts(sp_toggle)
    sp_toggle.set_defaults(func=cmd_toggle)

    sp_start = sub.add_parser("start", help="Start recording")
    add_model_opts(sp_start)
    sp_start.set_defaults(func=cmd_start)

    sp_stop = sub.add_parser("stop", help="Stop recording, transcribe, and type")
    add_model_opts(sp_stop)
    add_polish_opts(sp_stop)
    sp_stop.set_defaults(func=cmd_stop)

    sp_check = sub.add_parser("check", help="Check platform setup")
    sp_check.set_defaults(func=cmd_check)

    sp_tf = sub.add_parser("transcribe", help="Transcribe an audio file to stdout")
    sp_tf.add_argument("path", help="Path to audio file")
    add_model_opts(sp_tf)
    add_polish_opts(sp_tf)
    sp_tf.set_defaults(func=cmd_transcribe_file)

    sp_serve = sub.add_parser("serve", help="Run the warm-model daemon (keeps the model in memory for instant transcription)")
    add_model_opts(sp_serve)
    sp_serve.set_defaults(func=cmd_serve)

    sp_init = sub.add_parser("init", help="Check prerequisites and set up daemons for this OS (painless install)")
    sp_init.add_argument("--model", default=None,
                         help="Pin the warm-model daemon to a specific model. By default "
                              "the daemon follows your saved settings, so changing the model "
                              "in the settings window takes effect with no re-init.")
    sp_init.add_argument("--no-server", action="store_true",
                         help="Don't install the warm-model daemon service")
    sp_init.add_argument("--yes", action="store_true",
                         help="Run system-package installs automatically (uses sudo)")
    sp_init.set_defaults(func=cmd_init)

    sp_deinit = sub.add_parser("deinit", help="Remove the services and state that `init` created")
    sp_deinit.set_defaults(func=cmd_deinit)

    sp_settings = sub.add_parser("settings", aliases=["gui", "config"],
                                 help="Open the settings window (model, translation, tone…)")
    sp_settings.set_defaults(func=cmd_settings)

    sp_tray = sub.add_parser("tray", aliases=["app"],
                             help="Run the system-tray app (launch this as an application)")
    sp_tray.set_defaults(func=cmd_tray)

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
