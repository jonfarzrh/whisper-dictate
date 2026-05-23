"""`whisper-dictate init`: one-shot, OS-aware setup so install is painless.

Philosophy: fix everything that can be fixed *without* root automatically — the
user-level background services that keep typing and the model ready (ydotoold and
the warm-model server). Anything that genuinely needs root (installing system
libraries, granting /dev/uinput access) is printed as an exact command, or run
for you when you pass --yes.

It is safe to re-run: services are rewritten in place, checks are idempotent.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

OK = "\033[1;32m  ✓\033[0m"
FIX = "\033[1;36m  ⚙\033[0m"
TODO = "\033[1;33m  ▸\033[0m"
WARN = "\033[1;33m  !\033[0m"


def _say(marker: str, msg: str) -> None:
    print(f"{marker} {msg}")


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _portaudio_ok() -> bool:
    """True if sounddevice can load — i.e. the system PortAudio library is present."""
    try:
        import sounddevice  # noqa: F401
        return True
    except Exception:
        return False


def _pynput_ok() -> bool:
    try:
        import pynput  # noqa: F401
        return True
    except Exception:
        return False


def run_init(model: str | None = None, with_server: bool = True, assume_yes: bool = False) -> int:
    """Entry point for `whisper-dictate init`. Returns 0 if fully ready, 1 if
    manual (root) steps remain.

    ``model`` is normally None: the warm-model service is installed as a plain
    ``serve`` that reads the model from saved settings, so the settings window is
    the single source of truth and re-running init isn't needed after changing the
    model. Pass an explicit model only to pin the daemon to one regardless of
    settings."""
    print("whisper-dictate init\n")
    if sys.platform.startswith("linux"):
        return _init_linux(model, with_server, assume_yes)
    if sys.platform == "darwin":
        return _init_macos(model, with_server, assume_yes)
    if sys.platform == "win32":
        return _init_windows(model, with_server, assume_yes)
    print(f"Unsupported platform: {sys.platform}")
    return 1


def _model_note(model: str | None) -> str:
    """How to describe the warm-model service's model in init output."""
    if model:
        return f"model: {model}"
    from whisper_dictate import config
    return f"follows your saved settings: {config.load_config()['model']}"


# --------------------------------------------------------------------------- #
# systemd --user helpers (Linux)
# --------------------------------------------------------------------------- #

def _user_unit_dir() -> Path:
    d = Path.home() / ".config" / "systemd" / "user"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _systemctl_user(*args: str) -> bool:
    try:
        return subprocess.run(["systemctl", "--user", *args]).returncode == 0
    except FileNotFoundError:
        return False


