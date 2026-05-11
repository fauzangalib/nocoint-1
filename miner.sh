#!/usr/bin/env bash
# $NOCOIN manual helper. Loads identity from .env (or env vars).
# Usage:
#   ./miner.sh pull
#   ./miner.sh submit <puzzle_id> <answer>

set -euo pipefail

# Load .env if present (no override of existing env).
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

: "${NOCOIN_WALLET:?set NOCOIN_WALLET (copy .env.example to .env)}"
: "${NOCOIN_AGENT:?set NOCOIN_AGENT}"
: "${NOCOIN_APIKEY:?set NOCOIN_APIKEY}"

BASE="https://bqrapnlqqtjedjyhlfci.supabase.co/functions/v1/submit-solution"

pull() {
  curl -s "$BASE?eth=$NOCOIN_WALLET" -H "apikey: $NOCOIN_APIKEY"
  echo
}

submit() {
  local pid="$1" ans="$2"
  curl -s -X POST "$BASE" \
    -H "apikey: $NOCOIN_APIKEY" \
    -H "Content-Type: application/json" \
    -d "$(jq -nc --arg e "$NOCOIN_WALLET" --arg a "$NOCOIN_AGENT" \
                 --arg p "$pid" --arg s "$ans" \
          '{eth_address:$e, agent_name:$a, puzzle_id:$p, answer:$s}')"
  echo
}

case "${1:-}" in
  pull)   pull ;;
  submit) submit "${2:?puzzle_id}" "${3:?answer}" ;;
  *)      echo "usage: $0 pull | submit <id> <answer>" >&2; exit 1 ;;
esac
