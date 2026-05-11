#!/usr/bin/env python3
"""
rpow2.py - headless miner for https://rpow2.com

A tribute site to Hal Finney's original RPOW. The site exposes a browser
miner; this script performs the SAME proof-of-work from the terminal, against
the SAME public API, using an existing browser session (cookie-auth only).

Golden rules (same spirit as ./miner.py):
  1. No credentials are read, stored, or transmitted. We authenticate by
     reusing a session cookie you already obtained in a real browser after
     solving their Cloudflare Turnstile challenge. The script refuses to run
     without RPOW2_COOKIE set.
  2. The mining account is whatever your cookie says. There is no wallet
     configured here. We never call /send, /wrap, /phantom/*, or any
     endpoint that moves value.
  3. Stdlib only. No third-party packages.
  4. Polite: respects the server's COOLDOWN response, exponential backoff on
     429, a minimum gap between requests, clean Ctrl-C exit.

The proof-of-work (reverse-engineered from the site's miner.worker bundle):
  - Server POST /challenge  -> {challenge_id, nonce_prefix (hex), difficulty_bits}
  - Client finds u64 nonce such that:
        digest = sha256( prefix_bytes || nonce.to_bytes(8, 'little') )
        # interpret digest as big-endian 256-bit int
        # count its trailing zero bits
        trailing_zeros(digest) >= difficulty_bits
  - Client POST /mint  {challenge_id, solution_nonce}  -> {token: {id, value}}

Usage:
  export RPOW2_COOKIE='session=...; other=...'   # full Cookie header value
  python3 rpow2.py                  # mine forever
  python3 rpow2.py --once           # mint one token and exit
  python3 rpow2.py --workers 4      # use 4 processes
  python3 rpow2.py --selftest       # verify the PoW solver, no network
  python3 rpow2.py --status         # GET /ledger and /me, then exit
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import multiprocessing as mp
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from typing import Any

API = "https://api.rpow2.com"
UA = "rpow2-headless/1.0 (+https://github.com/)"
MIN_GAP = 0.5            # minimum seconds between HTTP calls
MAX_BACKOFF = 60.0
BASE_UNITS_PER_RPOW = 1_000_000_000  # from /ledger

log = logging.getLogger("rpow2")


# ---------- PoW primitives ----------------------------------------------
def trailing_zero_bits(digest: bytes) -> int:
    """Count trailing zero bits of a digest, using the exact convention the
    site's worker uses (BA() in miner.worker.js): scan bytes from the last
    index backward; for each all-zero byte add 8; for the first non-zero byte
    add the index of its lowest set bit (bit 0 = value 1 = LSB).
    Equivalent to: count trailing zeros of int.from_bytes(digest, 'big').
    """
    g = 0
    for i in range(len(digest) - 1, -1, -1):
        e = digest[i]
        if e == 0:
            g += 8
            continue
        c = 0
        while not (e & (1 << c)):
            c += 1
        return g + c
    return g


def trailing_zero_bits_fast(digest: bytes) -> int:
    n = int.from_bytes(digest, "big")
    if n == 0:
        return len(digest) * 8
    return (n & -n).bit_length() - 1


def solve(prefix: bytes, bits: int, start: int = 0, stride: int = 1,
          stop: "mp.Event | None" = None,
          progress_every: int = 1 << 18) -> tuple[int, int] | None:
    """Find nonce such that sha256(prefix || nonce_u64_le) has >= bits
    trailing zeros. Returns (nonce, hashes_done) or None if aborted."""
    mask = (1 << bits) - 1
    buf = bytearray(prefix) + bytearray(8)
    plen = len(prefix)
    n = start
    hashes = 0
    sha = hashlib.sha256
    while True:
        if stop is not None and hashes % progress_every == 0 and stop.is_set():
            return None
        # write nonce as little-endian u64 into buf[plen:plen+8]
        buf[plen + 0] = n & 0xFF
        buf[plen + 1] = (n >> 8) & 0xFF
        buf[plen + 2] = (n >> 16) & 0xFF
        buf[plen + 3] = (n >> 24) & 0xFF
        buf[plen + 4] = (n >> 32) & 0xFF
        buf[plen + 5] = (n >> 40) & 0xFF
        buf[plen + 6] = (n >> 48) & 0xFF
        buf[plen + 7] = (n >> 56) & 0xFF
        d = sha(bytes(buf)).digest()
        hashes += 1
        # check: last `bits` bits of big-endian integer are zero.
        # That equals: the low `bits` bits of int.from_bytes(d, 'big') are 0.
        if bits <= 8:
            if (d[-1] & mask) == 0:
                return n, hashes
        else:
            if int.from_bytes(d, "big") & mask == 0:
                return n, hashes
        n += stride


# ---------- multi-core driver -------------------------------------------
def _worker(prefix: bytes, bits: int, start: int, stride: int,
            stop: "mp.Event", out: "mp.Queue") -> None:
    try:
        res = solve(prefix, bits, start=start, stride=stride, stop=stop)
        if res is not None:
            out.put(res)
    except Exception as e:  # pragma: no cover - best-effort
        out.put(("error", repr(e)))


def solve_parallel(prefix: bytes, bits: int, workers: int) -> tuple[int, int]:
    if workers <= 1:
        res = solve(prefix, bits)
        assert res is not None
        return res
    ctx = mp.get_context("spawn")
    stop = ctx.Event()
    q: mp.Queue = ctx.Queue()
    procs = [
        ctx.Process(target=_worker, args=(prefix, bits, i, workers, stop, q),
                    daemon=True)
        for i in range(workers)
    ]
    for p in procs:
        p.start()
    try:
        first = q.get()
    finally:
        stop.set()
        for p in procs:
            p.join(timeout=2)
            if p.is_alive():
                p.terminate()
    if isinstance(first, tuple) and len(first) == 2 and isinstance(first[0], int):
        return first  # (nonce, hashes_in_that_worker)
    raise RuntimeError(f"worker error: {first!r}")


# ---------- HTTP client -------------------------------------------------
class ApiError(Exception):
    def __init__(self, status: int, body: Any):
        super().__init__(f"HTTP {status}: {body!r}")
        self.status = status
        self.body = body


class Client:
    def __init__(self, cookie: str) -> None:
        if not cookie or "=" not in cookie:
            raise SystemExit(
                "RPOW2_COOKIE not set. Log in to https://rpow2.com in a "
                "browser, then copy the Cookie header for api.rpow2.com "
                "(DevTools -> Application -> Cookies)."
            )
        self.cookie = cookie
        self._last = 0.0

    def _gap(self) -> None:
        dt = time.monotonic() - self._last
        if dt < MIN_GAP:
            time.sleep(MIN_GAP - dt)
        self._last = time.monotonic()

    def _req(self, method: str, path: str,
             body: dict | None = None) -> tuple[int, Any]:
        self._gap()
        data = None
        headers = {
            "accept": "application/json",
            "user-agent": UA,
            "cookie": self.cookie,
            "origin": "https://rpow2.com",
            "referer": "https://rpow2.com/",
        }
        if body is not None:
            data = json.dumps(body).encode()
            headers["content-type"] = "application/json"
        req = urllib.request.Request(API + path, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read().decode() or "{}"
                return r.status, json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode() or "{}"
            try:
                parsed = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                parsed = {"error": raw}
            return e.code, parsed

    # endpoints we actually use. No /send, /wrap, /phantom/*.
    def ledger(self) -> dict:
        s, b = self._req("GET", "/ledger")
        if s != 200:
            raise ApiError(s, b)
        return b

    def me(self) -> dict:
        s, b = self._req("GET", "/me")
        if s != 200:
            raise ApiError(s, b)
        return b

    def challenge(self) -> dict:
        s, b = self._req("POST", "/challenge")
        if s != 200:
            raise ApiError(s, b)
        return b

    def mint(self, challenge_id: str, solution_nonce: int) -> dict:
        s, b = self._req("POST", "/mint", {
            "challenge_id": challenge_id,
            "solution_nonce": str(solution_nonce),
        })
        if s != 200:
            raise ApiError(s, b)
        return b


# ---------- mining loop -------------------------------------------------
def fmt_rpow(base_units: int | str) -> str:
    try:
        bu = int(base_units)
    except (TypeError, ValueError):
        return str(base_units)
    whole, frac = divmod(bu, BASE_UNITS_PER_RPOW)
    return f"{whole}.{frac:09d} RPOW"


def print_status(c: Client) -> None:
    led = c.ledger()
    log.info("ledger: difficulty=%s bits, reward=%s, users=%s, minted=%s/%s",
             led.get("current_difficulty_bits"),
             fmt_rpow(led.get("current_reward_base_units", 0)),
             led.get("user_count"),
             fmt_rpow(led.get("minted_supply_counter_base_units", 0)),
             fmt_rpow(led.get("max_supply_base_units", 0)))
    try:
        me = c.me()
        log.info("me: email=%s balance=%s tokens=%s",
                 me.get("email"),
                 fmt_rpow(me.get("balance_base_units", 0)),
                 me.get("token_count", me.get("tokens_minted", "?")))
    except ApiError as e:
        log.warning("me: %s", e)


def mine_one(c: Client, workers: int) -> dict | None:
    ch = c.challenge()
    cid = ch["challenge_id"]
    prefix_hex = ch["nonce_prefix"]
    bits = int(ch["difficulty_bits"])
    prefix = bytes.fromhex(prefix_hex)
    log.info("challenge %s: bits=%d prefix=%s", cid[:8], bits, prefix_hex)

    t0 = time.monotonic()
    nonce, hashes = solve_parallel(prefix, bits, workers)
    dt = time.monotonic() - t0
    rate = hashes / dt if dt > 0 else 0.0
    log.info("solved in %.2fs, nonce=%d, ~%.2f MH/s (this worker)",
             dt, nonce, rate / 1e6)

    # sanity check before we bother the server
    buf = prefix + nonce.to_bytes(8, "little")
    d = hashlib.sha256(buf).digest()
    tz = trailing_zero_bits(d)
    if tz < bits:
        log.error("local verify FAILED: %d < %d trailing zeros", tz, bits)
        return None

    res = c.mint(cid, nonce)
    tok = res.get("token", {})
    log.info("MINT ok: id=%s value=%s",
             tok.get("id", "?"), fmt_rpow(tok.get("value", 0)))
    return res


def mine_loop(c: Client, workers: int, once: bool) -> None:
    backoff = 1.0
    minted = 0
    while True:
        try:
            res = mine_one(c, workers)
            if res is not None:
                minted += 1
                backoff = 1.0
                if once:
                    return
        except ApiError as e:
            b = e.body if isinstance(e.body, dict) else {}
            err = b.get("error")
            if err == "COOLDOWN":
                wait = float(b.get("retry_after", 5))
                log.info("server COOLDOWN, sleeping %.1fs", wait)
                time.sleep(wait)
                continue
            if e.status == 429:
                log.warning("429, backoff %.1fs", backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue
            if e.status == 401:
                log.error("401 unauthorized. Your RPOW2_COOKIE is stale; "
                          "log in again in the browser and re-export it.")
                return
            log.error("api error: %s", e)
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
        except KeyboardInterrupt:
            log.info("stopped by user after %d token(s)", minted)
            return


# ---------- self-test ---------------------------------------------------
def selftest() -> int:
    # (1) two implementations of trailing_zero_bits agree on random digests
    import random
    random.seed(0xC0FFEE)
    for _ in range(2000):
        b = bytes(random.randint(0, 255) for _ in range(32))
        a = trailing_zero_bits(b)
        c = trailing_zero_bits_fast(b)
        if a != c:
            print("MISMATCH", b.hex(), a, c)
            return 1
    # edge cases
    assert trailing_zero_bits(b"\x00" * 32) == 256
    assert trailing_zero_bits(b"\x00" * 31 + b"\x01") == 0
    assert trailing_zero_bits(b"\x00" * 31 + b"\x02") == 1
    assert trailing_zero_bits(b"\x01" + b"\x00" * 31) == 248

    # (2) end-to-end: solve a low-difficulty challenge, verify via the
    #     independent (byte-scan) verifier.
    prefix = bytes.fromhex("deadbeefcafef00d")
    for bits in (8, 12, 16, 20):
        res = solve(prefix, bits)
        assert res is not None
        nonce, _ = res
        d = hashlib.sha256(prefix + nonce.to_bytes(8, "little")).digest()
        tz = trailing_zero_bits(d)
        assert tz >= bits, (bits, tz, nonce, d.hex())
        # and: nonces below must NOT have satisfied it (spot-check around)
        for k in (1, 2, 3):
            if nonce - k >= 0:
                dd = hashlib.sha256(
                    prefix + (nonce - k).to_bytes(8, "little")).digest()
                assert trailing_zero_bits(dd) < bits or (nonce - k) == 0
        print(f"  bits={bits:3d} nonce={nonce:>10d} digest={d.hex()} tz={tz}")
    print("selftest OK")
    return 0


# ---------- main --------------------------------------------------------
def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    p = argparse.ArgumentParser(description="headless rpow2.com miner")
    p.add_argument("--once", action="store_true",
                   help="mint one token and exit")
    p.add_argument("--workers", type=int, default=1,
                   help="number of parallel solver processes (default 1)")
    p.add_argument("--selftest", action="store_true",
                   help="run offline PoW solver self-tests and exit")
    p.add_argument("--status", action="store_true",
                   help="show /ledger and /me then exit")
    args = p.parse_args()

    if args.selftest:
        return selftest()

    cookie = os.environ.get("RPOW2_COOKIE", "").strip()
    c = Client(cookie)

    if args.status:
        print_status(c)
        return 0

    print_status(c)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    mine_loop(c, max(1, args.workers), args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
