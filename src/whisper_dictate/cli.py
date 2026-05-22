"""CLI for whisper-dictate."""
from __future__ import annotations

import argparse
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
    """Best-effort cross-platform desktop notification (Linux/macOS/Windows)."""
    from whisper_dictate.notify import notify
    notify(msg)


def _transcribe(args: argparse.Namespace, wav: Path) -> str:
    """Transcribe via the warm-model daemon if it's running, else in-process.
    The daemon keeps the model resident so this avoids the per-call load cost."""
    from whisper_dictate.server import transcribe_via_server

    text = transcribe_via_server(
        wav, args.model, args.device, args.compute_type, args.language, args.vad
    )
    if text is not None:
        return text

    from whisper_dictate.transcriber import transcribe
    return transcribe(
        wav,
        model_name=args.model,
        device=args.device,
        compute_type=args.compute_type,
        language=args.language,
        vad=args.vad,
    )


def cmd_toggle(args: argparse.Namespace) -> int:
    """Start recording if idle; stop + transcribe + type if recording."""
    if is_recording():
        _notify("⏳ Transcribing...")
        wav = stop_recording()
        if wav is None or not wav.exists():
            _notify("❌ No audio captured")
            return 1

        text = _transcribe(args, wav)

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

    text = _transcribe(args, wav)
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
    print(_transcribe(args, Path(args.path)))
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
    """Check all prerequisites for this OS and set up what can be done without root."""
    from whisper_dictate.init import run_init
    return run_init(model=args.model, with_server=not args.no_server, assume_yes=args.yes)


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
        sp.add_argument("--vad", action="store_true",
                        help="Enable voice-activity detection filtering. Off by default "
                             "(it can drop quiet speech in toggle dictation).")

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

    sp_serve = sub.add_parser("serve", help="Run the warm-model daemon (keeps the model in memory for instant transcription)")
    add_model_opts(sp_serve)
    sp_serve.set_defaults(func=cmd_serve)

    sp_init = sub.add_parser("init", help="Check prerequisites and set up daemons for this OS (painless install)")
    sp_init.add_argument("--model", default="large-v3",
                         help="Model the warm-model daemon should preload. Default: large-v3")
    sp_init.add_argument("--no-server", action="store_true",
                         help="Don't install the warm-model daemon service")
    sp_init.add_argument("--yes", action="store_true",
                         help="Run system-package installs automatically (uses sudo)")
    sp_init.set_defaults(func=cmd_init)

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
