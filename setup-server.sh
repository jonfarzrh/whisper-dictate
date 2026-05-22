#!/usr/bin/env bash
#
# setup-server.sh — run the whisper-dictate warm-model daemon as a systemd
# --user service so the Whisper model stays loaded in VRAM and dictation is
# near-instant (~0.3s) instead of paying the ~3s model-load cost every press.
#
# Run as your normal user (NOT root).
#
set -euo pipefail

UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/whisper-dictate-server.service"
BIN="$(command -v whisper-dictate || echo "$HOME/.local/bin/whisper-dictate")"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32mOK\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }

if [[ $EUID -eq 0 ]]; then
    warn "Run this as your normal user, not root. Aborting."
    exit 1
fi
if [[ ! -x "$BIN" ]]; then
    warn "whisper-dictate not found on PATH or at ~/.local/bin. Install it first."
    exit 1
fi
ok "Using whisper-dictate at: $BIN"

# By default the daemon follows your saved settings (the model picked in the
# settings window). Pin a specific model only if you want to:  WD_MODEL=medium ./setup-server.sh
if [[ -n "${WD_MODEL:-}" ]]; then
    SERVE_CMD="$BIN serve --model $WD_MODEL"
    MODEL_NOTE="model: $WD_MODEL"
else
    SERVE_CMD="$BIN serve"
    MODEL_NOTE="model: follows your saved settings"
fi

info "Writing systemd user unit: $UNIT ($MODEL_NOTE)"
mkdir -p "$UNIT_DIR"
cat > "$UNIT" << EOF
[Unit]
Description=whisper-dictate warm-model daemon
After=graphical-session.target

[Service]
ExecStart=$SERVE_CMD
Restart=on-failure
# Keep the model resident; it holds a few GB of VRAM by design.

[Install]
WantedBy=default.target
EOF
ok "Unit written."

info "Reloading and starting the daemon (first start loads the model — give it a few seconds)..."
systemctl --user daemon-reload
systemctl --user enable --now whisper-dictate-server

echo
info "Status:"
systemctl --user --no-pager status whisper-dictate-server || true

echo
info "Follow the load progress with:  journalctl --user -u whisper-dictate-server -f"
info "Once it logs 'ready, listening', your hotkey transcriptions will be ~0.3s."
echo
info "To stop / disable later:"
echo "    systemctl --user disable --now whisper-dictate-server"
