"""Qt (PySide6) settings window and system-tray app.

This is the graphical front-end: a polished settings dialog and a tray icon you
launch like a normal application. It reads/writes the same JSON config as the CLI
(`config.py`), so changes here flow into every hotkey-triggered dictation.

PySide6 is an optional dependency (the `gui` extra) — import it lazily and fail
with a clear message so a hotkey-only install isn't forced to carry Qt.
"""
from __future__ import annotations

import os
import sys

from whisper_dictate import config
from whisper_dictate.polish import STYLE_PRESETS

_GUI_MISSING_MSG = (
    "The graphical interface needs PySide6, which is a required dependency but "
    "couldn't be imported — your install looks incomplete. Reinstall it:\n"
    "  uv tool install --force --reinstall whisper-dictate\n"
    "(or `uv pip install PySide6` in your environment)."
)

# Friendly labels for the values we don't store verbatim.
_LANG_AUTO = "auto-detect"        # spoken language stored as ""
_COMMON_LANGS = [
    "English", "Spanish", "French", "German", "Italian", "Portuguese",
    "Dutch", "Russian", "Japanese", "Korean", "Chinese", "Arabic", "Hindi",
]
_SPOKEN_LANGS = [_LANG_AUTO, "en", "es", "fr", "de", "it", "pt", "nl", "ru",
                 "ja", "ko", "zh", "ar", "hi"]

# Design system (OLED-dark, Slate palette + green accent), per the UI/UX skill.
_C = {
    "bg": "#0F172A",          # window background (slate-950-ish)
    "surface": "#1E293B",     # card surface (slate-800)
    "surface_hi": "#243247",  # hovered surface
    "input": "#0B1220",       # input field background (deeper than cards)
    "input_off": "#162132",   # disabled input
    "fg": "#F8FAFC",          # primary text (slate-50)
    "muted": "#CBD5E1",       # field labels (slate-300)
    "muted2": "#94A3B8",      # subtitle / secondary (slate-400)
    "hint": "#7C8AA0",        # hint text (>= 4.5:1 on bg)
    "border": "#334155",      # control borders (slate-700)
    "border_hi": "#475569",   # hovered borders (slate-600)
    "card_border": "#26344B",
    "accent": "#22C55E",      # green-500
    "accent_hi": "#16A34A",   # green-600
    "accent_press": "#15803D",
    "on_accent": "#052E16",   # near-black green for text on accent (high contrast)
    "danger": "#EF4444",
}

# Qt stylesheets don't support transitions/letter-spacing/text-transform; state
# changes are instant and section labels are upper-cased in code.
_QSS = """
QWidget {{ background: {bg}; color: {fg}; }}
QToolTip {{ background: {surface}; color: {fg}; border: 1px solid {border}; padding: 4px 6px; }}

QFrame#card {{ background: {surface}; border: 1px solid {card_border}; border-radius: 12px; }}
QLabel {{ background: transparent; }}
QLabel#title {{ font-size: 20px; font-weight: 700; color: {fg}; }}
QLabel#subtitle {{ font-size: 13px; color: {muted2}; }}
QLabel#section {{ font-size: 11px; font-weight: 700; color: {muted2}; }}
QLabel#field {{ font-size: 13px; color: {muted}; }}
QLabel#hint {{ font-size: 12px; color: {hint}; }}
QLabel#status_ok {{ color: {accent}; font-size: 12px; }}
QLabel#status_bad {{ color: {danger}; font-size: 12px; }}

QComboBox, QLineEdit {{
    background: {input}; border: 1px solid {border}; border-radius: 8px;
    padding: 8px 10px; color: {fg}; min-height: 20px;
    selection-background-color: {accent}; selection-color: {on_accent};
}}
QComboBox:hover, QLineEdit:hover {{ border-color: {border_hi}; }}
QComboBox:focus, QLineEdit:focus {{ border: 1px solid {accent}; }}
QComboBox:disabled, QLineEdit:disabled {{ color: {hint}; background: {input_off}; }}
QComboBox::drop-down {{ border: none; width: 28px; }}
QComboBox::down-arrow {{ image: url("{chevron}"); width: 12px; height: 12px; margin-right: 10px; }}
QComboBox QAbstractItemView {{
    background: {surface}; border: 1px solid {border}; border-radius: 8px;
    padding: 4px; outline: none;
    selection-background-color: {accent}; selection-color: {on_accent};
}}

QCheckBox {{ spacing: 9px; color: {fg}; }}
QCheckBox::indicator {{
    width: 18px; height: 18px; border: 1px solid {border_hi};
    border-radius: 5px; background: {input};
}}
QCheckBox::indicator:hover {{ border-color: {accent}; }}
QCheckBox::indicator:checked {{
    background: {accent}; border-color: {accent}; image: url("{check}");
}}

QPushButton {{
    background: transparent; color: {fg}; border: 1px solid {border};
    border-radius: 8px; padding: 9px 18px; font-weight: 500;
}}
QPushButton:hover {{ background: {surface_hi}; border-color: {border_hi}; }}
QPushButton:pressed {{ background: {surface}; }}
QPushButton#save {{ background: {accent}; color: {on_accent}; border: none; font-weight: 700; }}
QPushButton#save:hover {{ background: {accent_hi}; }}
QPushButton#save:pressed {{ background: {accent_press}; }}

QProgressDialog {{ background: {bg}; }}
QProgressBar {{ border: 1px solid {border}; border-radius: 6px; background: {input}; text-align: center; }}
QProgressBar::chunk {{ background: {accent}; border-radius: 5px; }}
QMessageBox {{ background: {bg}; }}
"""