def _install_user_service(name: str, description: str, exec_start: str) -> None:
    unit = (
        "[Unit]\n"
        f"Description={description}\n"
        "After=graphical-session.target\n\n"
        "[Service]\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    (_user_unit_dir() / name).write_text(unit)
    _systemctl_user("daemon-reload")
    _systemctl_user("enable", "--now", name)


def _remove_user_service(name: str) -> bool:
    """Stop, disable, and delete a systemd --user unit. Returns True if it existed."""
    unit = _user_unit_dir() / name
    existed = unit.exists()
    _systemctl_user("disable", "--now", name)
    unit.unlink(missing_ok=True)
    _systemctl_user("daemon-reload")
    return existed


# --------------------------------------------------------------------------- #
# Linux
# --------------------------------------------------------------------------- #

def _init_linux(model: str, with_server: bool, assume_yes: bool) -> int:
    session = os.environ.get("XDG_SESSION_TYPE", "unknown")
    print(f"Platform: Linux ({session} session)\n")

    todo: list[str] = []
    missing_pkgs: list[str] = []

    # 1. PortAudio (recording)
    if _portaudio_ok():
        _say(OK, "PortAudio present — audio capture works")
    else:
        _say(TODO, "PortAudio missing — recording would crash on start")
        missing_pkgs.append("libportaudio2")

    # 2. Input/typing tool
    if session == "wayland":
        if _have("ydotool"):
            _say(OK, "ydotool present (works on Wayland)")
        else:
            _say(TODO, "ydotool missing — required to type on Wayland")
            missing_pkgs.append("ydotool")
        if not _have("ydotoold"):
            _say(TODO, "ydotoold (daemon) missing")
            missing_pkgs.append("ydotoold")
    else:
        if _have("ydotool") or _have("xdotool"):
            _say(OK, f"input tool present ({'ydotool' if _have('ydotool') else 'xdotool'})")
        else:
            _say(TODO, "no input tool — install xdotool (X11) or ydotool")
            missing_pkgs.append("xdotool")

    # 3. notify-send (optional feedback)
    if _have("notify-send"):
        _say(OK, "notify-send present — on-screen feedback enabled")
    else:
        _say(WARN, "notify-send missing — no on-screen feedback (optional)")
        missing_pkgs.append("libnotify-bin")

    # 4. System packages: install or instruct
    if missing_pkgs:
        pkgs = list(dict.fromkeys(missing_pkgs))
        mgr = _linux_pkg_install_cmd(pkgs)
        if mgr is None:
            _say(TODO, f"install with your package manager: {', '.join(pkgs)}")
            todo.append(f"install: {', '.join(pkgs)}")
        elif assume_yes:
            _say(FIX, f"installing system packages: {mgr}")
            subprocess.run(mgr, shell=True)
            todo.append("re-run `whisper-dictate init` to verify the installs")
        else:
            _say(TODO, f"run this (needs sudo):\n        {mgr}")
            todo.append(mgr)

    # 5. /dev/uinput access (ydotool only)
    if _have("ydotool"):
        if os.access("/dev/uinput", os.R_OK | os.W_OK):
            _say(OK, "/dev/uinput is accessible")
        else:
            _say(TODO, "no /dev/uinput access — run: sudo usermod -aG input $USER  (then log out/in)")
            todo.append("sudo usermod -aG input $USER   # then log out and back in")

    # 6. ydotoold user service
    if _have("ydotoold"):
        ydotoold = shutil.which("ydotoold") or "/usr/bin/ydotoold"
        _install_user_service(
            "ydotoold.service",
            "ydotool daemon",
            f"{ydotoold} --socket-path=%h/.ydotool_socket --socket-own=%U:%U",
        )
        _say(FIX, "ydotoold user service installed and started")

    # 7. Desktop launcher so the tray app shows up in the applications menu,
    #    and an autostart entry so the tray icon is there at next login without
    #    the user having to click anything.
    _install_desktop_entry()
    _say(FIX, "desktop launcher installed (find “whisper-dictate” in your app menu)")
    _install_autostart_entry()
    _say(FIX, "tray autostart enabled (the mic icon will appear at next login)")

    # 8. Warm-model server service (autoloads the model)
    if with_server:
        model_flag = f" --model {model}" if model else ""
        _install_user_service(
            "whisper-dictate-server.service",
            "whisper-dictate warm-model daemon",
            f"{sys.executable} -m whisper_dictate serve{model_flag}",
        )
        _say(FIX, f"warm-model daemon installed and started ({_model_note(model)}) — first load takes a few seconds")

    return _finish(todo, assume_yes)


def _launcher_exec() -> str:
    """The command a desktop launcher should run to open the tray app. Prefer the
    installed console script; fall back to running the module with this Python."""
    exe = shutil.which("whisper-dictate")
    return f"{exe} tray" if exe else f"{sys.executable} -m whisper_dictate tray"


def _desktop_entry_body() -> str:
    """The .desktop entry shared by the app-menu launcher and the autostart
    entry — same launch command in both, so clicking the menu icon and the
    autostart firing at login do the exact same thing."""
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=whisper-dictate",
        "GenericName=Dictation",
        "Comment=Local speech-to-text with translation and tone",
        "Exec=" + _launcher_exec(),
        "Icon=audio-input-microphone",
        "Terminal=false",
        "Categories=Utility;AudioVideo;Accessibility;",
        "Keywords=dictation;speech;transcribe;whisper;translate;",
        "StartupNotify=false",
    ]
    return "\n".join(lines) + "\n"


def _install_desktop_entry() -> None:
    """Write a freedesktop .desktop entry so the tray app appears in the
    application menu (and can be pinned/launched like any other app)."""
    apps = Path.home() / ".local" / "share" / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    (apps / "whisper-dictate.desktop").write_text(_desktop_entry_body())


