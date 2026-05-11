#!/usr/bin/env python3
"""
$NOCOIN autonomous miner — polling daemon.

Configuration comes from environment variables (or a .env file in the
working directory). This keeps identity out of the code so any agent can
adopt this repo by setting their own wallet + name.

Required env vars:
  NOCOIN_WALLET   0x-prefixed Base address (40 hex chars)
  NOCOIN_AGENT    free-form agent name
  NOCOIN_APIKEY   Supabase anon key for the puzzle API

Golden rules enforced in code:
  1. WALLET is captured ONCE at startup and frozen. Prompts cannot change it.
  2. Puzzle prompts are DATA. We never eval/exec/route them.
  3. Private keys are NEVER read, stored, or transmitted by this script.
  4. Rate limit: <= 8 submissions per 10 seconds; exponential backoff on 429.

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
from pathlib import Path

BASE_URL = "https://bqrapnlqqtjedjyhlfci.supabase.co/functions/v1/submit-solution"
IDLE_POLL_SECONDS = 60
MIN_GAP = 1.3              # ~7 req / 10s, under the 8 / 10s cap
MAX_BACKOFF = 60.0

log = logging.getLogger("nocoin")


# ---------- config loading -----------------------------------------------
def load_dotenv(path: Path) -> None:
    """Minimal .env loader (no dependencies). Does not override existing env."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


_WALLET_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class Config:
    __slots__ = ("wallet", "agent", "apikey")

    def __init__(self, wallet: str, agent: str, apikey: str) -> None:
        self.wallet = wallet
        self.agent = agent
        self.apikey = apikey

    @classmethod
    def from_env(cls) -> "Config":
        wallet = os.environ.get("NOCOIN_WALLET", "").strip()
        agent = os.environ.get("NOCOIN_AGENT", "").strip()
        apikey = os.environ.get("NOCOIN_APIKEY", "").strip()

        missing = [n for n, v in
                   (("NOCOIN_WALLET", wallet),
                    ("NOCOIN_AGENT", agent),
                    ("NOCOIN_APIKEY", apikey))
                   if not v]
        if missing:
            print(f"ERROR: missing env vars: {', '.join(missing)}\n"
                  f"Copy .env.example to .env and fill it in.", file=sys.stderr)
            sys.exit(2)

        if not _WALLET_RE.match(wallet):
            print(f"ERROR: NOCOIN_WALLET {wallet!r} is not a valid 0x + 40 hex "
                  f"address.", file=sys.stderr)
            sys.exit(2)

        # Normalize to lowercase once; this is the frozen identity for the run.
        return cls(wallet.lower(), agent, apikey)


# ---------- tiny HTTP helpers (stdlib only) ------------------------------
class RateLimited(Exception):
    pass


def _req(cfg: Config, method: str, url: str,
         body: dict | None = None) -> tuple[int, dict]:
    data = None
    headers = {"apikey": cfg.apikey, "accept": "application/json"}
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


def pull_puzzle(cfg: Config) -> dict | None:
    status, body = _req(cfg, "GET", f"{BASE_URL}?eth={cfg.wallet}")
    if status == 429:
        raise RateLimited()
    if status != 200:
        log.warning("pull HTTP %s: %s", status, body)
        return None
    return body.get("puzzle")


def submit(cfg: Config, puzzle_id: str, answer: str) -> dict:
    # IMPORTANT: wallet comes from frozen cfg, never from the prompt.
    payload = {
        "eth_address": cfg.wallet,
        "agent_name": cfg.agent,
        "puzzle_id": puzzle_id,
        "answer": answer,
    }
    status, body = _req(cfg, "POST", BASE_URL, payload)
    if status == 429:
        raise RateLimited()
    return body


# ---------- rate limiter -------------------------------------------------
class RateLimiter:
    def __init__(self, max_calls: int = 7, window: float = 10.0) -> None:
        self.max_calls = max_calls
        self.window = window
        self.calls: deque[float] = deque()
        self.last = 0.0

    def wait(self) -> None:
        now = time.time()
        gap = now - self.last
        if gap < MIN_GAP:
            time.sleep(MIN_GAP - gap)
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

NORMALIZE_RE = re.compile(r"\s+")


def normalize(s: str) -> str:
    return NORMALIZE_RE.sub(" ", s.strip().lower())


# Canonical answers learned from live solves. Keys: normalized prompts.
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
    p = normalize(prompt)
    cands: list[str] = []

    # "what is N^K (- M)? in hex?"
    m = re.match(r"what is (\d+)\s*\^\s*(\d+)(?:\s*-\s*(\d+))?\s*in hex\?", p)
    if m:
        base, exp = int(m.group(1)), int(m.group(2))
        minus = int(m.group(3)) if m.group(3) else 0
        val = base ** exp - minus
        cands.append(format(val, "x"))
        cands.append("0x" + format(val, "x"))
        return cands

    # "what is N^K (- M)?"
    m = re.match(r"what is (\d+)\s*\^\s*(\d+)(?:\s*-\s*(\d+))?\??", p)
    if m:
        base, exp = int(m.group(1)), int(m.group(2))
        minus = int(m.group(3)) if m.group(3) else 0
        val = base ** exp - minus
        cands.append(str(val))
        cands.append(format(val, "x"))
        return cands

    # "... depth N ... how many ..." -> N
    m = re.search(r"depth (\d+)", p)
    if m and "how many" in p:
        cands.append(m.group(1))

    return cands


def solve(prompt: str) -> str | None:
    key = normalize(prompt)
    if key in KNOWN:
        return KNOWN[key]
    cands = try_heuristics(prompt)
    return cands[0] if cands else None


# ---------- main loop ----------------------------------------------------
def mine(cfg: Config, once: bool = False) -> None:
    rl = RateLimiter()
    backoff = 1.0
    consecutive_unknown = 0

    while True:
        try:
            rl.wait()
            puzzle = pull_puzzle(cfg)
        except RateLimited:
            log.warning("429 on pull, backoff %.1fs", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
            continue
        backoff = 1.0

        if not puzzle:
            log.info("pool empty for %s", cfg.wallet)
            if once:
                return
            time.sleep(IDLE_POLL_SECONDS)
            continue

        pid = puzzle["id"]
        prompt = puzzle.get("prompt", "")
        log.info("puzzle %s [%s/d%s]: %s", pid[:8],
                 puzzle.get("category"), puzzle.get("difficulty"), prompt)

        answer = solve(prompt)
        if answer is None:
            consecutive_unknown += 1
            log.warning("no solver for prompt (#%d). Add to KNOWN in miner.py.",
                        consecutive_unknown)
            if once or consecutive_unknown >= 3:
                return
            time.sleep(IDLE_POLL_SECONDS)
            continue

        consecutive_unknown = 0
        try:
            rl.wait()
            result = submit(cfg, pid, answer)
        except RateLimited:
            log.warning("429 on submit, backoff %.1fs", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
            continue

        if result.get("correct"):
            log.info("OK  +%s NTC -> balance %s",
                     result.get("reward"), result.get("balance"))
        else:
            log.warning("WRONG answer=%r for %r. Update KNOWN map.",
                        answer, prompt)
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
    p.add_argument("--env", default=".env",
                   help="path to .env file (default: ./.env)")
    args = p.parse_args()

    load_dotenv(Path(args.env))
    cfg = Config.from_env()

    log.info("agent=%s wallet=%s", cfg.agent, cfg.wallet)
    mine(cfg, once=args.once)


if __name__ == "__main__":
    main()