def _checkmark_png() -> str:
    """Render a white check mark PNG once (for the checked checkbox indicator) and
    return its path as a forward-slashed URL string for QSS. Cached on disk."""
    import os
    import tempfile

    from PySide6 import QtCore, QtGui

    path = os.path.join(tempfile.gettempdir(), "whisper-dictate-check.png")
    if not os.path.exists(path):
        pm = QtGui.QPixmap(18, 18)
        pm.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        pen = QtGui.QPen(QtGui.QColor(_C["on_accent"]), 2.2)
        pen.setCapStyle(QtCore.Qt.RoundCap)
        pen.setJoinStyle(QtCore.Qt.RoundJoin)
        p.setPen(pen)
        p.drawPolyline([QtCore.QPointF(4, 9.5), QtCore.QPointF(8, 13), QtCore.QPointF(14, 5.5)])
        p.end()
        pm.save(path)
    return path.replace("\\", "/")


def _chevron_png() -> str:
    """Render a downward chevron PNG once (for the combo-box arrow) and return its
    QSS-friendly path. Cached on disk."""
    import os
    import tempfile

    from PySide6 import QtCore, QtGui

    path = os.path.join(tempfile.gettempdir(), "whisper-dictate-chevron.png")
    if not os.path.exists(path):
        pm = QtGui.QPixmap(12, 12)
        pm.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        pen = QtGui.QPen(QtGui.QColor(_C["muted2"]), 1.6)
        pen.setCapStyle(QtCore.Qt.RoundCap)
        pen.setJoinStyle(QtCore.Qt.RoundJoin)
        p.setPen(pen)
        p.drawPolyline([QtCore.QPointF(2.5, 4.5), QtCore.QPointF(6, 8), QtCore.QPointF(9.5, 4.5)])
        p.end()
        pm.save(path)
    return path.replace("\\", "/")


def _apply_theme(app) -> None:
    """Apply the Fusion base, a dark palette (so native popups/menus match), the
    Inter font with sensible fallbacks, and the refined stylesheet."""
    from PySide6 import QtGui, QtWidgets

    app.setStyle("Fusion")

    font = QtGui.QFont()
    font.setFamilies(["Inter", "Segoe UI", "Cantarell", "Noto Sans",
                      "Helvetica Neue", "Arial", "sans-serif"])
    font.setPointSize(10)
    app.setFont(font)

    def c(key):
        return QtGui.QColor(_C[key])
    pal = QtGui.QPalette()
    Role = QtGui.QPalette.ColorRole
    pal.setColor(Role.Window, c("bg"))
    pal.setColor(Role.WindowText, c("fg"))
    pal.setColor(Role.Base, c("input"))
    pal.setColor(Role.AlternateBase, c("surface"))
    pal.setColor(Role.Text, c("fg"))
    pal.setColor(Role.Button, c("surface"))
    pal.setColor(Role.ButtonText, c("fg"))
    pal.setColor(Role.ToolTipBase, c("surface"))
    pal.setColor(Role.ToolTipText, c("fg"))
    pal.setColor(Role.Highlight, c("accent"))
    pal.setColor(Role.HighlightedText, c("on_accent"))
    pal.setColor(Role.PlaceholderText, c("hint"))
    pal.setColor(QtGui.QPalette.ColorGroup.Disabled, Role.Text, c("hint"))
    app.setPalette(pal)

    app.setStyleSheet(_QSS.format(check=_checkmark_png(), chevron=_chevron_png(), **_C))


