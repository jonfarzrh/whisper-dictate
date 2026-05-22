#!/usr/bin/env bash
#
# setup-ydotool.sh — configure the ydotoold daemon + socket for whisper-dictate.
#
# Run as your normal user (NOT root). It will:
#   1. Verify ydotool / ydotoold are installed.
#   2. Install a systemd --user service for ydotoold.
#   3. Enable + start that service.
#   4. Add YDOTOOL_SOCKET to ~/.zshrc and ~/.bashrc.
#   5. Tell you whether a logout/login is still needed for /dev/uinput access.
#
set -euo pipefail

# NOTE: Ubuntu/Pop!_OS ship ydotool 0.1.8, whose ydotoold ignores --socket-path
# and always listens on /tmp/.ydotool_socket. So that is the path clients must use.
SOCKET="/tmp/.ydotool_socket"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/ydotoold.service"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32mOK\033[0m %s\n' "$*"; }

# 1. Prerequisites -----------------------------------------------------------
if [[ $EUID -eq 0 ]]; then
    warn "Run this as your normal user, not root. Aborting."
    exit 1
fi

for bin in ydotool ydotoold; do
    if ! command -v "$bin" >/dev/null 2>&1; then
        warn "$bin not found on PATH. Install it first: sudo apt install $bin"
        exit 1
    fi
done
ok "ydotool and ydotoold are installed."

# 2. systemd --user service --------------------------------------------------
info "Writing systemd user unit: $UNIT"
mkdir -p "$UNIT_DIR"
cat > "$UNIT" << 'EOF'
[Unit]
Description=ydotool daemon
After=graphical-session.target

[Service]
ExecStart=/usr/bin/ydotoold --socket-path=%h/.ydotool_socket --socket-own=%U:%U
Restart=on-failure

[Install]
WantedBy=default.target
EOF
ok "Unit written."

# 3. Enable + start ----------------------------------------------------------
info "Reloading and enabling ydotoold..."
systemctl --user daemon-reload
systemctl --user enable --now ydotoold || warn "Service failed to start (likely needs the 'input' group — see below)."

# 4. Environment variable ----------------------------------------------------
for rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
    [[ -e "$rc" ]] || continue
    if grep -q 'YDOTOOL_SOCKET' "$rc"; then
        ok "YDOTOOL_SOCKET already present in $rc"
    else
        info "Adding YDOTOOL_SOCKET export to $rc"
        printf '\nexport YDOTOOL_SOCKET="/tmp/.ydotool_socket"\n' >> "$rc"
    fi
done

# 5. Final status ------------------------------------------------------------
echo
info "Status check:"
systemctl --user --no-pager status ydotoold || true

echo
if id -nG | tr ' ' '\n' | grep -qx input; then
    ok "You are in the 'input' group."
else
    warn "You are NOT yet in the 'input' group in this session."
    warn "Run:  sudo usermod -aG input \$USER   (if you haven't already)"
    warn "Then LOG OUT and back in to activate it."
fi

echo
if [[ -S "$SOCKET" ]]; then
    ok "Socket exists: $SOCKET — ydotool should be ready."
    echo "   Open this shell fresh (or 'export YDOTOOL_SOCKET=$SOCKET') and test:"
    echo "     whisper-dictate check"
else
    warn "Socket $SOCKET not present yet."
    warn "This usually clears after a logout/login (input-group + daemon restart)."
fi
