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


def run_init(model: str = "large-v3", with_server: bool = True, assume_yes: bool = False) -> int:
    """Entry point for `whisper-dictate init`. Returns 0 if fully ready, 1 if
    manual (root) steps remain."""
    print("whisper-dictate init\n")
    if sys.platform.startswith("linux"):
        return _init_linux(model, with_server, assume_yes)
    if sys.platform == "darwin":
        return _init_macos(model, with_server)
    if sys.platform == "win32":
        return _init_windows(model, with_server)
    print(f"Unsupported platform: {sys.platform}")
    return 1


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

    # 7. Warm-model server service (autoloads the model)
    if with_server:
        _install_user_service(
            "whisper-dictate-server.service",
            "whisper-dictate warm-model daemon",
            f"{sys.executable} -m whisper_dictate serve --model {model}",
        )
        _say(FIX, f"warm-model daemon installed and started (model: {model}) — first load takes a few seconds")

    return _finish(todo)


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

def _init_macos(model: str, with_server: bool) -> int:
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
        _say(FIX, f"warm-model launchd agent installed and loaded (model: {model})")

    return _finish(todo)


def _setup_launchd_agent(model: str) -> None:
    label = "ai.whisperdictate.server"
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist = plist_dir / f"{label}.plist"
    args = "".join(
        f"        <string>{a}</string>\n"
        for a in (sys.executable, "-m", "whisper_dictate", "serve", "--model", model)
    )
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

def _init_windows(model: str, with_server: bool) -> int:
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

    if with_server:
        # Register a per-user logon scheduled task so the model autoloads at sign-in.
        exe = shutil.which("whisper-dictate") or "whisper-dictate"
        cmd = (
            f'schtasks /Create /TN "whisper-dictate-server" /SC ONLOGON /F '
            f'/TR "{exe} serve --model {model}"'
        )
        rc = subprocess.run(cmd, shell=True).returncode
        if rc == 0:
            _say(FIX, f"warm-model logon task registered (model: {model})")
            _say(TODO, 'start it now without re-logging in: schtasks /Run /TN "whisper-dictate-server"')
        else:
            _say(WARN, "could not register the logon task; start the daemon manually: whisper-dictate serve")
            todo.append("run `whisper-dictate serve` at logon (Task Scheduler)")

    return _finish(todo)


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


def _deinit_macos() -> None:
    plist = Path.home() / "Library" / "LaunchAgents" / "ai.whisperdictate.server.plist"
    if plist.exists():
        subprocess.run(["launchctl", "unload", str(plist)], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        plist.unlink(missing_ok=True)
        _say(FIX, "removed and unloaded the warm-model launchd agent")
    else:
        _say(OK, "warm-model launchd agent was not installed")


def _deinit_windows() -> None:
    rc = subprocess.run(
        'schtasks /Delete /TN "whisper-dictate-server" /F',
        shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode
    if rc == 0:
        _say(FIX, "removed the warm-model logon task")
    else:
        _say(OK, "warm-model logon task was not present")


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


def _finish(todo: list[str]) -> int:
    print("\nVerification:")
    try:
        from whisper_dictate.backends import get_backend
        backend = get_backend()
        ok, msg = backend.check()
        print(f"  backend: {backend.name} — {'OK' if ok else 'NOT OK'}")
        print(f"  {msg}")
    except Exception as e:  # noqa: BLE001
        print(f"  backend check failed: {e}")

    if todo:
        print("\nManual steps still needed:")
        for t in todo:
            print(f"  - {t}")
        print("\nFinish those, then re-run `whisper-dictate init`.")
        return 1

    print("\nAll set ✓  Bind `whisper-dictate` to a global hotkey and start dictating.")
    return 0
