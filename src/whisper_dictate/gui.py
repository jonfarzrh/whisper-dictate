"""Tkinter settings window for whisper-dictate.

Lets a non-technical user pick their model, language, translation target and tone
without touching CLI flags. It reads/writes the JSON config in `config.py`; a
plain hotkey press of `whisper-dictate` then picks up whatever was saved here.

Tkinter ships with Python (including uv's managed builds), so this adds no
dependency. The window-building logic is kept as a standalone function so a
future tray/menu-bar app can pop the same dialog without rework.
"""
from __future__ import annotations

import glob
import os
import sys
import threading

from whisper_dictate import config
from whisper_dictate.polish import STYLE_PRESETS


def _ensure_tcl_env() -> None:
    """Point TCL_LIBRARY/TK_LIBRARY at the interpreter's own bundled Tcl/Tk.

    Relocatable Python builds (e.g. uv's managed CPython, python-build-standalone)
    bake in the build machine's Tcl path, so on an end-user box Tkinter fails with
    "Can't find a usable init.tcl". The real libs ship next to the interpreter —
    under `lib/` on Linux/macOS and under `tcl/` on Windows — so locate them and
    set the env vars Tk reads at import time. No-op if the vars are already set or
    no bundled libs are found (then a system Tk install is used)."""
    if os.environ.get("TCL_LIBRARY"):
        return
    # (subdir holding the version dirs, tcl glob, tk glob) for each layout.
    layouts = (("lib", "tcl8.*", "tk8.*"), ("tcl", "tcl8.*", "tk8.*"))
    for prefix in (sys.base_prefix, sys.prefix):
        for sub, tcl_glob, tk_glob in layouts:
            tcl = sorted(glob.glob(os.path.join(prefix, sub, tcl_glob)))
            tk = sorted(glob.glob(os.path.join(prefix, sub, tk_glob)))
            if tcl and os.path.exists(os.path.join(tcl[-1], "init.tcl")):
                os.environ["TCL_LIBRARY"] = tcl[-1]
                if tk:
                    os.environ["TK_LIBRARY"] = tk[-1]
                return

# Friendly label <-> stored-value mappings for the fields where the stored
# value isn't what we want to show the user. Everything else is shown verbatim.
_LANG_NONE = "auto-detect"     # stored as ""
_OPT_NONE = "(none)"           # stored as "" for translate_to / style
_MIC_DEFAULT = "System default"  # stored as "" for input_device

# A starter list of spoken/target languages. Both comboboxes stay editable, so a
# user can type any Whisper code or language name we didn't list.
_COMMON_LANGS = [
    "English", "Spanish", "French", "German", "Italian", "Portuguese",
    "Dutch", "Russian", "Japanese", "Korean", "Chinese", "Arabic", "Hindi",
]
_SPOKEN_LANG_CODES = [
    "en", "es", "fr", "de", "it", "pt", "nl", "ru", "ja", "ko", "zh", "ar", "hi",
]


