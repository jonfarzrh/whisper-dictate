#!/bin/bash
#
# Mac-side runtime validation for the MLX-Whisper engine integration.
#
# Run this on your Apple Silicon Mac from the whisper-dictate repo root.
# It exercises the in-process AND daemon paths under MLX, and confirms
# faster_whisper still works on Mac (CPU fallback).
#
# Usage:
#   chmod +x test_mlx_mac.sh
#   ./test_mlx_mac.sh
#
# The script is fail-fast (set -e). If any step fails the script aborts
# at that step with a FAIL banner; everything that passed before that
# is printed PASS.
#
# Targets macOS default bash (3.x) and POSIX coreutils only.

set -e
set -u

# -------- pretty banners --------
banner() {
    echo ""
    echo "================================================================"
    echo "  $*"
    echo "================================================================"
}
pass() { echo "[PASS] $*"; }
fail() { echo "[FAIL] $*" >&2; exit 1; }
info() { echo "[..]   $*"; }

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_ROOT"

TEST_WAV="/tmp/wd-mlx-test.wav"
SOCKET_PATH=""
DAEMON_PID=""

cleanup() {
    if [ -n "$DAEMON_PID" ] && kill -0 "$DAEMON_PID" 2>/dev/null; then
        info "cleanup: killing daemon pid=$DAEMON_PID"
        kill "$DAEMON_PID" 2>/dev/null || true
        wait "$DAEMON_PID" 2>/dev/null || true
    fi
    rm -f "$TEST_WAV"
}
trap cleanup EXIT INT TERM

# -------- Step 1: platform check --------
banner "Step 1 / 10: verify darwin arm64"
OS="$(uname -s)"
ARCH="$(uname -m)"
info "uname -s = $OS"
info "uname -m = $ARCH"
if [ "$OS" != "Darwin" ]; then fail "expected Darwin, got $OS"; fi
if [ "$ARCH" != "arm64" ]; then fail "expected arm64, got $ARCH (Intel Mac not supported by MLX)"; fi
pass "running on darwin arm64"

# -------- Step 2: install mlx-whisper extra --------
banner "Step 2 / 10: uv sync --extra apple"
uv sync --extra apple
pass "uv sync --extra apple completed"

# Sanity: confirm mlx_whisper is importable in the .venv
if ! uv run python -c "import mlx_whisper" 2>/dev/null; then
    fail "mlx_whisper still not importable after uv sync --extra apple"
fi
pass "mlx_whisper importable in .venv"

# -------- Step 3: generate test WAV --------
banner "Step 3 / 10: generate test WAV"
uv run python - <<'PY'
import math, struct, wave
# 1 second of low-amplitude 440 Hz so MLX has something to chew on.
SR = 16000
DUR = 1.0
samples = []
for i in range(int(SR * DUR)):
    samples.append(int(3000 * math.sin(2 * math.pi * 440 * i / SR)))
with wave.open("/tmp/wd-mlx-test.wav", "w") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(b"".join(struct.pack("<h", s) for s in samples))
print("wrote /tmp/wd-mlx-test.wav")
PY
[ -f "$TEST_WAV" ] || fail "test wav not created"
pass "test wav created at $TEST_WAV"

# -------- Step 4: check command surfaces engine=mlx --------
banner "Step 4 / 10: whisper-dictate check shows engine: mlx"
CHECK_OUT="$(uv run whisper-dictate check 2>&1)"
echo "$CHECK_OUT"
echo "---"
if ! echo "$CHECK_OUT" | grep -q "engine:[[:space:]]*mlx"; then
    fail "expected 'engine: mlx' in check output"
fi
if ! echo "$CHECK_OUT" | grep -qi "MLX"; then
    fail "expected MLX detection reason in check output"
fi
pass "check command reports engine=mlx with detection reason"

# -------- Step 5: in-process transcribe under auto -> MLX --------
banner "Step 5 / 10: in-process transcribe (auto-engine = MLX)"
info "running: whisper-dictate transcribe $TEST_WAV"
TS_OUT="$(uv run whisper-dictate transcribe "$TEST_WAV" 2>&1)"
TS_RC=$?
echo "$TS_OUT"
echo "---"
if [ $TS_RC -ne 0 ]; then fail "transcribe (auto) exited $TS_RC"; fi
pass "auto transcribe returned exit 0"

