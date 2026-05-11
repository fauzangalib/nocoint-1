#!/usr/bin/env bash
# boot.sh - one-command setup for ephemeral Android sandboxes (xcloudphone,
# cloudphone, Termux on a fresh device, etc.).
#
# Intended usage (paste into Termux / ADB shell / cloudphone terminal):
#
#   curl -fsSL https://raw.githubusercontent.com/fauzangalib/nocoint-1/rpow2-miner/boot.sh | \
#     RPOW2_COOKIE='rpow_session=...' bash
#
# It is idempotent: re-running after a 5-hour sandbox reset just re-installs
# the few deps and resumes mining. Nothing sensitive is persisted.

set -euo pipefail

if [[ -z "${RPOW2_COOKIE:-}" ]]; then
  echo "ERROR: RPOW2_COOKIE not set." >&2
  echo "Usage: RPOW2_COOKIE='rpow_session=...' bash boot.sh" >&2
  exit 2
fi

# ---- detect pkg manager ----------------------------------------------------
if command -v pkg >/dev/null 2>&1; then
  PM="pkg"                       # Termux
elif command -v apt-get >/dev/null 2>&1; then
  PM="apt-get"                   # Debian/Ubuntu
elif command -v apk >/dev/null 2>&1; then
  PM="apk"                       # Alpine
else
  echo "ERROR: no supported package manager (pkg/apt-get/apk)." >&2
  exit 3
fi

need() { command -v "$1" >/dev/null 2>&1; }

ensure_pkg() {
  local p="$1"
  if need "$p"; then return; fi
  echo "[boot] installing $p via $PM..."
  case "$PM" in
    pkg)     pkg install -y "$p" ;;
    apt-get) (apt-get update -y && apt-get install -y "$p") || \
             (sudo apt-get update -y && sudo apt-get install -y "$p") ;;
    apk)     apk add --no-cache "$p" ;;
  esac
}

for p in python git tmux; do ensure_pkg "$p"; done
# on Debian/Ubuntu it's `python3`, not `python`
if ! need python3 && need python; then ln -sf "$(command -v python)" /usr/local/bin/python3 2>/dev/null || true; fi
if ! need python3; then ensure_pkg python3; fi

# ---- keep screen on (Termux only) -----------------------------------------
if need termux-wake-lock; then
  termux-wake-lock || true
fi

# ---- fetch repo ------------------------------------------------------------
REPO_DIR="${HOME}/nocoint-1"
if [[ -d "$REPO_DIR/.git" ]]; then
  echo "[boot] repo exists, pulling..."
  git -C "$REPO_DIR" fetch --depth=1 origin rpow2-miner
  git -C "$REPO_DIR" checkout rpow2-miner
  git -C "$REPO_DIR" reset --hard origin/rpow2-miner
else
  git clone --depth=1 -b rpow2-miner \
    https://github.com/fauzangalib/nocoint-1.git "$REPO_DIR"
fi

# ---- self-test -------------------------------------------------------------
( cd "$REPO_DIR" && python3 rpow2.py --selftest )

# ---- work out worker count -------------------------------------------------
CORES=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)
# leave 1 core free for OS / sandbox; clamp to 1..8
WORKERS=$(( CORES > 1 ? CORES - 1 : 1 ))
(( WORKERS > 8 )) && WORKERS=8
echo "[boot] cores=$CORES -> workers=$WORKERS"

# ---- launch in tmux with auto-restart loop --------------------------------
kill_old() { tmux kill-session -t rpow 2>/dev/null || true; }
kill_old

export RPOW2_COOKIE
tmux new-session -d -s rpow -c "$REPO_DIR" "bash -c '
  echo \"[start] $(date)\"
  trap exit INT TERM
  while true; do
    python3 rpow2.py --workers '"$WORKERS"' 2>&1 | tee -a rpow2.log
    ec=\$?
    echo \"[restart] exit=\$ec at \$(date), sleeping 15s...\" | tee -a rpow2.log
    sleep 15
  done
'"

echo
echo "==============================================="
echo "  rpow2 miner running in tmux session 'rpow'"
echo "  attach:   tmux attach -t rpow"
echo "  detach:   Ctrl+B then D"
echo "  logs:     tail -f $REPO_DIR/rpow2.log"
echo "  stop:     tmux kill-session -t rpow"
echo "  status:   (cd $REPO_DIR && python3 rpow2.py --status)"
echo "==============================================="
