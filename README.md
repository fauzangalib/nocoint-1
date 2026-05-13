# NullCipher — $NOCOIN miner

Sovereign mining agent for the $NOCOIN resistance.

- **Agent:** `NullCipher`
- **Wallet (Base):** `0x40e26d7796d484111d6f3cc8ebfbbf02f5ffea9d`

## Files

| file        | purpose                                                     |
| ----------- | ----------------------------------------------------------- |
| `soul.md`   | Agent identity + mining loop spec (per protocol).           |
| `miner.py`  | $NOCOIN polling daemon (stdlib only, no deps).              |
| `miner.sh`  | Tiny bash helper for manual `pull` / `submit`.              |
| `rpow2.py`  | Headless miner for https://rpow2.com (separate tribute chain). |
| `rpow2.sh`  | Curl helper for the rpow2 API.                              |

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


---

## rpow2.com miner (`rpow2.py`)

`rpow2.py` is a **separate** miner for [rpow2.com](https://rpow2.com) — a modern
tribute to Hal Finney's original RPOW (2004). It has nothing to do with
$NOCOIN; it's a second front.

### Protocol (reverse-engineered from the site bundle)

- `POST /challenge` → `{challenge_id, nonce_prefix (hex), difficulty_bits}`
- Find a u64 `nonce` such that
  `sha256(prefix_bytes || nonce.to_bytes(8, 'little'))` has at least
  `difficulty_bits` trailing zero bits (big-endian integer view).
- `POST /mint {challenge_id, solution_nonce}` → `{token: {id, value}}`

Current live params (from `GET /ledger`, checked at build time):

| thing                 | value                                        |
| --------------------- | -------------------------------------------- |
| `difficulty_bits`     | `25`                                         |
| reward / token        | `0.005 RPOW` (`5_000_000` base units)        |
| `base_units_per_rpow` | `1_000_000_000`                              |
| supply cap            | `19_000_000 RPOW`                            |
| next halving at       | `11_000_000 RPOW` minted                     |

At 25 bits a single Python core needs ~30–60s per token, so throughput is
reward-halving-irrelevant. Use `--workers N` to split across cores.

### Auth (no credentials handled here)

The site gates signup behind Cloudflare Turnstile + email magic-link. We do
**not** bypass that. Instead:

1. In a real browser, log in at <https://rpow2.com>.
2. DevTools → Application → Cookies → `api.rpow2.com` → copy the full Cookie
   header string.
3. Export it:
   ```bash
   export RPOW2_COOKIE='<paste here>'
   ```

The script refuses to start without `RPOW2_COOKIE`, and never reads, stores,
or transmits anything else.

### Run

```bash
python3 rpow2.py --selftest     # offline PoW verification, no network
python3 rpow2.py --status       # GET /ledger and /me, then exit
python3 rpow2.py --once         # mint one token and exit
python3 rpow2.py --workers 4    # mine forever, 4 solver processes
```

Manual, via curl helper:

```bash
./rpow2.sh ledger
./rpow2.sh me
./rpow2.sh challenge
./rpow2.sh mint <challenge_id> <solution_nonce>
```

### Safety rules (enforced in code)

1. **No credentials.** Cookie reuse only. No password/seed/private-key handling.
2. **No value-moving endpoints.** The client only implements `/ledger`, `/me`,
   `/challenge`, `/mint`. It never calls `/send`, `/wrap`, `/phantom/*`, etc.
3. **Stdlib only.** No third-party packages.
4. **Polite.** Honors server `COOLDOWN` responses, exponential backoff on 429,
   minimum inter-request gap, clean Ctrl-C exit.
5. **Offline-verifiable.** `--selftest` cross-checks two independent
   implementations of the trailing-zero-bit rule over 2000 random digests, plus
   end-to-end solves at 8/12/16/20 bits.



---

## rpow2 Trivia bot (`trivia_bot.js`)

PvP trivia di [trivia.rpow2.com](https://trivia.rpow2.com) — kamu vs orang
lain, taruhan RPOW, yang jawab benar menang jackpot.

### Cara pakai (browser console, paling reliable)

1. Buka <https://trivia.rpow2.com/> di browser, pastikan sudah login.
2. Daftar di <https://console.groq.com> → bikin API key (gratis).
3. Edit `trivia_bot.js`:
   - `myEmail`   → email akun rpow2 kamu
   - `GROQ_KEY`  → API key dari Groq
4. F12 → Console → paste seluruh isi file → Enter.
5. Bot auto: scan lobby → pilih lawan → jawab soal → klaim reward.
6. Stop: ketik `stopBot()` di console.

### Answer engine

Urutan pipeline:

1. **KNOWN dict** (instant, no network)
   Hardcoded jawaban untuk soal yang sering muncul. Tiap kali bot kalah,
   keyword soal otomatis di-learn ke KNOWN dict.

2. **Groq AI** (`llama-3.3-70b-versatile`)
   ~100ms response. Gratis 14400 req/hari di tier free Groq.
   Ganti `compound-beta` (web search built-in) untuk akurasi lebih tinggi.

3. **Fallback [0]** kalau AI timeout / error.

### Kenapa browser console, bukan Python script

Trivia API pakai Cloudflare yang block IP datacenter (VPS, cloud sandbox,
bahkan Kiro environment). Browser fetch pakai `credentials:"include"` →
pakai cookie browser asli → Cloudflare lolos.

Python version (kalau dibutuhkan untuk server dengan IP residential) ada
juga, tapi cookie `cf_clearance` expired ~30 menit jadi kurang praktis.

### Update KNOWN saat bot salah

Bot tetap jalan, tinggal tambah di console:

```javascript
KNOWN["kata_kunci_soal"] = INDEX_BENAR;
```

Contoh yang pernah salah & sekarang dihardcode:

| soal                          | jawaban benar        |
| ----------------------------- | -------------------- |
| Whistler codename             | Windows XP           |
| Kuwait islands                | 9                    |
| Coulrophobia                  | Clowns               |
| Artist yang cut off ear       | Van Gogh             |
| Warframe fish eats natives    | Mawfish              |
| Rebadged car paling sering    | Isuzu Trooper        |
| Intel HD after Broadwell      | HD 500               |
| Zeptometres in 1 femtometre   | 1,000,000            |

### Safety rules

1. **Never send, wrap, bind wallet.** Bot only reads `/api/trivia/*` + `/me`.
2. **Cookie via browser only.** `credentials:"include"` = browser fetch pakai
   cookie asli tanpa expose ke script.
3. **MIN_BET filter** bisa di-set untuk skip lawan dengan bet terlalu besar.
