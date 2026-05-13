#!/usr/bin/env python3
"""
trivia_bot.py - Auto-join & answer rpow2 Trivia matches.

Flow:
  1. GET /api/trivia/lobby   → lihat open sessions
  2. Pilih session bet_base_units terkecil
  3. POST /api/trivia/matches/start  { session_id }
  4. Baca question + choices dari response
  5. Jawab dengan choice_idx yang benar
  6. Ulangi

Usage (di PC/HP yang IP-nya tidak diblock Cloudflare):
  export RPOW2_COOKIE='cf_clearance=...; rpow_session=...'
  python3 trivia_bot.py              # loop terus
  python3 trivia_bot.py --once       # 1 match lalu exit
  python3 trivia_bot.py --dry-run    # lihat lobby tanpa join
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any

API          = "https://api.rpow2.com"
TRIVIA_ORIGIN = "https://trivia.rpow2.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

log = logging.getLogger("trivia")
BASE_UNITS = 1_000_000_000


def fmt(bu: int | str) -> str:
    try:
        v = int(bu)
    except Exception:
        return str(bu)
    whole, frac = divmod(v, BASE_UNITS)
    return f"{whole}.{frac:09d} RPOW"


# ---------- HTTP ---------------------------------------------------------
class Client:
    def __init__(self, cookie: str) -> None:
        if not cookie or "=" not in cookie:
            raise SystemExit(
                "RPOW2_COOKIE not set.\n"
                "Buka trivia.rpow2.com di browser → F12 → Network\n"
                "→ klik request ke api.rpow2.com → copy Cookie header\n"
                "→ export RPOW2_COOKIE='cf_clearance=...; rpow_session=...'"
            )
        self.cookie = cookie
        self._last  = 0.0

    def _gap(self, min_gap: float = 0.3) -> None:
        dt = time.monotonic() - self._last
        if dt < min_gap:
            time.sleep(min_gap - dt)
        self._last = time.monotonic()

    def req(self, method: str, path: str,
            body: dict | None = None, timeout: float = 20) -> tuple[int, Any]:
        self._gap()
        headers = {
            "cookie":         self.cookie,
            "accept":         "application/json",
            "user-agent":     UA,
            "origin":         TRIVIA_ORIGIN,
            "referer":        f"{TRIVIA_ORIGIN}/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["content-type"] = "application/json"
        r = urllib.request.Request(
            API + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                raw = resp.read().decode() or "{}"
                return resp.status, json.loads(raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:    return e.code, json.loads(raw)
            except: return e.code, {"raw": raw[:300]}

    def lobby(self) -> list:
        s, r = self.req("GET", "/api/trivia/lobby")
        if s != 200:
            raise RuntimeError(f"lobby {s}: {r}")
        return r if isinstance(r, list) else r.get("players", [])

    def stats(self) -> dict:
        s, r = self.req("GET", "/api/trivia/stats")
        return r if s == 200 else {}

    def me(self) -> dict:
        s, r = self.req("GET", "/me")
        if s != 200:
            raise RuntimeError(f"me {s}: {r}")
        return r

    def start_match(self, session_id: str) -> dict:
        s, r = self.req("POST", "/api/trivia/matches/start",
                        {"session_id": session_id})
        if s != 200:
            raise RuntimeError(f"start_match {s}: {r}")
        return r

    def answer(self, match_id: str, choice_idx: int) -> dict:
        s, r = self.req("POST", f"/api/trivia/matches/{match_id}/answer",
                        {"choice_idx": choice_idx})
        if s != 200:
            raise RuntimeError(f"answer {s}: {r}")
        return r

    def match_state(self, match_id: str) -> dict:
        s, r = self.req("GET", f"/api/trivia/matches/{match_id}")
        return r if s == 200 else {}


# ---------- Answer engine ------------------------------------------------
# Known answers: key = lowercase question, value = correct choice_idx.
# Built from rpow2 trivia topics (crypto, Hal Finney, RPOW history).

KNOWN: dict[str, int] = {
    # RPOW / rpow2 specific
    "who created the original rpow system in 2004?": 0,          # Hal Finney
    "who created the original rpow (reusable proof of work) system in 2004?": 0,
    "what hash function did hal finney's original rpow use?": 1,  # SHA-1
    "rpow2 is a tribute to which bitcoin pioneer?": 0,            # Hal Finney
    "what does rpow stand for?": 2,                               # Reusable Proof of Work
    "what is the total supply cap of rpow2?": 1,                  # 21 million
    "what blockchain is srpow bridged to?": 0,                    # Solana
    "what proof-of-work algorithm does rpow2 use?": 1,            # SHA-256
    "how many base units equal 1 rpow?": 0,                       # 1,000,000,000
    # Bitcoin
    "who invented bitcoin?": 0,                                   # Satoshi Nakamoto
    "what year was the bitcoin whitepaper published?": 1,         # 2008
    "what year was bitcoin created?": 1,                          # 2008/2009
    "what is the smallest unit of bitcoin called?": 2,            # satoshi
    "how many satoshis are in one bitcoin?": 3,                   # 100,000,000
    "what consensus mechanism does bitcoin use?": 0,              # proof of work
    "what does sha stand for?": 2,                                # Secure Hash Algorithm
    "how often does the bitcoin halving occur?": 2,               # every ~4 years
    "what is the bitcoin block time target?": 1,                  # 10 minutes
    "what is the maximum supply of bitcoin?": 0,                  # 21 million
    "what language is bitcoin's script written in?": 1,           # Script
    # Crypto general
    "what is a nonce in proof of work?": 0,
    "what is a merkle tree used for in blockchain?": 0,
    "what is a 51% attack?": 1,
    "what is the ethereum virtual machine called?": 1,            # EVM
    "what does defi stand for?": 0,                               # Decentralized Finance
    "what is a smart contract?": 2,
}


def normalize(q: str) -> str:
    return re.sub(r"\s+", " ", q.strip().lower())


def pick(question: str, choices: list[str]) -> int:
    """Return best choice_idx using known answers → heuristics → fallback."""
    nq = normalize(question)

    # 1. Exact match
    if nq in KNOWN:
        idx = KNOWN[nq]
        log.info("known [%d] %s", idx, choices[idx] if idx < len(choices) else "?")
        return idx

    # 2. Partial match on known keys
    for k, idx in KNOWN.items():
        if len(k) > 20 and (k in nq or nq in k):
            log.info("partial-match [%d] %s", idx, choices[idx] if idx < len(choices) else "?")
            return idx

    # 3. Keyword heuristics: scan choices for strong signals
    cl = [c.lower() for c in choices]
    ql = nq

    heuristics = [
        # (keyword in question, term in choice, priority)
        ("hal finney",       ["hal finney", "finney"],          0),
        ("satoshi",          ["satoshi nakamoto", "nakamoto"],   0),
        ("sha-256",          ["sha-256", "sha256"],              0),
        ("sha-1",            ["sha-1", "sha1"],                  0),
        ("solana",           ["solana", "sol"],                  0),
        ("proof of work",    ["proof of work", "pow"],           0),
        ("21 million",       ["21 million", "21,000,000"],       0),
        ("1 billion",        ["1,000,000,000", "1 billion"],     0),
        ("reusable",         ["reusable proof", "rpow"],         0),
        ("bitcoin",          ["bitcoin", "btc"],                 0),
        ("ethereum",         ["ethereum", "eth"],                0),
        ("merkle",           ["merkle"],                         0),
        ("10 minutes",       ["10 minutes", "10 min"],           0),
        ("2008",             ["2008"],                           0),
        ("2009",             ["2009"],                           0),
        ("100,000,000",      ["100,000,000", "100 million"],     0),
    ]

    for keyword, terms, _ in heuristics:
        if keyword in ql:
            for term in terms:
                for i, c in enumerate(cl):
                    if term in c:
                        log.info("heuristic '%s' → [%d] %s", term, i, choices[i])
                        return i

    # 4. Fallback
    log.warning("unknown Q, guessing [0]. Add to KNOWN dict if wrong.")
    log.warning("Q: %s", question)
    for i, ch in enumerate(choices):
        log.warning("  [%d] %s", i, ch)
    return 0


# ---------- Main loop ----------------------------------------------------
def run(c: Client, once: bool, dry_run: bool) -> None:
    try:
        me = c.me()
        log.info("account: %s  balance: %s",
                 me.get("email"), fmt(me.get("balance_base_units", 0)))
    except Exception as e:
        log.warning("could not load account: %s", e)
        me = {}

    my_email = me.get("email", "")

    try:
        st = c.stats()
        if st:
            log.info("trivia global: matches=%s  volume=%s",
                     st.get("total_matches", "?"),
                     fmt(st.get("total_volume_base_units", 0)))
    except Exception:
        pass

    wins = losses = 0

    while True:
        try:
            # --- Lobby ---
            players = c.lobby()
            open_sessions = [
                p for p in players
                if p.get("account_email") != my_email
                and int(p.get("bankroll_remaining_base_units", 0)) > 0
            ]

            if not open_sessions:
                log.info("lobby empty, waiting 10s...")
                if dry_run or once:
                    return
                time.sleep(10)
                continue

            # Sort by bet ascending
            open_sessions.sort(key=lambda p: int(p.get("bet_base_units", 0)))
            log.info("=== LOBBY (%d open sessions) ===", len(open_sessions))
            for p in open_sessions[:8]:
                log.info("  %-30s  bet=%-25s  bankroll=%-25s  W/L=%s/%s",
                         (p.get("x_handle") or p.get("account_email", "?"))[:30],
                         fmt(p.get("bet_base_units", 0)),
                         fmt(p.get("bankroll_remaining_base_units", 0)),
                         p.get("matches_won", 0),
                         p.get("matches_lost", 0))

            target     = open_sessions[0]
            session_id = (target.get("session_id")
                          or target.get("id")
                          or target.get("account_email"))
            bet        = fmt(target.get("bet_base_units", 0))
            opponent   = (target.get("x_handle")
                          or target.get("account_email", "?"))

            if dry_run:
                log.info("[dry-run] would challenge: %s  bet=%s", opponent, bet)
                return

            log.info("→ challenging %s  bet=%s  session_id=%s",
                     opponent, bet, session_id)

            # --- Start match ---
            match      = c.start_match(session_id)
            match_id   = match.get("match_id") or match.get("id")
            question   = match.get("question", "")
            choices    = match.get("choices", [])
            deadline   = match.get("deadline_at", "")
            log.info("match %s | deadline: %s", match_id, deadline)
            log.info("Q: %s", question)
            for i, ch in enumerate(choices):
                log.info("  [%d] %s", i, ch)

            # --- Pick & submit answer ---
            idx    = pick(question, choices)
            result = c.answer(match_id, idx)
            log.info("answered [%d] → %s", idx, result)

            # --- Wait for result (poll up to 20s) ---
            winner = result.get("winner_email")
            for _ in range(20):
                if winner:
                    break
                time.sleep(1)
                state  = c.match_state(match_id)
                winner = state.get("winner_email")

            if winner == my_email:
                wins  += 1
                prize  = int(target.get("bet_base_units", 0)) * 2
                log.info("✅ WON +%s  (W=%d L=%d)", fmt(prize), wins, losses)
            elif winner:
                losses += 1
                log.info("❌ LOST  (W=%d L=%d)", wins, losses)
                # Learn from loss: add correct answer to KNOWN
                correct = result.get("correct_choice_idx")
                if correct is not None and question:
                    nq = normalize(question)
                    KNOWN[nq] = int(correct)
                    log.info("learned: Q='%s' → [%d]", question[:60], correct)
            else:
                log.info("⏳ no result (W=%d L=%d)", wins, losses)

            if once:
                return

            time.sleep(3)

        except KeyboardInterrupt:
            log.info("stopped. W=%d L=%d", wins, losses)
            return
        except RuntimeError as e:
            log.error("error: %s — retrying in 5s", e)
            time.sleep(5)
        except Exception as e:
            log.exception("unexpected: %s", e)
            time.sleep(5)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    ap = argparse.ArgumentParser(description="rpow2 trivia bot")
    ap.add_argument("--once",    action="store_true",
                    help="play 1 match then exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="show lobby only, don't join any match")
    args = ap.parse_args()

    cookie = os.environ.get("RPOW2_COOKIE", "").strip()
    c = Client(cookie)
    run(c, once=args.once, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