def run_settings() -> int:
    """Open the settings window (blocks until closed). Returns 0 on normal exit,
    1 if Tkinter is unavailable (e.g. a headless box or a Python built without
    Tk) — with a printed hint rather than a traceback."""
    _ensure_tcl_env()
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except Exception as e:  # noqa: BLE001 - missing Tk is an environment issue, not a bug
        print(
            f"Could not load the settings GUI ({e}).\n"
            "Tkinter isn't available in this Python. On Linux install the system "
            "package (e.g. `sudo apt install python3-tk`), or just edit the config "
            f"file directly:\n  {config.config_file()}"
        )
        return 1

    cfg = config.load_config()

    root = tk.Tk()
    root.title("whisper-dictate settings")
    root.resizable(False, False)

    frm = ttk.Frame(root, padding=16)
    frm.grid(sticky="nsew")
    row = 0

    def add_row(label: str, widget) -> None:
        nonlocal row
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
        widget.grid(row=row, column=1, sticky="ew", pady=4)
        row += 1

    frm.columnconfigure(1, weight=1, minsize=220)

    # --- Transcription ---
    ttk.Label(frm, text="Transcription", font=("", 10, "bold")).grid(
        row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
    row += 1

    # Microphone picker — first entry is the system-default sentinel ("" in
    # config). Device names come from sounddevice; if PortAudio isn't installed
    # or no input device is present, only "System default" is offered.
    mic_names: list[str] = []
    try:
        import sounddevice as sd
        mic_names = [d["name"] for d in sd.query_devices()
                     if d.get("max_input_channels", 0) > 0]
    except Exception:  # noqa: BLE001 - no PortAudio / no mic -> show default only
        pass
    saved_mic = str(cfg.get("input_device", ""))
    mic_var = tk.StringVar(value=(_MIC_DEFAULT if saved_mic == "" else saved_mic))
    add_row("Microphone", ttk.Combobox(
        frm, textvariable=mic_var, state="readonly",
        values=[_MIC_DEFAULT, *mic_names]))

    engine_var = tk.StringVar(value=str(cfg.get("engine", "auto")))
    add_row("Engine", ttk.Combobox(
        frm, textvariable=engine_var, state="readonly",
        values=["auto", "faster_whisper", "mlx"]))

    model_var = tk.StringVar(value=str(cfg["model"]))
    add_row("Whisper model", ttk.Combobox(
        frm, textvariable=model_var,
        values=["tiny", "base", "small", "medium", "large-v3"]))

    device_var = tk.StringVar(value=str(cfg["device"]))
    add_row("Device", ttk.Combobox(
        frm, textvariable=device_var, state="readonly",
        values=["auto", "cuda", "cpu"]))

    compute_var = tk.StringVar(value=str(cfg["compute_type"]))
    add_row("Compute type", ttk.Combobox(
        frm, textvariable=compute_var,
        values=["auto", "float16", "int8", "int8_float16"]))

    # Spoken language: show "auto-detect" for the stored empty string.
    lang_var = tk.StringVar(value=(_LANG_NONE if cfg["language"] == "" else str(cfg["language"])))
    add_row("Spoken language", ttk.Combobox(
        frm, textvariable=lang_var, values=[_LANG_NONE, *_SPOKEN_LANG_CODES]))

    vad_var = tk.BooleanVar(value=bool(cfg["vad"]))
    add_row("Voice-activity filter", ttk.Checkbutton(frm, variable=vad_var))

    # Linux-only: ydotool/xdotool inter-keystroke delay. On macOS/Windows the
    # typing backend (pynput) has no equivalent knob, so the control is hidden
    # there to keep the form uncluttered. The saved value is still preserved.
    key_delay_var: "tk.StringVar | None" = None
    if sys.platform.startswith("linux"):
        try:
            initial_delay = str(int(cfg.get("type_key_delay_ms", 12)))
        except (TypeError, ValueError):
            initial_delay = "12"
        key_delay_var = tk.StringVar(value=initial_delay)
        add_row("Typing key delay (ms)",
                ttk.Spinbox(frm, from_=1, to=200, textvariable=key_delay_var, width=8))

    ttk.Separator(frm, orient="horizontal").grid(
        row=row, column=0, columnspan=2, sticky="ew", pady=10)
    row += 1

    # --- Translate & restyle (Ollama) ---
    ttk.Label(frm, text="Translate & restyle (needs Ollama)", font=("", 10, "bold")).grid(
        row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
    row += 1

    translate_var = tk.StringVar(value=(_OPT_NONE if cfg["translate_to"] == "" else str(cfg["translate_to"])))
    add_row("Translate to", ttk.Combobox(
        frm, textvariable=translate_var, values=[_OPT_NONE, *_COMMON_LANGS]))

    style_var = tk.StringVar(value=(_OPT_NONE if cfg["style"] == "" else str(cfg["style"])))
    add_row("Tone / style", ttk.Combobox(
        frm, textvariable=style_var, values=[_OPT_NONE, *sorted(STYLE_PRESETS)]))

    ollama_model_var = tk.StringVar(value=str(cfg["ollama_model"]))
    add_row("Ollama model", ttk.Entry(frm, textvariable=ollama_model_var))

    ollama_host_var = tk.StringVar(value=str(cfg["ollama_host"]))
    add_row("Ollama host (optional)", ttk.Entry(frm, textvariable=ollama_host_var))

    # Live connectivity check so users know whether translate/restyle will work.
    status_var = tk.StringVar(value="")

    def check_ollama() -> None:
        from whisper_dictate import polish
        host = ollama_host_var.get().strip() or None
        if polish.is_available(host):
            status_var.set("✓ Ollama reachable")
        else:
            status_var.set("✗ Ollama not reachable — translate/restyle will be skipped")

    ttk.Button(frm, text="Check Ollama", command=check_ollama).grid(
        row=row, column=0, sticky="w", pady=(4, 0))
    ttk.Label(frm, textvariable=status_var, foreground="#555").grid(
        row=row, column=1, sticky="w", pady=(4, 0))
    row += 1

    ttk.Separator(frm, orient="horizontal").grid(
        row=row, column=0, columnspan=2, sticky="ew", pady=10)
    row += 1

    def _download_model(model: str, host: str | None) -> None:
        """Pull `model` in a background thread, mirroring progress into the status
        label. Tkinter isn't thread-safe, so the worker only mutates a plain dict;
        a main-thread poller (root.after) reads it and touches the widgets."""
        from whisper_dictate import polish

        prog: dict = {"line": "starting…", "done": False, "error": None}

        def worker() -> None:
            def on_prog(msg: dict) -> None:
                total, done = msg.get("total"), msg.get("completed")
                if total and done:
                    prog["line"] = f"{msg.get('status', 'downloading')} {int(done * 100 / total)}%"
                else:
                    prog["line"] = msg.get("status", "")
            try:
                polish.pull_model(model, host=host, on_progress=on_prog)
            except Exception as e:  # noqa: BLE001 - report download failure to the user
                prog["error"] = str(e)
            finally:
                prog["done"] = True

        def poll() -> None:
            status_var.set(f"⬇ Downloading {model}: {prog['line']}")
            if not prog["done"]:
                root.after(250, poll)
                return
            save_btn.config(state="normal")
            if prog["error"]:
                status_var.set(f"✗ Download failed: {prog['error']}")
                messagebox.showerror(
                    "Download failed",
                    f"Could not download '{model}':\n{prog['error']}\n\n"
                    "Settings are saved. Translation/tone will be skipped until the "
                    "model is available.",
                )
            else:
                messagebox.showinfo(
                    "Ready",
                    f"'{model}' downloaded. Settings saved — your next dictation will "
                    "translate/restyle as configured.",
                )
                root.destroy()

        save_btn.config(state="disabled")
        threading.Thread(target=worker, daemon=True).start()
        root.after(250, poll)

    def on_save() -> None:
        lang = lang_var.get().strip()
        translate = translate_var.get().strip()
        style = style_var.get().strip()
        translate = "" if translate in ("", _OPT_NONE) else translate
        style = "" if style in ("", _OPT_NONE) else style
        model = ollama_model_var.get().strip() or config.DEFAULTS["ollama_model"]
        host = ollama_host_var.get().strip() or None
        mic = mic_var.get().strip()
        # Preserve type_key_delay_ms when the control isn't shown (non-Linux), so
        # editing settings on macOS/Windows doesn't clobber a Linux-set value.
        if key_delay_var is not None:
            try:
                key_delay = int(key_delay_var.get())
            except (TypeError, ValueError):
                key_delay = int(config.DEFAULTS["type_key_delay_ms"])  # type: ignore[arg-type]
        else:
            key_delay = cfg.get("type_key_delay_ms", config.DEFAULTS["type_key_delay_ms"])
        config.save_config({
            "input_device": "" if mic in ("", _MIC_DEFAULT) else mic,
            "engine": engine_var.get().strip() or "auto",
            "model": model_var.get().strip() or "large-v3",
            "device": device_var.get().strip() or "auto",
            "compute_type": compute_var.get().strip() or "auto",
            "language": "" if lang in ("", _LANG_NONE) else lang,
            "vad": bool(vad_var.get()),
            "translate_to": translate,
            "style": style,
            "ollama_model": model,
            "ollama_host": ollama_host_var.get().strip(),
            "type_key_delay_ms": key_delay,
        })

        # Translation/tone needs the Ollama model present. If it isn't, offer to
        # download it now so the user isn't silently left with raw transcripts.
        from whisper_dictate import polish
        if (translate or style):
            status, detail = polish.diagnose(host)
            if status != "ok":
                titles = {
                    "not_installed": "Ollama not installed",
                    "not_running": "Ollama not running",
                    "unreachable": "Ollama unreachable",
                }
                messagebox.showwarning(
                    titles.get(status, "Ollama unavailable"),
                    f"Settings saved, but translation/tone can't run yet:\n\n{detail}\n\n"
                    "Until then, dictation types the raw transcript. Reopen this "
                    "window once Ollama is ready to download the model.",
                )
                root.destroy()
                return
            if not polish.model_installed(model, host):
                if messagebox.askyesno(
                    "Download model?",
                    f"The model '{model}' needed for translation/tone isn't "
                    "installed yet. Download it now? (This can be several GB and "
                    "take a few minutes.)",
                ):
                    _download_model(model, host)
                    return  # poller shows progress, then closes on success
                messagebox.showinfo(
                    "Saved",
                    "Settings saved. Until the model is downloaded, dictation will "
                    "type the raw transcript without translating/restyling.",
                )
                root.destroy()
                return

        messagebox.showinfo(
            "Saved",
            "Settings saved. Your next dictation will use them.\n\n"
            f"Stored at:\n{config.config_file()}",
        )
        root.destroy()

    btns = ttk.Frame(frm)
    btns.grid(row=row, column=0, columnspan=2, sticky="e")
    ttk.Button(btns, text="Cancel", command=root.destroy).grid(row=0, column=0, padx=(0, 8))
    save_btn = ttk.Button(btns, text="Save", command=on_save)
    save_btn.grid(row=0, column=1)
    save_btn.focus_set()

    root.mainloop()
    return 0