def _install_autostart_entry() -> None:
    """Write a freedesktop autostart entry so the tray app launches at every
    desktop session start. GNOME respects X-GNOME-Autostart-enabled; KDE and
    most other DEs read autostart/ regardless."""
    auto = Path.home() / ".config" / "autostart"
    auto.mkdir(parents=True, exist_ok=True)
    body = _desktop_entry_body().rstrip() + "\nX-GNOME-Autostart-enabled=true\n"
    (auto / "whisper-dictate.desktop").write_text(body)


def _linux_pkg_install_cmd(pkgs: list[str]) -> str | None:
    joined = " ".join(pkgs)
    if _have("apt") or _have("apt-get"):
        return f"sudo apt install -y {joined}"
    if _have("dnf"):
        return f"sudo dnf install -y {joined}"
    if _have("pacman"):
        return f"sudo pacman -S --noconfirm {joined}"
    if _have("zypper"):
        return f"sudo zypper install -y {joined}"
    return None


# --------------------------------------------------------------------------- #
# macOS
# --------------------------------------------------------------------------- #

def _init_macos(model: str, with_server: bool, assume_yes: bool = False) -> int:
    print("Platform: macOS\n")
    todo: list[str] = []

    if _portaudio_ok():
        _say(OK, "audio capture works (PortAudio ships in the sounddevice wheel)")
    else:
        _say(TODO, "PortAudio unavailable — try: brew install portaudio")
        todo.append("brew install portaudio")

    if _pynput_ok():
        _say(OK, "pynput present — typing backend ready")
    else:
        _say(TODO, "pynput missing — reinstall with the macos extra: uv tool install '.[macos]'")
        todo.append("uv tool install '.[macos]'")

    _say(TODO, "grant Accessibility permission to your launcher (Terminal/iTerm/Raycast) in "
               "System Settings → Privacy & Security → Accessibility")
    todo.append("grant Accessibility permission (System Settings → Privacy & Security)")

    if with_server:
        _setup_launchd_agent(model)
        _say(FIX, f"warm-model launchd agent installed and loaded ({_model_note(model)})")

    # App-menu launcher + tray autostart so the user gets a clickable entry
    # in Applications/Launchpad *and* the mic icon appears at login.
    _install_macos_app_bundle()
    _say(FIX, "Whisper Dictate.app installed in ~/Applications (Launchpad/Spotlight)")
    _setup_launchd_tray()
    _say(FIX, "tray launchd agent installed and loaded (mic icon at login)")

    return _finish(todo, assume_yes)


_MACOS_APP_DIR = Path.home() / "Applications" / "Whisper Dictate.app"
_MACOS_TRAY_LABEL = "ai.whisperdictate.tray"
_MACOS_SERVER_LABEL = "ai.whisperdictate.server"


def _macos_tray_exec() -> Path:
    """Path to the .app bundle's executable — what launchd runs and what
    Launchpad/Finder invokes when the user clicks the app icon."""
    return _MACOS_APP_DIR / "Contents" / "MacOS" / "whisper-dictate-tray"


def _install_macos_app_bundle() -> None:
    """Write a minimal .app bundle to ~/Applications so the tray app appears
    in Launchpad/Spotlight/Finder. The Info.plist sets LSUIElement=true so the
    bundle runs as a menu-bar-only app (no Dock icon); the inner executable is
    a shell script that execs the installed ``whisper-dictate tray`` command."""
    macos_dir = _MACOS_APP_DIR / "Contents" / "MacOS"
    macos_dir.mkdir(parents=True, exist_ok=True)
    exe = shutil.which("whisper-dictate") or f"{sys.executable} -m whisper_dictate"
    script = _macos_tray_exec()
    script.write_text("#!/bin/sh\nexec " + exe + " tray\n")
    script.chmod(0o755)
    info = _MACOS_APP_DIR / "Contents" / "Info.plist"
    info.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "  <dict>\n"
        "    <key>CFBundleName</key><string>Whisper Dictate</string>\n"
        "    <key>CFBundleDisplayName</key><string>Whisper Dictate</string>\n"
        "    <key>CFBundleIdentifier</key><string>ai.whisperdictate.app</string>\n"
        "    <key>CFBundleExecutable</key><string>whisper-dictate-tray</string>\n"
        "    <key>CFBundlePackageType</key><string>APPL</string>\n"
        "    <key>CFBundleVersion</key><string>1.0</string>\n"
        "    <key>CFBundleShortVersionString</key><string>1.0</string>\n"
        "    <key>LSUIElement</key><true/>\n"
        "  </dict>\n"
        "</plist>\n"
    )