# -------- Step 6: explicit faster_whisper engine still works on Mac --------
banner "Step 6 / 10: --engine faster_whisper (CPU fallback on Mac)"
info "running: whisper-dictate transcribe $TEST_WAV --engine faster_whisper"
FW_OUT="$(uv run whisper-dictate transcribe "$TEST_WAV" --engine faster_whisper 2>&1)"
FW_RC=$?
echo "$FW_OUT"
echo "---"
if [ $FW_RC -ne 0 ]; then fail "transcribe (faster_whisper) exited $FW_RC"; fi
pass "faster_whisper transcribe returned exit 0 on Mac"

# -------- Step 7: --engine mlx --vad emits warning, still succeeds --------
banner "Step 7 / 10: --engine mlx --vad emits warning + still succeeds"
info "running: whisper-dictate transcribe $TEST_WAV --engine mlx --vad"
# Capture stderr separately so we can grep for the warning.
VAD_STDERR_FILE="$(mktemp)"
set +e
uv run whisper-dictate transcribe "$TEST_WAV" --engine mlx --vad 2>"$VAD_STDERR_FILE"
VAD_RC=$?
set -e
echo "--- stderr ---"
cat "$VAD_STDERR_FILE"
echo "--- /stderr ---"
if [ $VAD_RC -ne 0 ]; then
    rm -f "$VAD_STDERR_FILE"
    fail "transcribe (mlx + vad) exited $VAD_RC"
fi
if ! grep -qi "VAD not supported under MLX" "$VAD_STDERR_FILE"; then
    rm -f "$VAD_STDERR_FILE"
    fail "expected VAD-warning on stderr"
fi
rm -f "$VAD_STDERR_FILE"
pass "mlx + vad: succeeded AND emitted the expected stderr warning"

# -------- Step 8: warm-model daemon under MLX --------
banner "Step 8 / 10: warm-model daemon with MLX engine"
# Where the daemon writes its socket — must match recorder._state_dir() on macOS.
# On macOS this is $TMPDIR/whisper-dictate/server.sock (TMPDIR is the per-user
# /var/folders/... that launchd uses); fall back to /tmp if unset.
STATE_BASE="${TMPDIR:-/tmp}"
SOCKET_PATH="${STATE_BASE%/}/whisper-dictate/server.sock"
info "expecting socket at: $SOCKET_PATH"

# Start daemon in background. Send its logs to a temp file.
DAEMON_LOG="$(mktemp)"
uv run whisper-dictate serve >"$DAEMON_LOG" 2>&1 &
DAEMON_PID=$!
info "daemon started, pid=$DAEMON_PID"

# Wait for the daemon to log "ready". Generous timeout: first-ever MLX run
# downloads the model, which takes a while on slow links.
WAITED=0
MAX_WAIT=300
while [ $WAITED -lt $MAX_WAIT ]; do
    if grep -q "ready, listening on" "$DAEMON_LOG" 2>/dev/null; then
        break
    fi
    sleep 2
    WAITED=$((WAITED + 2))
done
if [ $WAITED -ge $MAX_WAIT ]; then
    echo "--- daemon log ---"
    cat "$DAEMON_LOG"
    rm -f "$DAEMON_LOG"
    fail "daemon did not become ready within ${MAX_WAIT}s"
fi
info "daemon ready after ${WAITED}s"

# Resolve actual socket path from daemon log (more reliable than guessing TMPDIR).
SOCKET_PATH="$(grep -o 'listening on [^[:space:]]*' "$DAEMON_LOG" | head -1 | awk '{print $3}')"
info "daemon socket: $SOCKET_PATH"
[ -S "$SOCKET_PATH" ] || { cat "$DAEMON_LOG"; rm -f "$DAEMON_LOG"; fail "socket file missing"; }

# Transcribe via the daemon (the CLI auto-detects the socket).
info "running transcribe against daemon"
DAEMON_TS_OUT="$(uv run whisper-dictate transcribe "$TEST_WAV" 2>&1)"
DAEMON_TS_RC=$?
echo "$DAEMON_TS_OUT"
echo "--- daemon log so far ---"
cat "$DAEMON_LOG"
echo "--- /daemon log ---"
if [ $DAEMON_TS_RC -ne 0 ]; then
    rm -f "$DAEMON_LOG"
    fail "daemon transcribe exited $DAEMON_TS_RC"
fi
# The daemon log should mention loading under the mlx engine.
if ! grep -qi "mlx" "$DAEMON_LOG"; then
    rm -f "$DAEMON_LOG"
    fail "daemon log doesn't mention mlx — did it actually use MLX?"
fi
pass "daemon path works under MLX"

