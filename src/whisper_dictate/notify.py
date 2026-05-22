"""Best-effort cross-platform desktop notifications.

Each platform has a native, dependency-free notifier:
- Linux:   notify-send (libnotify) if present
- macOS:   osascript "display notification" (built into macOS)
- Windows: a .NET NotifyIcon balloon via PowerShell (built into Windows 10/11)

Everything is wrapped so a failure (or an unknown platform) falls back to
stderr rather than raising — notifications are never load-bearing.
"""
from __future__ import annotations

import shutil
import subprocess
import sys

TITLE = "whisper-dictate"


def notify(msg: str) -> None:
    try:
        if sys.platform.startswith("linux"):
            _notify_linux(msg)
        elif sys.platform == "darwin":
            _notify_macos(msg)
        elif sys.platform == "win32":
            _notify_windows(msg)
        else:
            _fallback(msg)
    except Exception:  # noqa: BLE001 - notifications must never break dictation
        _fallback(msg)


def _fallback(msg: str) -> None:
    print(f"{TITLE}: {msg}", file=sys.stderr)


def _notify_linux(msg: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-t", "1500", TITLE, msg], check=False)
    else:
        _fallback(msg)


def _notify_macos(msg: str) -> None:
    # AppleScript string literals use double quotes; escape backslashes then quotes.
    safe = msg.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(
        ["osascript", "-e", f'display notification "{safe}" with title "{TITLE}"'],
        check=False,
    )


def _notify_windows(msg: str) -> None:
    # Non-blocking balloon tip. The PowerShell process sleeps and disposes the
    # icon itself, so we launch it detached and don't wait — otherwise dictation
    # would stall ~1.5s on every notification.
    safe = msg.replace("'", "''")  # PowerShell single-quote escaping
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$n=New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon=[System.Drawing.SystemIcons]::Information;"
        "$n.Visible=$true;"
        f"$n.ShowBalloonTip(1500,'{TITLE}','{safe}',"
        "[System.Windows.Forms.ToolTipIcon]::Info);"
        "Start-Sleep -Milliseconds 1800;$n.Dispose()"
    )
    creationflags = getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
