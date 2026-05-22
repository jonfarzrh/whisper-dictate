# whisper-dictate

Local Whisper-powered dictation that types into any application. Cross-platform (Linux/macOS/Windows), GPU-accelerated via faster-whisper.

## Installation

Every platform needs [uv](https://docs.astral.sh/uv/) and Python ≥ 3.11. Follow the section for your platform — each ends with verifying setup and binding a hotkey. For NVIDIA GPU acceleration, see [GPU acceleration](#gpu-acceleration).

### Linux — Wayland (Pop!_OS COSMIC, GNOME 4x, KDE Plasma 6)

1. **System libraries.** Recording needs PortAudio; typing on Wayland needs ydotool *and* its daemon (separate packages on Debian/Ubuntu):

       sudo apt install libportaudio2 ydotool ydotoold
       sudo apt install libnotify-bin          # optional: on-screen 🎙️/✓ feedback

2. **Install the tool** (CPU-only — for GPU add the `--with` flags from [GPU acceleration](#gpu-acceleration)):

       uv tool install whisper-dictate

3. **Set up the ydotool daemon** so synthetic typing reaches native Wayland windows. Use the helper script from this repo:

       ./setup-ydotool.sh

   It installs a `ydotoold` systemd user service, sets `YDOTOOL_SOCKET`, and reports whether you need to join the `input` group. (On Pop!_OS a uinput udev ACL usually grants access already — no relogin needed. Check with `getfacl /dev/uinput`.)

   > **ydotool 0.1.8 socket quirk:** that version's daemon ignores `--socket-path` and always listens on `/tmp/.ydotool_socket`, so clients must use `export YDOTOOL_SOCKET="/tmp/.ydotool_socket"`. The script handles this for you.

4. **(Recommended) Keep the model warm** so each press is ~0.3s instead of ~3s:

       ./setup-server.sh          # systemd user service running `whisper-dictate serve`

5. **Verify and bind a hotkey:**

       whisper-dictate check      # expect: OK (ydotool on wayland)

   COSMIC: **Settings → Keyboard → Custom Shortcuts**, command `whisper-dictate` (use the full path from `which whisper-dictate` if COSMIC can't resolve it), bound to e.g. `Super+Space`. If transcripts come out empty/garbled, your mic gain is likely too low — raise it: `pactl set-source-volume @DEFAULT_SOURCE@ 100%`.

### Linux — X11 (Xorg GNOME, i3, XFCE, …)

Same as Wayland but with **xdotool** (no daemon or socket needed):

    sudo apt install libportaudio2 xdotool
    uv tool install whisper-dictate            # add the GPU --with flags if applicable
    whisper-dictate check                      # expect: OK (xdotool on x11)

Optionally run `./setup-server.sh` for the warm-model daemon, then bind `whisper-dictate` to a hotkey in your DE/WM.

### macOS

PortAudio ships *inside* the `sounddevice` wheel, so there's no Homebrew step.

    uv tool install 'whisper-dictate[macos]'   # the [macos] extra pulls in pynput for typing

On first use, grant **Accessibility** permission to whatever launches the CLI (Terminal, iTerm, Raycast…) under **System Settings → Privacy & Security → Accessibility**. Transcription runs on CPU — start with `--model small` if `large-v3` feels slow.

Bind a hotkey with Raycast, Hammerspoon, or Shortcuts.app. To keep the model warm, run `whisper-dictate serve` (e.g. as a `launchd` agent).

### Windows

PortAudio ships inside the `sounddevice` wheel.

    uv tool install 'whisper-dictate[windows]' # the [windows] extra pulls in pynput for typing

For an NVIDIA GPU, add the `--with` flags from [GPU acceleration](#gpu-acceleration). Bind a hotkey with PowerToys, AutoHotkey, or a `.lnk` shortcut; run `whisper-dictate serve` in the background to keep the model warm.

### From a local wheel (any platform)

    uv build
    uv tool install dist/whisper_dictate-*.whl

## Usage

    whisper-dictate              # toggle: start, then stop+type
    whisper-dictate start        # explicit start
    whisper-dictate stop         # explicit stop+transcribe+type
    whisper-dictate check        # diagnose platform setup
    whisper-dictate transcribe FILE.wav   # transcribe a file to stdout
    whisper-dictate serve        # warm-model daemon (keeps the model in memory)

Bind `whisper-dictate` (no args = toggle) to a global hotkey in your OS settings.

When a `serve` daemon is running, the toggle automatically routes through it for near-instant (~0.3s) transcription; otherwise it loads the model in-process each time (~3s). Models are cached in `~/.cache/huggingface/` after first download.

### Options

    --model large-v3        # tiny | base | small | medium | large-v3
    --device auto           # auto | cuda | cpu
    --compute-type auto     # float16 | int8 | int8_float16 | ...
    --language en           # ISO code, or "" to auto-detect

## GPU acceleration

faster-whisper runs on CTranslate2, which needs the CUDA runtime, cuBLAS, and cuDNN 9. The simplest way — no system packages, no sudo — is to bundle the NVIDIA libraries into the tool's environment when installing:

    uv tool install whisper-dictate \
      --with "nvidia-cudnn-cu12>=9,<10" \
      --with nvidia-cublas-cu12

whisper-dictate preloads those wheels at runtime (they install to a path the dynamic loader doesn't search by default), so CUDA "just works" — `--device auto` detects the GPU and uses `large-v3` at `float16`. With the [warm-model daemon](#usage) running, transcription is ~0.3s.

Alternatively, if you already have a system CUDA + cuDNN 9 install on your library path, plain `uv tool install whisper-dictate` will use it.

Models download to `~/.cache/huggingface/` on first use. If you hit CUDA OOM with `large-v3`, drop to `--model medium` or `--compute-type int8_float16`.

## Troubleshooting

    whisper-dictate check

Common issues:

- **Toggling just keeps saying "Recording…" and never types (Linux):** `PortAudio library not found` — the recorder worker crashes on start, so each invocation spawns a fresh doomed worker. Install it: `sudo apt install libportaudio2`.
- **"Permission denied" on /dev/uinput (Linux):** log out after `usermod -aG input`. Some distros (incl. Pop!_OS) instead grant the active-session user a uinput udev ACL — check with `getfacl /dev/uinput`; if your user is listed, no group/relogin is needed.
- **Text doesn't appear on Wayland:** xdotool is being used instead of ydotool. Run `whisper-dictate check`.
- **ydotool reports success but nothing types (Ubuntu/Pop!_OS):** ydotool 0.1.8's daemon listens on `/tmp/.ydotool_socket`, not `$HOME/.ydotool_socket`. Set `export YDOTOOL_SOCKET="/tmp/.ydotool_socket"` to match.
- **Empty transcript despite speaking:** usually a too-quiet mic. Check the captured level and raise input gain (`pactl set-source-volume @DEFAULT_SOURCE@ 100%`). VAD filtering is off by default for this reason; if you turned it on with `--vad` and lose quiet speech, drop it again.
- **Every press takes ~3s:** the model is being loaded each time. Start the warm-model daemon (`whisper-dictate serve`, or `./setup-server.sh`) to keep it resident (~0.3s per press).
- **Transcription is slow / runs on CPU despite an NVIDIA GPU:** cuDNN isn't being found. Reinstall with the bundled CUDA wheels (see [GPU acceleration](#gpu-acceleration)).
- **Garbled / dropped characters in web apps:** increase the key delay (currently 3ms).
- **CUDA OOM with large-v3:** drop to `--model medium` or `--compute-type int8_float16`.

## License

MIT