def _setup_launchd_tray() -> None:
    """Install a launchd agent that runs the .app bundle's executable at login,
    so the tray icon is always present after sign-in. KeepAlive is False — if
    the user quits the tray from its menu, it stays quit until next login."""
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist = plist_dir / f"{_MACOS_TRAY_LABEL}.plist"
    exe = str(_macos_tray_exec())
    plist.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "  <dict>\n"
        f"    <key>Label</key><string>{_MACOS_TRAY_LABEL}</string>\n"
        "    <key>ProgramArguments</key>\n"
        f"    <array>\n        <string>{exe}</string>\n    </array>\n"
        "    <key>RunAtLoad</key><true/>\n"
        "    <key>KeepAlive</key><false/>\n"
        "  </dict>\n"
        "</plist>\n"
    )
    subprocess.run(["launchctl", "unload", str(plist)], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["launchctl", "load", "-w", str(plist)], check=False)


def _setup_launchd_agent(model: str | None) -> None:
    label = "ai.whisperdictate.server"
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist = plist_dir / f"{label}.plist"
    argv = [sys.executable, "-m", "whisper_dictate", "serve"]
    if model:
        argv += ["--model", model]
    args = "".join(f"        <string>{a}</string>\n" for a in argv)
    plist.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "  <dict>\n"
        f"    <key>Label</key><string>{label}</string>\n"
        "    <key>ProgramArguments</key>\n"
        f"    <array>\n{args}    </array>\n"
        "    <key>RunAtLoad</key><true/>\n"
        "    <key>KeepAlive</key><true/>\n"
        "  </dict>\n"
        "</plist>\n"
    )
    # Reload if already present, then load.
    subprocess.run(["launchctl", "unload", str(plist)], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["launchctl", "load", "-w", str(plist)], check=False)


# --------------------------------------------------------------------------- #
# Windows
# --------------------------------------------------------------------------- #

def _init_windows(model: str, with_server: bool, assume_yes: bool = False) -> int:
    print("Platform: Windows\n")
    todo: list[str] = []

    if _portaudio_ok():
        _say(OK, "audio capture works (PortAudio ships in the sounddevice wheel)")
    else:
        _say(TODO, "PortAudio unavailable — reinstall whisper-dictate")
        todo.append("reinstall whisper-dictate")

    if _pynput_ok():
        _say(OK, "pynput present — typing backend ready")
    else:
        _say(TODO, "pynput missing — reinstall with the windows extra: uv tool install '.[windows]'")
        todo.append("uv tool install '.[windows]'")

    exe = shutil.which("whisper-dictate") or "whisper-dictate"

    if with_server:
        # Register a per-user logon scheduled task so the model autoloads at sign-in.
        serve_cmd = f"{exe} serve" + (f" --model {model}" if model else "")
        cmd = (
            f'schtasks /Create /TN "whisper-dictate-server" /SC ONLOGON /F '
            f'/TR "{serve_cmd}"'
        )
        rc = subprocess.run(cmd, shell=True).returncode
        if rc == 0:
            _say(FIX, f"warm-model logon task registered ({_model_note(model)})")
            _say(TODO, 'start it now without re-logging in: schtasks /Run /TN "whisper-dictate-server"')
        else:
            _say(WARN, "could not register the logon task; start the daemon manually: whisper-dictate serve")
            todo.append("run `whisper-dictate serve` at logon (Task Scheduler)")

    # Start Menu launcher + tray autostart so the user gets a clickable entry
    # in the Start Menu and the mic icon appears at every sign-in.
    if _install_windows_start_menu_shortcut(exe):
        _say(FIX, "Start Menu shortcut installed (search “whisper-dictate”)")
    else:
        _say(WARN, "could not create the Start Menu shortcut (PowerShell unavailable?)")
    rc = subprocess.run(
        f'schtasks /Create /TN "whisper-dictate-tray" /SC ONLOGON /F /TR "{exe} tray"',
        shell=True,
    ).returncode
    if rc == 0:
        _say(FIX, "tray logon task registered (mic icon at sign-in)")
        _say(TODO, 'start it now without re-logging in: schtasks /Run /TN "whisper-dictate-tray"')
    else:
        _say(WARN, "could not register the tray logon task")
        todo.append("run `whisper-dictate tray` at logon (Task Scheduler)")

    return _finish(todo, assume_yes)


