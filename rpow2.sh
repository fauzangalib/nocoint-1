#!/usr/bin/env bash
# rpow2.sh - tiny curl helper for https://api.rpow2.com
#
# Auth is cookie-based. Log in at https://rpow2.com in your browser (email
# magic-link + Cloudflare Turnstile), then grab the Cookie header for
# api.rpow2.com from DevTools and export it:
#
#   export RPOW2_COOKIE='session=...; ...'
#
# Usage:
#   ./rpow2.sh ledger
#   ./rpow2.sh me
#   ./rpow2.sh challenge
#   ./rpow2.sh mint <challenge_id> <solution_nonce>

set -euo pipefail

API="https://api.rpow2.com"

_curl() {
  local method="$1"; shift
  local path="$1"; shift
  local body="${1:-}"
  local args=(
    -sS
    -X "$method"
    -H "accept: application/json"
    -H "origin: https://rpow2.com"
    -H "referer: https://rpow2.com/"
  )
  if [[ -n "${RPOW2_COOKIE:-}" ]]; then
    args+=(-H "cookie: $RPOW2_COOKIE")
  fi
  if [[ -n "$body" ]]; then
    args+=(-H "content-type: application/json" --data "$body")
  fi
  curl "${args[@]}" "$API$path"
}

case "${1:-}" in
  ledger)    _curl GET /ledger ;;
  me)        _curl GET /me ;;
  challenge) _curl POST /challenge ;;
  mint)
    : "${2?usage: mint <challenge_id> <solution_nonce>}"
    : "${3?usage: mint <challenge_id> <solution_nonce>}"
    _curl POST /mint "{\"challenge_id\":\"$2\",\"solution_nonce\":\"$3\"}"
    ;;
  *)
    echo "usage: $0 {ledger|me|challenge|mint <id> <nonce>}" >&2
    exit 2
    ;;
esac
echo