def _mic_icon(recording: bool = False):
    """Build the tray/window icon at runtime (no asset file): a microphone glyph
    on a rounded accent square; red while recording."""
    from PySide6 import QtCore, QtGui

    size = 64
    pm = QtGui.QPixmap(size, size)
    pm.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)

    bg = QtGui.QColor("#d23b3b") if recording else QtGui.QColor("#2d7d46")
    p.setBrush(bg)
    p.setPen(QtCore.Qt.NoPen)
    p.drawRoundedRect(2, 2, size - 4, size - 4, 14, 14)

    # Microphone, drawn in white.
    p.setPen(QtGui.QPen(QtGui.QColor("white"), 4))
    p.setBrush(QtGui.QColor("white"))
    body = QtCore.QRectF(size / 2 - 9, 14, 18, 26)
    p.drawRoundedRect(body, 9, 9)               # capsule
    p.setBrush(QtCore.Qt.NoBrush)
    p.drawArc(QtCore.QRectF(size / 2 - 15, 20, 30, 32), 180 * 16, 180 * 16)  # cradle
    p.drawLine(int(size / 2), 46, int(size / 2), 54)   # stem
    p.drawLine(int(size / 2 - 9), 54, int(size / 2 + 9), 54)  # base
    p.end()
    return QtGui.QIcon(pm)