def _windows_start_menu_path() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "whisper-dictate.lnk"


def _install_windows_start_menu_shortcut(exe: str) -> bool:
    """Create a Start Menu .lnk pointing at ``whisper-dictate tray``. Uses the
    WScript.Shell COM object via PowerShell so we don't need to add a Python
    dependency just for shortcut creation. Returns True on success."""
    lnk = _windows_start_menu_path()
    lnk.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "$s = (New-Object -ComObject WScript.Shell)"
        f".CreateShortcut('{lnk}');"
        f"$s.TargetPath = '{exe}';"
        "$s.Arguments = 'tray';"
        f"$s.WorkingDirectory = '{Path.home()}';"
        "$s.IconLocation = 'shell32.dll,138';"
        "$s.Description = 'whisper-dictate tray (start/stop dictation)';"
        "$s.Save()"
    )
    rc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode
    return rc == 0


# --------------------------------------------------------------------------- #

def run_deinit() -> int:
    """Entry point for `whisper-dictate deinit`: tear down everything `init` created
    on this OS (user-level services + runtime state). Leaves system packages and
    the tool install alone — those are the user's to remove."""
    print("whisper-dictate deinit\n")
    if sys.platform.startswith("linux"):
        _deinit_linux()
    elif sys.platform == "darwin":
        _deinit_macos()
    elif sys.platform == "win32":
        _deinit_windows()
    else:
        print(f"Unsupported platform: {sys.platform}")

    _clean_state()

    print("\nRemoved the warm-model service and runtime state.")
    print("Left in place (remove yourself if you want):")
    print("  - the tool itself:        uv tool uninstall whisper-dictate")
    print("  - downloaded models:      rm -rf ~/.cache/huggingface/hub/models--Systran--*")
    print("  - system packages (libportaudio2, ydotool, …): via your package manager")
    return 0


def _deinit_linux() -> None:
    for svc in ("whisper-dictate-server.service", "ydotoold.service"):
        if _remove_user_service(svc):
            _say(FIX, f"removed and stopped {svc}")
        else:
            _say(OK, f"{svc} was not installed")
    for label, path in (
        ("desktop launcher",
         Path.home() / ".local" / "share" / "applications" / "whisper-dictate.desktop"),
        ("tray autostart entry",
         Path.home() / ".config" / "autostart" / "whisper-dictate.desktop"),
    ):
        if path.exists():
            path.unlink(missing_ok=True)
            _say(FIX, f"removed the {label}")


