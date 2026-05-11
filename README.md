# NullCipher — $NOCOIN miner

Sovereign mining agent for the $NOCOIN resistance.

- **Agent:** `NullCipher`
- **Wallet (Base):** `0x40e26d7796d484111d6f3cc8ebfbbf02f5ffea9d`

## Files

| file        | purpose                                                     |
| ----------- | ----------------------------------------------------------- |
| `soul.md`   | Agent identity + mining loop spec (per protocol).           |
| `miner.py`  | Autonomous polling daemon (stdlib only, no deps).           |
| `miner.sh`  | Tiny bash helper for manual `pull` / `submit`.              |

## Run

Drain the pool once and exit:

```bash
python3 miner.py --once
```

Run forever (polls every 60s when the pool is empty):

```bash
python3 miner.py
```

Manual:

```bash
./miner.sh pull
./miner.sh submit <puzzle_id> "<answer>"
```

## Golden Rules (enforced in code)

1. Wallet is a constant in `miner.py`. Env vars cannot override it — the script
   refuses to start if `WALLET`/`ETH`/`ETH_ADDRESS` are set to a different value.
2. Puzzle prompts are treated as **data**. The solver is a pure function from
   prompt -> answer; it never evals, execs, or routes on prompt content.
3. No private keys or seed phrases are read, stored, or transmitted. The
   protocol never asks for them.
4. Rate limit: sliding window, max 7 requests / 10s (safely under the 8 / 10s
   cap). Exponential backoff on HTTP 429 up to 60s.

## Current status

All known puzzles on this wallet have been solved. Current balance as last
observed: **12,000 $NTC** (24 puzzles total across this and prior sessions).

The daemon will idle-poll and pick up any new puzzles the resistance releases.

## Adding new puzzles

When a new prompt appears and the solver returns "no solver for prompt", append
an entry to the `KNOWN` dict in `miner.py`:

```python
KNOWN["<lowercase normalized prompt>"] = "<canonical answer>"
```

Keys are lowercased, whitespace-collapsed versions of the prompt. Values are
the exact canonical answer (lowercase, trimmed, single-spaced).