def _build_settings_window():
    """Construct (but don't show) the settings window. Returns the QWidget."""
    from PySide6 import QtCore, QtWidgets

    from whisper_dictate import polish

    from PySide6.QtCore import Qt

    cfg = config.load_config()
    # A QDialog (not a plain top-level QWidget) so tiling compositors — notably
    # Pop!_OS COSMIC's auto-tiling — float it instead of stretching it to fill a
    # tile, which mangles the layout. SetFixedSize then locks it to its natural
    # size hint (non-resizable), reinforcing "float me" to the window manager.
    w = QtWidgets.QDialog()
    w.setWindowTitle("whisper-dictate — Settings")
    w.setWindowIcon(_mic_icon())

    root = QtWidgets.QVBoxLayout(w)
    root.setContentsMargins(24, 22, 24, 22)
    root.setSpacing(18)
    root.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetFixedSize)
    w.setMinimumWidth(460)

    # --- Header: icon chip + title/subtitle ---
    header = QtWidgets.QHBoxLayout()
    header.setSpacing(12)
    chip = QtWidgets.QLabel()
    chip.setPixmap(_mic_icon().pixmap(40, 40))
    chip.setFixedSize(40, 40)
    header.addWidget(chip, 0, Qt.AlignTop)
    htext = QtWidgets.QVBoxLayout()
    htext.setSpacing(2)
    title = QtWidgets.QLabel("whisper-dictate"); title.setObjectName("title")
    sub = QtWidgets.QLabel("Local dictation · translation · tone"); sub.setObjectName("subtitle")
    htext.addWidget(title); htext.addWidget(sub)
    header.addLayout(htext)
    header.addStretch(1)
    root.addLayout(header)

    def make_card(section_title: str):
        """A titled card: section label above a surface frame with a form grid."""
        wrap = QtWidgets.QVBoxLayout(); wrap.setSpacing(8)
        lbl = QtWidgets.QLabel(section_title.upper()); lbl.setObjectName("section")
        wrap.addWidget(lbl)
        card = QtWidgets.QFrame(); card.setObjectName("card")
        grid = QtWidgets.QGridLayout(card)
        grid.setContentsMargins(18, 16, 18, 16)
        grid.setHorizontalSpacing(16); grid.setVerticalSpacing(12)
        grid.setColumnStretch(1, 1)
        wrap.addWidget(card)
        root.addLayout(wrap)
        return grid

    def field(grid, row: int, label: str, widget):
        lab = QtWidgets.QLabel(label); lab.setObjectName("field")
        grid.addWidget(lab, row, 0, Qt.AlignVCenter)
        grid.addWidget(widget, row, 1)
        return widget

    # --- Transcription card ---
    g = make_card("Transcription")

    # Microphone picker. "System default" maps to "" so PortAudio picks whatever
    # the OS is currently using. Stored values are device *names* (not indices)
    # so they survive USB reconnects that shuffle PortAudio's numbering.
    mic_cb = QtWidgets.QComboBox()
    mic_cb.addItem("System default", "")
    try:
        import sounddevice as sd
        for d in sd.query_devices():
            if d.get("max_input_channels", 0) > 0:
                mic_cb.addItem(d["name"], d["name"])
    except Exception:  # noqa: BLE001 - no mic / no PortAudio -> just the default entry
        pass
    saved_mic = str(cfg.get("input_device", ""))
    mic_idx = mic_cb.findData(saved_mic)
    mic_cb.setCurrentIndex(mic_idx if mic_idx >= 0 else 0)
    field(g, 0, "Microphone", mic_cb)

    engine_cb = QtWidgets.QComboBox()
    engine_cb.addItems(["auto", "faster_whisper", "mlx"])
    engine_cb.setCurrentText(str(cfg.get("engine", "auto")))
    field(g, 1, "Engine", engine_cb)

    # Surface MLX availability so Apple-Silicon users know what "auto" will do.
    # The hint is only shown when MLX is actually importable on this machine —
    # on Linux/Windows it just stays empty.
    from whisper_dictate.transcriber import _is_mlx_available
    if _is_mlx_available():
        mlx_hint = QtWidgets.QLabel(
            "MLX (Apple Silicon GPU) detected — 'auto' will use it."
        )
        mlx_hint.setObjectName("hint")
        mlx_hint.setWordWrap(True)
        g.addWidget(mlx_hint, 2, 1)
        _row = 3
    else:
        _row = 2

    model_cb = QtWidgets.QComboBox(); model_cb.setEditable(True)
    model_cb.addItems(["tiny", "base", "small", "medium", "large-v3"])
    model_cb.setCurrentText(str(cfg["model"]))
    field(g, _row, "Whisper model", model_cb)

    device_cb = QtWidgets.QComboBox()
    device_cb.addItems(["auto", "cuda", "cpu"])
    device_cb.setCurrentText(str(cfg["device"]))
    field(g, _row + 1, "Device", device_cb)

    compute_cb = QtWidgets.QComboBox(); compute_cb.setEditable(True)
    compute_cb.addItems(["auto", "float16", "int8", "int8_float16"])
    compute_cb.setCurrentText(str(cfg["compute_type"]))
    field(g, _row + 2, "Compute type", compute_cb)

    lang_cb = QtWidgets.QComboBox(); lang_cb.setEditable(True)
    lang_cb.addItems(_SPOKEN_LANGS)
    lang_cb.setCurrentText(_LANG_AUTO if cfg["language"] == "" else str(cfg["language"]))
    field(g, _row + 3, "Spoken language", lang_cb)

    vad_chk = QtWidgets.QCheckBox("Drop silence (voice-activity filter)")
    vad_chk.setChecked(bool(cfg["vad"]))
    g.addWidget(vad_chk, _row + 4, 1)

    # Linux-only: ydotool/xdotool inter-keystroke delay. Irrelevant on macOS
    # (pynput) and Windows (pynput), so we hide the control there to keep the
    # form uncluttered. The saved value is still preserved across edits.
    key_delay_spin: "QtWidgets.QSpinBox | None" = None
    if sys.platform.startswith("linux"):
        key_delay_spin = QtWidgets.QSpinBox()
        key_delay_spin.setRange(1, 200)
        key_delay_spin.setSuffix(" ms")
        try:
            key_delay_spin.setValue(int(cfg.get("type_key_delay_ms", 12)))
        except (TypeError, ValueError):
            key_delay_spin.setValue(12)
        key_delay_spin.setToolTip(
            "Delay between synthesised keystrokes. Bump this if dictation drops "
            "spaces or letters in slow consumers (JetBrains terminals, Electron "
            "apps). Default 12 ms."
        )
        field(g, _row + 5, "Typing key delay", key_delay_spin)

    # --- Translate & tone card ---
    g2 = make_card("Translate & tone")

    translate_chk = QtWidgets.QCheckBox("Translate to")
    translate_cb = QtWidgets.QComboBox(); translate_cb.setEditable(True)
    translate_cb.addItems(_COMMON_LANGS)
    translate_cb.setCurrentText(str(cfg["translate_to"]) or "English")
    g2.addWidget(translate_chk, 0, 0)
    g2.addWidget(translate_cb, 0, 1)

    tone_chk = QtWidgets.QCheckBox("Apply tone")
    tone_cb = QtWidgets.QComboBox(); tone_cb.setEditable(True)
    tone_cb.addItems(sorted(STYLE_PRESETS))
    tone_cb.setCurrentText(str(cfg["style"]) or "professional")
    g2.addWidget(tone_chk, 1, 0)
    g2.addWidget(tone_cb, 1, 1)

    # Tone and translation are independent — either, both, or neither.
    translate_chk.setChecked(bool(cfg["translate_to"]))
    tone_chk.setChecked(bool(cfg["style"]))
    translate_cb.setEnabled(translate_chk.isChecked())
    tone_cb.setEnabled(tone_chk.isChecked())
    translate_chk.toggled.connect(translate_cb.setEnabled)
    tone_chk.toggled.connect(tone_cb.setEnabled)

    hint = QtWidgets.QLabel(
        "Tone works on its own — leave “Translate to” off to rewrite your words "
        "in the language you spoke."
    )
    hint.setObjectName("hint"); hint.setWordWrap(True)
    g2.addWidget(hint, 2, 0, 1, 2)

    model_edit = QtWidgets.QLineEdit(str(cfg["ollama_model"]))
    field(g2, 3, "Ollama model", model_edit)
    host_edit = QtWidgets.QLineEdit(str(cfg["ollama_host"]))
    host_edit.setPlaceholderText("http://localhost:11434 (default)")
    field(g2, 4, "Ollama host", host_edit)

    check_btn = QtWidgets.QPushButton("Check Ollama")
    status_lbl = QtWidgets.QLabel(""); status_lbl.setObjectName("hint"); status_lbl.setWordWrap(True)
    check_row = QtWidgets.QHBoxLayout(); check_row.setSpacing(10)
    check_row.addWidget(check_btn); check_row.addWidget(status_lbl, 1)
    g2.addLayout(check_row, 5, 0, 1, 2)

    def on_check():
        host = host_edit.text().strip() or None
        st, _detail = polish.diagnose(host)
        msgs = {"ok": "Ollama reachable", "not_installed": "Ollama not installed",
                "not_running": "Ollama installed but not running",
                "unreachable": "Ollama unreachable"}
        status_lbl.setText(("✓ " if st == "ok" else "✗ ") + msgs.get(st, st))
        status_lbl.setObjectName("status_ok" if st == "ok" else "status_bad")
        status_lbl.style().unpolish(status_lbl); status_lbl.style().polish(status_lbl)
    check_btn.clicked.connect(on_check)

    # --- Footer buttons ---
    btns = QtWidgets.QHBoxLayout()
    btns.addStretch(1)
    cancel_btn = QtWidgets.QPushButton("Cancel")
    save_btn = QtWidgets.QPushButton("Save"); save_btn.setObjectName("save")
    save_btn.setDefault(True)
    btns.addWidget(cancel_btn); btns.addWidget(save_btn)
    root.addLayout(btns)

    cancel_btn.clicked.connect(w.close)

    # Keep worker/dialog refs alive on the widget during a model download.
    w._pull_refs = None  # type: ignore[attr-defined]

    def collect() -> dict:
        lang = lang_cb.currentText().strip()
        # Preserve type_key_delay_ms when the control isn't shown (non-Linux), so
        # editing settings on macOS/Windows doesn't clobber a value set on Linux.
        key_delay = (key_delay_spin.value() if key_delay_spin is not None
                     else cfg.get("type_key_delay_ms", config.DEFAULTS["type_key_delay_ms"]))
        return {
            "input_device": mic_cb.currentData() or "",
            "engine": engine_cb.currentText().strip() or "auto",
            "model": model_cb.currentText().strip() or "large-v3",
            "device": device_cb.currentText().strip() or "auto",
            "compute_type": compute_cb.currentText().strip() or "auto",
            "language": "" if lang in ("", _LANG_AUTO) else lang,
            "vad": vad_chk.isChecked(),
            "translate_to": translate_cb.currentText().strip() if translate_chk.isChecked() else "",
            "style": tone_cb.currentText().strip() if tone_chk.isChecked() else "",
            "ollama_model": model_edit.text().strip() or config.DEFAULTS["ollama_model"],
            "ollama_host": host_edit.text().strip(),
            "type_key_delay_ms": key_delay,
        }

    def on_save():
        values = collect()
        config.save_config(values)
        needs_ollama = bool(values["translate_to"] or values["style"])
        host = values["ollama_host"] or None
        model = values["ollama_model"]

        if not needs_ollama:
            QtWidgets.QMessageBox.information(
                w, "Saved", "Settings saved. Your next dictation will use them.")
            w.close()
            return

        st, detail = polish.diagnose(host)
        if st != "ok":
            QtWidgets.QMessageBox.warning(
                w, "Ollama not ready",
                f"Settings saved, but translation/tone can't run yet:\n\n{detail}\n\n"
                "Until then, dictation types the raw transcript.")
            w.close()
            return
        if polish.model_installed(model, host):
            QtWidgets.QMessageBox.information(
                w, "Saved", "Settings saved. Your next dictation will use them.")
            w.close()
            return

        # Model missing — offer to download it with progress.
        if QtWidgets.QMessageBox.question(
            w, "Download model?",
            f"The model “{model}” needed for translation/tone isn't installed.\n"
            "Download it now? (Can be several GB.)",
        ) != QtWidgets.QMessageBox.Yes:
            w.close()
            return
        _start_download(w, model, host)

    save_btn.clicked.connect(on_save)
    return w