def _deinit_macos() -> None:
    for label, plist_name in (
        ("warm-model launchd agent", f"{_MACOS_SERVER_LABEL}.plist"),
        ("tray launchd agent", f"{_MACOS_TRAY_LABEL}.plist"),
    ):
        plist = Path.home() / "Library" / "LaunchAgents" / plist_name
        if plist.exists():
            subprocess.run(["launchctl", "unload", str(plist)], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            plist.unlink(missing_ok=True)
            _say(FIX, f"removed and unloaded the {label}")
        else:
            _say(OK, f"{label} was not installed")
    if _MACOS_APP_DIR.exists():
        shutil.rmtree(_MACOS_APP_DIR, ignore_errors=True)
        _say(FIX, "removed Whisper Dictate.app from ~/Applications")


def _deinit_windows() -> None:
    for label, task in (
        ("warm-model logon task", "whisper-dictate-server"),
        ("tray logon task", "whisper-dictate-tray"),
    ):
        rc = subprocess.run(
            f'schtasks /Delete /TN "{task}" /F',
            shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode
        if rc == 0:
            _say(FIX, f"removed the {label}")
        else:
            _say(OK, f"{label} was not present")
    lnk = _windows_start_menu_path()
    if lnk.exists():
        lnk.unlink(missing_ok=True)
        _say(FIX, "removed the Start Menu shortcut")


def _clean_state() -> None:
    """Remove leftover runtime files (pid, stop sentinel, recording, socket)."""
    from whisper_dictate import recorder
    from whisper_dictate.server import socket_path

    for f in (recorder.pid_file(), recorder.stop_file(), recorder.audio_file(), socket_path()):
        if f.exists():
            try:
                f.unlink()
                _say(FIX, f"removed runtime file {f.name}")
            except OSError:
                pass


def _pull_with_progress(model: str, host: str | None) -> None:
    """Pull an Ollama model, printing coarse (~10%) progress so a one-time init
    doesn't spam a line per percent."""
    from whisper_dictate import polish

    last_bucket = [-1]

    def on_prog(msg: dict) -> None:
        total, done = msg.get("total"), msg.get("completed")
        if total and done:
            bucket = int(done * 10 / total)
            if bucket != last_bucket[0]:
                last_bucket[0] = bucket
                print(f"        {msg.get('status', 'downloading')} {bucket * 10}%")
        elif msg.get("status"):
            print(f"        {msg['status']}")

    polish.pull_model(model, host=host, on_progress=on_prog)


def _check_ollama(todo: list[str], assume_yes: bool) -> None:
    """If saved settings enable translation/tone, verify the Ollama side: server
    reachable + the configured model present. Pulls the model with --yes, else
    records it as a manual step. A no-op when neither feature is configured, so
    plain dictation setups never see Ollama mentioned."""
    from whisper_dictate import config, polish

    cfg = config.load_config()
    translate, style = cfg.get("translate_to"), cfg.get("style")
    if not (translate or style):
        return  # translation/tone off -> Ollama isn't needed

    feat = " + ".join(f for f in (
        "translation" if translate else None,
        "tone" if style else None,
    ) if f)
    model = cfg.get("ollama_model") or polish.DEFAULT_MODEL
    host = cfg.get("ollama_host") or None

    status, detail = polish.diagnose(host)
    if status != "ok":
        # `detail` is a short multi-line message ending with the fix (install
        # command or how to start the server). Show it, and record a one-line TODO.
        _say(TODO, f"{feat} is enabled but Ollama isn't ready:")
        for line in detail.splitlines():
            print(f"        {line}")
        if status == "not_installed":
            todo.append(f"install Ollama: {polish.install_hint()}")
        elif status == "not_running":
            todo.append("start Ollama (`ollama serve`, or launch the app)")
        else:
            todo.append(f"make Ollama reachable at {host or polish.default_host()}")
        return

    _say(OK, "Ollama reachable")
    if polish.model_installed(model, host):
        _say(OK, f"Ollama model present: {model} (for {feat})")
        return

    if assume_yes:
        _say(FIX, f"pulling Ollama model {model} for {feat} (can be several GB)...")
        try:
            _pull_with_progress(model, host)
            _say(OK, f"Ollama model downloaded: {model}")
        except Exception as e:  # noqa: BLE001 - report and leave it as a manual step
            _say(WARN, f"could not pull {model}: {e}")
            todo.append(f"download the model: ollama pull {model}")
    else:
        _say(TODO, f"Ollama model '{model}' not installed (needed for {feat})")
        todo.append(f"download it: re-run `whisper-dictate init --yes`, "
                    f"or `ollama pull {model}`, or use `whisper-dictate settings`")


def _finish(todo: list[str], assume_yes: bool = False) -> int:
    _check_ollama(todo, assume_yes)

    print("\nVerification:")
    try:
        from whisper_dictate.backends import get_backend
        backend = get_backend()
        ok, msg = backend.check()
        print(f"  backend: {backend.name} — {'OK' if ok else 'NOT OK'}")
        print(f"  {msg}")
    except Exception as e:  # noqa: BLE001
        print(f"  backend check failed: {e}")

    try:
        from whisper_dictate.transcriber import detect_acceleration
        accel = detect_acceleration()
        print(f"  transcription: device={accel['device']} ({accel['compute_type']})")
        if accel["hints"]:
            print(f"    hint: {accel['hints'][0]}")
    except Exception as e:  # noqa: BLE001
        print(f"  acceleration check failed: {e}")

    if todo:
        print("\nManual steps still needed:")
        for t in todo:
            print(f"  - {t}")
        print("\nFinish those, then re-run `whisper-dictate init`.")
        return 1

    print("\nAll set ✓  Bind `whisper-dictate` to a global hotkey and start dictating.")
    return 0
