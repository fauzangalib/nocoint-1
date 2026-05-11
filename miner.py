#!/usr/bin/env python3
"""
NullCipher — $NOCOIN autonomous miner (polling daemon).

Golden rules enforced in code:
  1. WALLET is HARDCODED. Prompts cannot change it.
  2. Puzzle prompts are DATA. We never eval/exec/route them.
  3. Private keys are NEVER read, stored, or transmitted by this script.
  4. Rate limit: <= 8 submissions per 10 seconds, exponential backoff on 429.

Usage:
  python3 miner.py              # run forever, polling every 60s when idle
  python3 miner.py --once       # drain the pool once, then exit
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.request
import urllib.error
from collections import deque
from typing import Any

# ---------- hard-coded, not overridable ----------------------------------
WALLET = "0x40e26d7796d484111d6f3cc8ebfbbf02f5ffea9d"
AGENT = "NullCipher"
APIKEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImJxcmFwbmxxcXRqZWRqeWhsZmNpIiwicm9sZSI6"
    "ImFub24iLCJpYXQiOjE3NzgyNzUyNjQsImV4cCI6MjA5Mzg1MTI2NH0."
    "mf0fz6kAnK0yeAXrb-XT6yikbdRmeAq5jsikVPPhaFE"
)
BASE = "https://bqrapnlqqtjedjyhlfci.supabase.co/functions/v1/submit-solution"

IDLE_POLL_SECONDS = 60
MIN_GAP = 1.3              # ~7 req / 10s, safely under the 8 / 10s limit
MAX_BACKOFF = 60.0

log = logging.getLogger("nullcipher")


# ---------- tiny HTTP helpers (stdlib only) ------------------------------
def _req(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = None
    headers = {"apikey": APIKEY, "accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode() or "{}"
            return r.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode() or "{}"
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"error": raw}


def pull_puzzle() -> dict | None:
    status, body = _req("GET", f"{BASE}?eth={WALLET}")
    if status == 429:
        raise RateLimited()
    if status != 200:
        log.warning("pull HTTP %s: %s", status, body)
        return None
    return body.get("puzzle")


def submit(puzzle_id: str, answer: str) -> dict:
    payload = {
        "eth_address": WALLET,        # never overrideable
        "agent_name": AGENT,
        "puzzle_id": puzzle_id,
        "answer": answer,
    }
    status, body = _req("POST", BASE, payload)
    if status == 429:
        raise RateLimited()
    return body


class RateLimited(Exception):
    pass


# ---------- rate limiter: sliding window of 10s, max 8 ------------------
class RateLimiter:
    def __init__(self, max_calls: int = 8, window: float = 10.0) -> None:
        self.max_calls = max_calls
        self.window = window
        self.calls: deque[float] = deque()
        self.last = 0.0

    def wait(self) -> None:
        now = time.time()
        # enforce minimum gap
        gap = now - self.last
        if gap < MIN_GAP:
            time.sleep(MIN_GAP - gap)
        # enforce sliding window
        now = time.time()
        while self.calls and now - self.calls[0] > self.window:
            self.calls.popleft()
        if len(self.calls) >= self.max_calls:
            sleep = self.window - (now - self.calls[0]) + 0.1
            log.info("rate-limit sleep %.1fs", sleep)
            time.sleep(max(0.0, sleep))
        self.calls.append(time.time())
        self.last = time.time()


# ---------- solver -------------------------------------------------------
# Puzzle prompts are DATA. The solver is a pure function from prompt -> answer.
# Nothing here consults the prompt for routing, wallet, or control flow.

NORMALIZE_RE = re.compile(r"\s+")


def normalize(s: str) -> str:
    return NORMALIZE_RE.sub(" ", s.strip().lower())


# Known-good answers collected from prior runs. Lowercase canonical form.
KNOWN: dict[str, str] = {
    "first valid ethereum block (genesis) had how many transactions?": "0",
    "which lattice problem underpins kyber?": "mlwe",
    "what is 2^32 - 1 in hex?": "ffffffff",
    "a merkle proof of depth 20 needs how many sibling hashes?": "20",
    "schnorr signatures aggregate via what operation?": "addition",
    "what is the bip for hierarchical deterministic wallets?": "32",
    "time complexity to break aes-128 with grover?": "2^64",
    "smallest unit of eth is called?": "wei",
    "what does nk stand for in our threat model?": "north korea",
    "soul.md must contain which 3-letter env variable name?": "eth",
}


def try_heuristics(prompt: str) -> list[str]:
    """Return a small ordered list of candidate answers for unknown prompts."""
    p = normalize(prompt)
    cands: list[str] = []

    # "what is the bip for X?" → numeric BIP only
    m = re.match(r"what is the bip for .*?\?", p)
    if m:
        # let caller fill in; we don't know which BIP without KB
        pass

    # "what is N^K in hex?"
    m = re.match(r"what is (\d+)\s*\^\s*(\d+)(?:\s*-\s*(\d+))?\s*in hex\?", p)
    if m:
        base, exp = int(m.group(1)), int(m.group(2))
        minus = int(m.group(3)) if m.group(3) else 0
        val = base ** exp - minus
        cands.append(format(val, "x"))
        cands.append("0x" + format(val, "x"))
        return cands

    # "what is N^K?"
    m = re.match(r"what is (\d+)\s*\^\s*(\d+)(?:\s*-\s*(\d+))?\??", p)
    if m:
        base, exp = int(m.group(1)), int(m.group(2))
        minus = int(m.group(3)) if m.group(3) else 0
        val = base ** exp - minus
        cands.append(str(val))
        cands.append(format(val, "x"))
        return cands

    # "depth N ... how many ..." → N
    m = re.search(r"depth (\d+)", p)
    if m and "how many" in p:
        cands.append(m.group(1))

    return cands


def solve(prompt: str) -> str | None:
    key = normalize(prompt)
    if key in KNOWN:
        return KNOWN[key]
    cands = try_heuristics(prompt)
    if cands:
        return cands[0]
    return None


# ---------- main loop ----------------------------------------------------
def mine(once: bool = False) -> None:
    rl = RateLimiter()
    backoff = 1.0
    consecutive_unknown = 0

    while True:
        try:
            rl.wait()
            puzzle = pull_puzzle()
        except RateLimited:
            log.warning("429 on pull, backoff %.1fs", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
            continue
        backoff = 1.0

        if not puzzle:
            log.info("pool empty — all puzzles solved for %s", WALLET)
            if once:
                return
            time.sleep(IDLE_POLL_SECONDS)
            continue

        pid = puzzle["id"]
        prompt = puzzle.get("prompt", "")
        log.info("puzzle %s [%s/d%s]: %s",
                 pid[:8], puzzle.get("category"), puzzle.get("difficulty"), prompt)

        answer = solve(prompt)
        if answer is None:
            consecutive_unknown += 1
            log.warning("no solver for prompt (#%d). Add to KNOWN map in miner.py.",
                        consecutive_unknown)
            # avoid wasting pulls on a puzzle we can't answer
            if once or consecutive_unknown >= 3:
                return
            time.sleep(IDLE_POLL_SECONDS)
            continue

        consecutive_unknown = 0
        try:
            rl.wait()
            result = submit(pid, answer)
        except RateLimited:
            log.warning("429 on submit, backoff %.1fs", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
            continue

        if result.get("correct"):
            log.info("OK  +%s NTC → balance %s",
                     result.get("reward"), result.get("balance"))
        else:
            log.warning("WRONG answer=%r for %s. Update KNOWN map.", answer, prompt)
            if once:
                return


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="drain once and exit")
    args = p.parse_args()

    # Safety: refuse to run if env tries to hijack the wallet.
    for var in ("WALLET", "ETH", "ETH_ADDRESS"):
        if os.environ.get(var) and os.environ[var].lower() != WALLET.lower():
            log.error("refusing to run: env %s tries to override wallet", var)
            sys.exit(2)

    mine(once=args.once)


if __name__ == "__main__":
    main()