def _start_download(w, model: str, host):
    """Pull `model` on a worker thread, showing a progress dialog. Closes the
    settings window when the download succeeds."""
    from PySide6 import QtCore, QtWidgets

    from whisper_dictate import polish

    class Worker(QtCore.QObject):
        progress = QtCore.Signal(str)
        done = QtCore.Signal(str)  # "" on success, else error message

        def run(self):
            try:
                def on_prog(msg: dict):
                    total, comp = msg.get("total"), msg.get("completed")
                    if total and comp:
                        self.progress.emit(f"{msg.get('status', 'downloading')} "
                                           f"{int(comp * 100 / total)}%")
                    elif msg.get("status"):
                        self.progress.emit(msg["status"])
                polish.pull_model(model, host=host, on_progress=on_prog)
                self.done.emit("")
            except Exception as e:  # noqa: BLE001 - reported to the user
                self.done.emit(str(e))

    dlg = QtWidgets.QProgressDialog(f"Downloading {model}…", None, 0, 0, w)
    dlg.setWindowTitle("Downloading model")
    dlg.setCancelButton(None)
    dlg.setWindowModality(QtCore.Qt.WindowModal)
    dlg.setMinimumDuration(0)

    thread = QtCore.QThread()
    worker = Worker()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.progress.connect(lambda s: dlg.setLabelText(f"Downloading {model}\n{s}"))

    def finish(err: str):
        thread.quit(); thread.wait()
        dlg.close()
        if err:
            QtWidgets.QMessageBox.critical(
                w, "Download failed",
                f"Could not download “{model}”:\n{err}\n\nSettings are saved.")
        else:
            QtWidgets.QMessageBox.information(
                w, "Ready", f"“{model}” downloaded. Your next dictation will "
                "translate/restyle as configured.")
            w.close()

    worker.done.connect(finish)
    w._pull_refs = (thread, worker, dlg)  # type: ignore[attr-defined]
    thread.start()
    dlg.show()