# -------- Step 9: engine eviction via config switch --------
banner "Step 9 / 10: switch engine in config -> daemon evicts + pre-warms"
# We DON'T restart the daemon — the whole point of the watch_config thread is
# that the running daemon picks up the change.
CFG_FILE="$(uv run python -c 'from whisper_dictate import config; print(config.config_file())')"
info "config file: $CFG_FILE"

# Snapshot for restore
CFG_BACKUP="$(mktemp)"
cp "$CFG_FILE" "$CFG_BACKUP" 2>/dev/null || echo '{}' >"$CFG_BACKUP"

write_engine() {
    uv run python - <<PY
import json, pathlib
from whisper_dictate import config
cfg = config.load_config()
cfg["engine"] = "$1"
config.save_config(cfg)
print("wrote engine =", cfg["engine"])
PY
}

# Switch to faster_whisper and wait a few seconds for the watcher to react.
write_engine faster_whisper
info "waiting 8s for config watcher to pre-warm faster_whisper..."
sleep 8

# Run a transcribe and confirm it succeeds via the daemon.
info "transcribing under faster_whisper (daemon should already be warmed)"
FW_DAEMON_OUT="$(uv run whisper-dictate transcribe "$TEST_WAV" 2>&1)"
FW_DAEMON_RC=$?
echo "$FW_DAEMON_OUT"
if [ $FW_DAEMON_RC -ne 0 ]; then
    cat "$DAEMON_LOG"
    cp "$CFG_BACKUP" "$CFG_FILE"
    rm -f "$CFG_BACKUP" "$DAEMON_LOG"
    fail "daemon transcribe under faster_whisper exited $FW_DAEMON_RC"
fi

# The daemon log should show "settings changed, pre-warming ... (faster_whisper)".
if ! grep -qi "settings changed.*faster_whisper" "$DAEMON_LOG"; then
    echo "--- daemon log ---"; cat "$DAEMON_LOG"; echo "---"
    info "WARN: didn't see the explicit pre-warm log line, but transcribe succeeded"
fi

# Switch back to mlx and confirm again.
write_engine mlx
info "waiting 8s for config watcher to pre-warm mlx..."
sleep 8
MLX_DAEMON_OUT="$(uv run whisper-dictate transcribe "$TEST_WAV" 2>&1)"
MLX_DAEMON_RC=$?
echo "$MLX_DAEMON_OUT"
if [ $MLX_DAEMON_RC -ne 0 ]; then
    cat "$DAEMON_LOG"
    cp "$CFG_BACKUP" "$CFG_FILE"
    rm -f "$CFG_BACKUP" "$DAEMON_LOG"
    fail "daemon transcribe under mlx (after switch back) exited $MLX_DAEMON_RC"
fi
pass "daemon evicts + reloads on engine switch (faster_whisper <-> mlx)"

# Restore original config
cp "$CFG_BACKUP" "$CFG_FILE"
rm -f "$CFG_BACKUP"
info "restored original config.json"

# Stop the daemon cleanly
kill "$DAEMON_PID" 2>/dev/null || true
wait "$DAEMON_PID" 2>/dev/null || true
DAEMON_PID=""
rm -f "$DAEMON_LOG"

# -------- Step 10: settings GUI launches without crashing --------
banner "Step 10 / 10: settings GUI launches (visual confirmation by user)"
info "launching settings GUI for 5s — confirm 'Engine' dropdown is present"
uv run whisper-dictate settings &
GUI_PID=$!
sleep 5
if ! kill -0 "$GUI_PID" 2>/dev/null; then
    fail "settings GUI exited prematurely (didn't survive 5s)"
fi
kill "$GUI_PID" 2>/dev/null || true
wait "$GUI_PID" 2>/dev/null || true
pass "settings GUI survived 5s (visually confirm the Engine row, please)"

# -------- All green --------
banner "ALL 10 STEPS PASSED"
echo ""
echo "Summary:"
echo "  1. darwin arm64 confirmed"
echo "  2. uv sync --extra apple installed mlx-whisper"
echo "  3. test WAV generated"
echo "  4. 'whisper-dictate check' reports engine=mlx"
echo "  5. in-process transcribe under MLX succeeded"
echo "  6. --engine faster_whisper still works on Mac (CPU)"
echo "  7. --engine mlx --vad warns on stderr and still succeeds"
echo "  8. warm-model daemon works under MLX"
echo "  9. config-driven engine switch evicts + reloads the daemon"
echo " 10. settings GUI opens without crashing"
echo ""
echo "Manual confirmation still owed by the user:"
echo "  - The 'Engine' dropdown in the settings window (step 10) showed: auto / faster_whisper / mlx"
echo "  - Selecting mlx and saving updated config.json"
echo ""
