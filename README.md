# whisper-dictate

Local Whisper-powered dictation that types into any application. Cross-platform (Linux/macOS/Windows), GPU-accelerated via faster-whisper.

## Installation

Every platform needs [uv](https://docs.astral.sh/uv/) and Python ≥ 3.11. For NVIDIA GPU acceleration, see [GPU acceleration](#gpu-acceleration).

**The painless path:** install the tool, then run `whisper-dictate init`. It checks every prerequisite for your OS, sets up the background services it can without root (the ydotool daemon and the warm-model autoloader), and prints the exact commands for anything that needs `sudo` (or runs them with `--yes`). Re-run it any time — it's idempotent. The per-platform sections below cover the one-time system packages `init` can't install for you.

### Linux — Wayland (Pop!_OS COSMIC, GNOME 4x, KDE Plasma 6)

1. **System libraries.** Recording needs PortAudio; typing on Wayland needs ydotool *and* its daemon (separate packages on Debian/Ubuntu):

       sudo apt install libportaudio2 ydotool ydotoold
       sudo apt install libnotify-bin          # optional: on-screen 🎙️/✓ feedback

2. **Install the tool** (CPU-only — for GPU add the `--with` flags from [GPU acceleration](#gpu-acceleration)):

       uv tool install whisper-dictate

3. **Run init** — it installs the `ydotoold` and warm-model systemd user services, checks `/dev/uinput` access, and verifies everything:

       whisper-dictate init       # expect it to finish with "All set ✓"

   (Prefer scripts? `./setup-ydotool.sh` and `./setup-server.sh` do the daemon pieces individually.)

4. **Bind a hotkey.** COSMIC: **Settings → Keyboard → Custom Shortcuts**, command `whisper-dictate` (use the full path from `which whisper-dictate` if COSMIC can't resolve it), bound to e.g. `Super+Space`. If transcripts come out empty/garbled, your mic gain is likely too low — raise it: `pactl set-source-volume @DEFAULT_SOURCE@ 100%`.

### Linux — X11 (Xorg GNOME, i3, XFCE, …)

Same as Wayland but with **xdotool** (no daemon or socket needed):

    sudo apt install libportaudio2 xdotool
    uv tool install whisper-dictate            # add the GPU --with flags if applicable
    whisper-dictate init                       # sets up the warm-model daemon, verifies setup

Then bind `whisper-dictate` to a hotkey in your DE/WM.

### macOS

PortAudio ships *inside* the `sounddevice` wheel, so there's no Homebrew step.

    uv tool install 'whisper-dictate[macos]'   # the [macos] extra pulls in pynput for typing
    whisper-dictate init                       # installs a launchd agent for the warm-model daemon, verifies setup

On first use, grant **Accessibility** permission to whatever launches the CLI (Terminal, iTerm, Raycast…) under **System Settings → Privacy & Security → Accessibility** (`init` reminds you). Transcription runs on CPU — start with `--model small` if `large-v3` feels slow. Status notifications use the built-in `osascript`, so nothing extra to install.

Bind a hotkey with Raycast, Hammerspoon, or Shortcuts.app.

### Windows

PortAudio ships inside the `sounddevice` wheel.

    uv tool install 'whisper-dictate[windows]' # the [windows] extra pulls in pynput for typing
    whisper-dictate init                        # registers a logon task for the warm-model daemon, verifies setup

For an NVIDIA GPU, add the `--with` flags from [GPU acceleration](#gpu-acceleration). Bind a hotkey with PowerToys, AutoHotkey, or a `.lnk` shortcut. Status notifications use a built-in PowerShell toast, so nothing extra to install. (The warm-model daemon needs Unix-domain sockets — available on Windows 10 1803+; otherwise transcription falls back to loading the model per call.)

### From a local wheel (any platform)

    uv build
    uv tool install dist/whisper_dictate-*.whl

## Usage

    whisper-dictate init         # check prerequisites & set up daemons for your OS
    whisper-dictate deinit       # remove the services & state that `init` created
    whisper-dictate settings     # open the settings window (model, translation, tone…)
    whisper-dictate              # toggle: start, then stop+type
    whisper-dictate start        # explicit start
    whisper-dictate stop         # explicit stop+transcribe+type
    whisper-dictate check        # diagnose platform setup
    whisper-dictate transcribe FILE.wav   # transcribe a file to stdout
    whisper-dictate serve        # warm-model daemon (keeps the model in memory)

Bind `whisper-dictate` (no args = toggle) to a global hotkey in your OS settings.

### Settings (no command line needed)

    whisper-dictate settings     # (aliases: gui, config)

Opens a window to choose your model, spoken language, translation target and tone — no flags to remember. Your choices are saved to a config file (`~/.config/whisper-dictate/config.json`, or the platform equivalent) that **every** dictation reads, so once you've set it there, a bare hotkey press just does the right thing. Passing a flag on the command line still overrides the saved value for that run. The window also has a **Check Ollama** button so you can confirm translation/restyle will work before relying on it.

When a `serve` daemon is running, the toggle automatically routes through it for near-instant (~0.3s) transcription; otherwise it loads the model in-process each time (~3s). Models are cached in `~/.cache/huggingface/` after first download.

### Options

    --model large-v3        # tiny | base | small | medium | large-v3
    --device auto           # auto | cuda | cpu
    --compute-type auto     # float16 | int8 | int8_float16 | ...
    --language en           # ISO code, or "" to auto-detect

## Translate & restyle (optional, via Ollama)

After transcribing, the text can be run through a local [Ollama](https://ollama.com) model before it's typed — to **translate** it into another language, **rewrite** it in a given tone, or both at once. This is entirely opt-in: with neither set, nothing touches the network and dictation behaves exactly as before.

> The easiest way to turn this on is the [settings window](#settings-no-command-line-needed) (`whisper-dictate settings`) — pick a "Translate to" language and/or a tone and save. The flags below do the same thing for command-line/hotkey use.

**Setup:** install Ollama and pull a model (any chat model works):

    ollama pull llama3.1:8b      # the default; or qwen2.5:7b (stronger multilingual), gemma2:9b, etc.

Ollama runs as its own resident server, so — like the warm Whisper daemon — the model stays hot between calls.

If translation/tone is enabled in your settings, `whisper-dictate init` checks that Ollama is reachable and that your configured model is present — pulling it for you with `init --yes`, or listing the `ollama pull` command otherwise. The warm-model daemon `init` installs also preloads whatever Whisper model you saved in settings (not always `large-v3`).

    --translate-to LANG     # translate into this language (name or code: "English", "Spanish", "ja")
    --style TONE            # rewrite in a tone — see below
    --ollama-model NAME     # model to use (default: llama3.1:8b)
    --ollama-host URL       # default: $OLLAMA_HOST or http://localhost:11434

**Translation auto-detects the spoken language** — you only ever pick the *target*, never the source. So `--translate-to English` turns Korean (or anything) you speak into English; `--translate-to Spanish` turns your English into Spanish. (Whisper's own translate task only ever outputs English; routing through Ollama is what makes any → any possible.)

**`--style`** takes a preset — `professional`, `personable`, `concise`, `casual` — or any free-form instruction, e.g. `--style "as a polite email"`. Combine the two flags to translate *and* restyle in one pass.

    whisper-dictate --style professional
    whisper-dictate --translate-to English
    whisper-dictate --translate-to Spanish --style personable

Bind each variant to its own hotkey to get, say, plain dictation on one key and "clean this up professionally" on another. If Ollama is unreachable or errors, you get a notification and your **raw transcript is typed anyway** — your words are never lost.

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
- **Empty transcript despite speaking:** usually a too-quiet mic. The recording is *kept* on an empty/failed result (it's deleted only after a successful transcription), so you can inspect it at `$XDG_RUNTIME_DIR/whisper-dictate/recording.wav` (`%LOCALAPPDATA%\whisper-dictate\recording.wav` on Windows). Check its level and raise input gain (`pactl set-source-volume @DEFAULT_SOURCE@ 100%`). VAD filtering is off by default for this reason; if you turned it on with `--vad` and lose quiet speech, drop it again.
- **Every press takes ~3s:** the model is being loaded each time. Start the warm-model daemon (`whisper-dictate serve`, or `./setup-server.sh`) to keep it resident (~0.3s per press).
- **Transcription is slow / runs on CPU despite an NVIDIA GPU:** cuDNN isn't being found. Reinstall with the bundled CUDA wheels (see [GPU acceleration](#gpu-acceleration)).
- **Garbled / dropped characters in web apps:** increase the key delay (currently 3ms).
- **CUDA OOM with large-v3:** drop to `--model medium` or `--compute-type int8_float16`.

## License

MIT