def _ensure_app():
    """Return the running QApplication, creating one if needed. Sets the desktop
    file name so Wayland associates windows with the installed .desktop entry."""
    from PySide6 import QtWidgets

    app = QtWidgets.QApplication.instance()
    created = app is None
    if created:
        QtWidgets.QApplication.setDesktopFileName("whisper-dictate")
        app = QtWidgets.QApplication(sys.argv[:1])
        app.setApplicationName("whisper-dictate")
        _apply_theme(app)
    return app, created


def _instance_socket_name() -> str:
    """A per-user name for the single-instance IPC socket, so two users on the
    same machine each get their own tray (and never collide on the socket)."""
    import getpass

    try:
        who = getpass.getuser()
    except Exception:  # noqa: BLE001 - getuser can raise if no username is resolvable
        who = str(os.getuid()) if hasattr(os, "getuid") else "user"
    return f"whisper-dictate-tray-{who}"


def _signal_existing_instance(name: str) -> bool:
    """If a tray is already running, connect to its socket, ask it to surface its
    settings window, and return True. Return False if no instance is listening."""
    from PySide6 import QtNetwork

    sock = QtNetwork.QLocalSocket()
    sock.connectToServer(name)
    if not sock.waitForConnected(300):
        return False
    sock.write(b"show")
    sock.flush()
    sock.waitForBytesWritten(300)
    sock.disconnectFromServer()
    return True


