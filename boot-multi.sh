#!/usr/bin/env bash
# boot-multi.sh - run N rpow2 miners in parallel, one per account.
#
# Each rpow2 account is server-rate-limited to ~1 mint / 5 seconds. A single
# 8-core device spends most of the cooldown waiting. Running multiple accounts
# in parallel linearly scales throughput until CPU becomes the bottleneck.
#
# Usage:
#   Paste your cookies into ~/rpow2-cookies.txt, one per line:
#     rpow_session=eyJ...aaa
#     rpow_session=eyJ...bbb
#     rpow_session=eyJ...ccc
#
#   Then:
#     curl -fsSL https://raw.githubusercontent.com/fauzangalib/nocoint-1/rpow2-miner/boot-multi.sh | bash
#
# Or set RPOW2_COOKIES env (newline- or '|||'-separated):
#     RPOW2_COOKIES='rpow_session=aaa|||rpow_session=bbb' bash boot-multi.sh

set -euo pipefail

COOKIES_FILE="${HOME}/rpow2-cookies.txt"

collect_cookies() {
  if [[ -n "${RPOW2_COOKIES:-}" ]]; then
    printf '%s\n' "${RPOW2_COOKIES//|||/$'\n'}"
    return
  fi
  if [[ -f "$COOKIES_FILE" ]]; then
    cat "$COOKIES_FILE"
    return
  fi
  if [[ -n "${RPOW2_COOKIE:-}" ]]; then
    printf '%s\n' "$RPOW2_COOKIE"
    return
  fi
  echo ""
}

mapfile -t COOKIES < <(collect_cookies | grep -Ev '^\s*(#|$)' | sed 's/\r$//')
N="${#COOKIES[@]}"

if (( N == 0 )); then
  cat >&2 <<EOF
ERROR: no cookies found. Provide them in one of:
  (a) ~/rpow2-cookies.txt (one 'rpow_session=...' per line)
  (b) env RPOW2_COOKIES='cookie1|||cookie2|||cookie3'
  (c) env RPOW2_COOKIE='single_cookie' (falls back to single-account mode)
EOF
  exit 2
fi

echo "[boot-multi] found $N account(s)"

REPO_DIR="${HOME}/nocoint-1"

need() { command -v "$1" >/dev/null 2>&1; }

if command -v pkg >/dev/null 2>&1;  then PM="pkg"
elif command -v apt-get >/dev/null 2>&1; then PM="apt-get"
elif command -v apk >/dev/null 2>&1; then PM="apk"
else echo "ERROR: unsupported OS" >&2; exit 3; fi

ensure_pkg() {
  need "$1" && return
  case "$PM" in
    pkg)     pkg install -y "$1" ;;
    apt-get) (apt-get update -y && apt-get install -y "$1") || (sudo apt-get update -y && sudo apt-get install -y "$1") ;;
    apk)     apk add --no-cache "$1" ;;
  esac
}
for p in python git tmux; do ensure_pkg "$p" || true; done
if ! need python3 && need python; then ln -sf "$(command -v python)" /usr/local/bin/python3 2>/dev/null || true; fi
if ! need cc && ! need gcc && ! need clang; then
  case "$PM" in
    pkg) pkg install -y clang || true ;;
    apt-get) (apt-get install -y build-essential 2>/dev/null || sudo apt-get install -y build-essential) || true ;;
    apk) apk add --no-cache build-base || true ;;
  esac
fi

need termux-wake-lock && termux-wake-lock || true

if [[ -d "$REPO_DIR/.git" ]]; then
  git -C "$REPO_DIR" fetch --depth=1 origin rpow2-miner
  git -C "$REPO_DIR" checkout rpow2-miner
  git -C "$REPO_DIR" reset --hard origin/rpow2-miner
else
  git clone --depth=1 -b rpow2-miner https://github.com/fauzangalib/nocoint-1.git "$REPO_DIR"
fi

CC=""
need cc && CC="cc"
[[ -z "$CC" ]] && need gcc && CC="gcc"
[[ -z "$CC" ]] && need clang && CC="clang"
if [[ -n "$CC" && -f "$REPO_DIR/solver.c" ]]; then
  if "$CC" -O3 -o "$REPO_DIR/solver" "$REPO_DIR/solver.c"; then
    echo "[boot-multi] native solver compiled"
  else
    echo "[boot-multi] native compile failed, using Python fallback"
  fi
fi

python3 "$REPO_DIR/rpow2.py" --selftest

CORES=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)
USABLE=$(( CORES > 1 ? CORES - 1 : 1 ))
PER_ACCOUNT=$(( USABLE / N ))
(( PER_ACCOUNT < 1 )) && PER_ACCOUNT=1
(( PER_ACCOUNT > 4 )) && PER_ACCOUNT=4
echo "[boot-multi] cores=$CORES, accounts=$N, workers/account=$PER_ACCOUNT"

tmux kill-session -t rpow 2>/dev/null || true
for i in $(seq 0 15); do
  tmux kill-session -t "rpow$i" 2>/dev/null || true
done

for i in "${!COOKIES[@]}"; do
  c="${COOKIES[$i]}"
  label=$(printf '%s' "$c" | sed -n 's/^rpow_session=\([^.]*\).*/\1/p' \
    | tr '_-' '+/' | base64 -d 2>/dev/null | sed -n 's/.*"email":"\([^"]*\)".*/\1/p' || true)
  [[ -z "$label" ]] && label="acct$((i+1))"
  session="rpow$i"
  log="$REPO_DIR/rpow2-$i.log"
  echo "[boot-multi] starting $session for $label (log: $log)"
  tmux new-session -d -s "$session" -c "$REPO_DIR" \
    "bash -c 'export RPOW2_COOKIE=\"$c\"; \
       echo \"[start] \$(date) account=$label\" >> \"$log\"; \
       while true; do \
         python3 rpow2.py --workers $PER_ACCOUNT 2>&1 | tee -a \"$log\"; \
         ec=\$?; \
         echo \"[restart] exit=\$ec at \$(date), sleeping 15s...\" >> \"$log\"; \
         sleep 15; \
       done'"
done

echo
echo "==============================================="
echo "  $N rpow2 miners running in tmux"
echo "  list:       tmux ls"
echo "  attach i:   tmux attach -t rpow0   # 0..$((N-1))"
echo "  detach:     Ctrl+B then D"
echo "  tail logs:  tail -f $REPO_DIR/rpow2-*.log"
echo "  stop all:   for i in \$(seq 0 $((N-1))); do tmux kill-session -t rpow\$i; done"
echo "==============================================="
