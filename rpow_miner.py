#!/usr/bin/env python3
"""
RPOW2 native miner — multi-process SHA-256 PoW.

Challenge format (from https://api.rpow2.com/challenge):
  {
    "challenge_id":    "<uuid>",
    "nonce_prefix":    "<32 hex chars = 16 bytes>",
    "difficulty_bits": 25,
    "expires_at":      "<iso8601>"
  }

Mining rule:
  Find a suffix S such that SHA256(nonce_prefix || S) has >= difficulty_bits
  leading zero bits. Then submit {challenge_id, suffix} to the submit endpoint.

Usage:
  export RPOW_SESSION="eyJlbWFpbCI6..."   # from cookie rpow_session
  python3 rpow_miner.py --workers 4
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import multiprocessing as mp
import os
import secrets
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

API_BASE = "https://api.rpow2.com"
CHALLENGE_URL = f"{API_BASE}/challenge"
WALLET_URL = f"{API_BASE}/wallet"
STATS_URL = f"{API_BASE}/stats"
SUBMIT_URL = f"{API_BASE}/mint"           # confirmed endpoint

BASE_UNITS_PER_RPOW = 1_000_000_000       # 9 decimals, Solana SPL
DAILY_CAP_RPOW = 500.0                    # per-user daily mint cap
RATE_LIMIT_PER_MIN = 10                   # server: 10 submit/min/session
SAFE_GAP_SECONDS = 7.0                    # stay under rate limit (~8.5/min)
SUFFIX_FIELD = "suffix"                   # field name in submit payload

log = logging.getLogger("rpow")


# ---------- HTTP helpers ------------------------------------------------
def _req(method: str, url: str, session: str, body: bytes | None = None,
         content_type: str | None = None) -> tuple[int, bytes]:
    headers = {
        "Cookie": f"rpow_session={session}",
        "Origin": "https://rpow2.com",
        "Referer": "https://rpow2.com/",
        "User-Agent": "Mozilla/5.0 (rpow2-native-miner)",
        "Accept": "*/*",
    }
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def pull_challenge(session: str) -> dict:
    status, body = _req("POST", CHALLENGE_URL, session)
    if status != 200:
        raise RuntimeError(f"/challenge HTTP {status}: {body[:200]!r}")
    return json.loads(body)


def fetch_wallet(session: str) -> dict | None:
    """GET /wallet — returns balance, daily cap, daily remaining."""
    try:
        status, body = _req("GET", WALLET_URL, session)
        return json.loads(body) if status == 200 else None
    except Exception:
        return None


def check_cap_reached(session: str) -> bool:
    """Return True if daily cap is reached (within 0.001 RPOW of zero)."""
    wallet = fetch_wallet(session)
    if not wallet:
        return False
    remaining_units = int(wallet.get("daily_remaining_base_units", 0))
    return remaining_units < 1_000_000  # less than 0.001 RPOW left


def submit_solution(session: str, challenge_id: str, nonce_suffix: str,
                    field_name: str = SUFFIX_FIELD) -> tuple[int, dict]:
    """POST /mint with {challenge_id, <field_name>: suffix}. Returns (status, body)."""
    payload = json.dumps({
        "challenge_id": challenge_id,
        field_name:     nonce_suffix,
    }).encode()
    status, body = _req("POST", SUBMIT_URL, session, payload,
                        content_type="application/json")
    try:
        data = json.loads(body) if body else {}
    except json.JSONDecodeError:
        data = {"raw": body[:300].decode("utf-8", "replace")}
    return status, data


def detect_suffix_field(session: str) -> str:
    """
    On first run, try known field names to detect which the server accepts.
    We submit an intentionally bogus suffix and inspect error messages — a
    validation error mentioning "suffix" vs "nonce" tells us the field name.
    """
    # Pull a cheap challenge to use as probe
    ch = pull_challenge(session)
    cid = ch["challenge_id"]
    # invalid suffix (all zeros won't match diff 25); the server will reject
    # but echo the expected field in the error
    for field in ("suffix", "nonce_suffix", "nonce", "solution"):
        status, body = submit_solution(session, cid, "0" * 16, field_name=field)
        txt = json.dumps(body).lower()
        if status == 200:
            log.info("field '%s' accepted (unexpected)", field)
            return field
        # Heuristic: server accepts the schema if error is about value
        # ("invalid proof", "difficulty not met"), rejects schema if it mentions
        # "suffix" / "required" / "missing" / "unknown field"
        if any(k in txt for k in ("proof", "difficulty", "zero", "challenge expired")):
            log.info("detected suffix field name: '%s'", field)
            return field
    log.warning("field detection inconclusive; defaulting to 'suffix'")
    return "suffix"


# ---------- SHA-256 PoW worker ------------------------------------------
def leading_zero_bits(digest: bytes) -> int:
    """Count leading zero bits in a byte string."""
    n = 0
    for b in digest:
        if b == 0:
            n += 8
            continue
        # count leading zeros in this byte
        while b < 0x80:
            n += 1
            b <<= 1
        return n
    return n


def mine_worker(prefix_hex: str, difficulty: int, stop_event,
                result_queue, worker_id: int) -> None:
    """Hash SHA256(prefix + suffix) until leading zeros >= difficulty."""
    prefix_bytes = bytes.fromhex(prefix_hex)
    # each worker gets a unique suffix space
    counter = secrets.randbits(64) | (worker_id << 56)
    hashes = 0
    t0 = time.time()

    while not stop_event.is_set():
        suffix = f"{counter:016x}"
        h = hashlib.sha256(prefix_bytes + suffix.encode()).digest()
        if leading_zero_bits(h) >= difficulty:
            result_queue.put((suffix, worker_id, hashes))
            return
        counter += 1
        hashes += 1
        if hashes % 200_000 == 0:
            rate = hashes / (time.time() - t0) / 1e6
            log.debug("worker %d rate=%.2f MH/s", worker_id, rate)


def solve_challenge(prefix_hex: str, difficulty: int, workers: int) -> str:
    """Spawn workers, return the first valid nonce_suffix found."""
    ctx = mp.get_context("spawn")
    stop_event = ctx.Event()
    result_queue = ctx.Queue()
    procs = [
        ctx.Process(target=mine_worker,
                    args=(prefix_hex, difficulty, stop_event, result_queue, i))
        for i in range(workers)
    ]
    for p in procs:
        p.start()
    try:
        suffix, wid, hashes = result_queue.get()
        log.info("solution from worker %d after %d hashes", wid, hashes)
        return suffix
    finally:
        stop_event.set()
        for p in procs:
            p.join(timeout=1)
            if p.is_alive():
                p.terminate()


# ---------- main loop ---------------------------------------------------
def mine_forever(session: str, workers: int, check_cap_every: int = 20,
                 auto_detect_field: bool = False) -> None:
    solved = 0
    rpow_earned = 0.0
    t0 = time.time()
    last_submit = 0.0

    suffix_field = SUFFIX_FIELD
    if auto_detect_field:
        log.info("probing server for correct suffix field name...")
        suffix_field = detect_suffix_field(session)

    # initial wallet check
    wallet = fetch_wallet(session)
    if wallet:
        bal = int(wallet["balance_base_units"]) / BASE_UNITS_PER_RPOW
        rem = int(wallet["daily_remaining_base_units"]) / BASE_UNITS_PER_RPOW
        log.info("wallet: balance=%.4f RPOW  daily_remaining=%.2f / %.0f RPOW",
                 bal, rem, DAILY_CAP_RPOW)
        if rem < 0.001:
            log.info("daily cap already reached — come back tomorrow")
            return

    while True:
        # periodic cap check
        if solved > 0 and solved % check_cap_every == 0:
            if check_cap_reached(session):
                log.info("daily cap reached after %d solves (+%.4f RPOW). Stopping.",
                         solved, rpow_earned)
                return

        try:
            ch = pull_challenge(session)
        except Exception as e:
            log.error("pull failed: %s — retry in 5s", e)
            time.sleep(5)
            continue

        cid = ch["challenge_id"]
        prefix = ch["nonce_prefix"]
        diff = ch["difficulty_bits"]
        expires = datetime.fromisoformat(ch["expires_at"].replace("Z", "+00:00"))
        budget = (expires - datetime.now(timezone.utc)).total_seconds()
        log.info("challenge %s  prefix=%s  diff=%d  budget=%.0fs",
                 cid[:8], prefix[:12] + "...", diff, budget)

        tstart = time.time()
        suffix = solve_challenge(prefix, diff, workers)
        dt = time.time() - tstart
        log.info("solved in %.1fs  suffix=%s", dt, suffix)

        # respect server rate limit (10/min): wait if last submit too recent
        since_last = time.time() - last_submit
        if since_last < SAFE_GAP_SECONDS:
            time.sleep(SAFE_GAP_SECONDS - since_last)

        try:
            status, result = submit_solution(session, cid, suffix, suffix_field)
        except Exception as e:
            log.error("submit failed: %s", e)
            continue

        last_submit = time.time()

        if status == 429:
            # rate limited — back off the full reset window
            log.warning("429 rate-limited, sleeping 60s")
            time.sleep(60)
            continue

        if status != 200:
            log.warning("submit HTTP %s: %s", status, result)
            continue

        # Success response format:
        #   {"token": {"id": "...", "value_base_units": "5000000", "issued_at": "..."}}
        token = result.get("token")
        if token and token.get("value_base_units"):
            value_rpow = int(token["value_base_units"]) / 1e9
            rpow_earned += value_rpow
            solved += 1
            elapsed = (time.time() - t0) / 60
            rate_per_hr = (rpow_earned / elapsed * 60) if elapsed > 0 else 0
            log.info("OK  +%.4f RPOW  token=%s  total=%d (%.3f RPOW, %.1f min, %.2f RPOW/hr)",
                     value_rpow, token["id"][:8], solved, rpow_earned, elapsed, rate_per_hr)
        else:
            log.warning("rejected or unknown response: %s", result)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    p = argparse.ArgumentParser()
    p.add_argument("--workers", type=int, default=mp.cpu_count() - 1,
                   help="number of SHA-256 worker processes")
    p.add_argument("--detect-field", action="store_true",
                   help="probe server to detect suffix field name (recommended first run)")
    args = p.parse_args()

    session = os.environ.get("RPOW_SESSION", "").strip()
    if not session:
        print("ERROR: set RPOW_SESSION env var to your cookie value",
              file=sys.stderr)
        print("  (from rpow2.com cookies: rpow_session=<VALUE>)", file=sys.stderr)
        sys.exit(2)

    log.info("rpow2 miner starting  workers=%d", args.workers)
    try:
        mine_forever(session, args.workers)
    except KeyboardInterrupt:
        log.info("stopped by user")


if __name__ == "__main__":
    main()