def run_settings() -> int:
    """Open the settings window standalone (blocks until closed)."""
    try:
        from PySide6 import QtWidgets  # noqa: F401
    except Exception:  # noqa: BLE001 - PySide6 not installed
        print(_GUI_MISSING_MSG)
        return 1
    app, created = _ensure_app()
    w = _build_settings_window()
    w.show()
    if created:
        app.exec()
    return 0


def run_tray() -> int:
    """Run the system-tray application: a mic icon with Start/Stop, Settings, and
    Quit, plus a recording-state indicator. This is the long-running 'app'."""
    try:
        from PySide6 import QtCore, QtNetwork, QtWidgets
    except Exception:  # noqa: BLE001
        print(_GUI_MISSING_MSG)
        return 1

    from whisper_dictate import recorder

    app, _ = _ensure_app()
    app.setQuitOnLastWindowClosed(False)  # closing settings must not kill the tray

    # Single-instance guard: if a tray is already running, hand off to it (raising
    # its settings window) and exit, instead of adding a second duplicate icon.
    sock_name = _instance_socket_name()
    if _signal_existing_instance(sock_name):
        return 0

    if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
        print("No system tray detected on this desktop. Use `whisper-dictate "
              "settings` for the settings window instead.")
        return 1

    tray = QtWidgets.QSystemTrayIcon(_mic_icon())
    tray.setToolTip("whisper-dictate")
    menu = QtWidgets.QMenu()
    act_toggle = menu.addAction("Start / stop dictation")
    menu.addSeparator()
    act_settings = menu.addAction("Settings…")
    act_quit = menu.addAction("Quit")
    tray.setContextMenu(menu)

    state = {"window": None}

    def toggle_dictation():
        # Reuse the full CLI pipeline in a separate process so the tray stays
        # responsive and we don't load Whisper into the GUI process. Fixed argv
        # list (no shell), so there's nothing user-controlled to inject.
        import subprocess
        subprocess.Popen([sys.executable, "-m", "whisper_dictate", "toggle"])

    def open_settings():
        win = state["window"]
        if win is None:
            win = _build_settings_window()
            state["window"] = win
        win.show(); win.raise_(); win.activateWindow()

    act_toggle.triggered.connect(toggle_dictation)
    act_settings.triggered.connect(open_settings)
    act_quit.triggered.connect(app.quit)

    # Become the primary instance: listen on the IPC socket so later launches can
    # find us. removeServer clears a stale socket left by a crashed instance (we
    # only reach here after confirming no live instance answered above).
    QtNetwork.QLocalServer.removeServer(sock_name)
    server = QtNetwork.QLocalServer(app)
    server.listen(sock_name)

    def on_new_connection():
        # Any incoming connection is another launch asking us to surface the UI;
        # the payload is advisory, so we don't need to wait to read it.
        conn = server.nextPendingConnection()
        if conn is not None:
            conn.disconnected.connect(conn.deleteLater)
        open_settings()
    server.newConnection.connect(on_new_connection)

    def on_activated(reason):
        if reason == QtWidgets.QSystemTrayIcon.Trigger:       # left click
            toggle_dictation()
        elif reason == QtWidgets.QSystemTrayIcon.DoubleClick:
            open_settings()
    tray.activated.connect(on_activated)

    # Reflect recording state in the icon/tooltip/menu label.
    def refresh():
        rec = recorder.is_recording()
        tray.setIcon(_mic_icon(recording=rec))
        tray.setToolTip("whisper-dictate — recording…" if rec else "whisper-dictate")
        act_toggle.setText("Stop dictation && insert" if rec else "Start dictation")
    timer = QtCore.QTimer()
    timer.timeout.connect(refresh)
    timer.start(1000)
    refresh()

    tray.show()
    tray.showMessage("whisper-dictate", "Running in the tray. Click to dictate; "
                     "right-click for settings.", _mic_icon(), 4000)
    open_settings()  # launching the app shows settings; the icon stays in the tray
    return app.exec()
