// ============================================================
// RPOW2 TRIVIA BOT - Browser Console Version
// ============================================================
//
// Cara pakai:
//   1. Buka https://trivia.rpow2.com/ di browser (sudah login)
//   2. F12 → Console
//   3. Ganti myEmail & GROQ_KEY di bawah
//   4. Paste semua → Enter
//   5. Stop: ketik `stopBot()` di console
//
// Kenapa via browser console (bukan Python):
//   Trivia API pakai Cloudflare yang block IP datacenter.
//   Di browser pakai `credentials: "include"` → otomatis pakai
//   cookie browser → Cloudflare ga block.
//
// Update KNOWN saat bot salah:
//   KNOWN["kata_kunci_soal"] = INDEX_BENAR;
// ============================================================

const api       = "https://api.rpow2.com";
const myEmail   = "youremail@gmail.com";                                // ← GANTI email kamu
const GROQ_KEY  = "gsk_YOUR_GROQ_API_KEY_HERE";                         // ← dari console.groq.com (gratis)
const MIN_BET   = 0;  // set ke 100000000 (0.1 RPOW) untuk filter bet gede

let running = true, wins = 0, losses = 0, timeouts = 0;
function stopBot() { running = false; console.log(`STOPPED W=${wins} L=${losses} ⏳=${timeouts}`); }

// ============================================================
// KNOWN ANSWERS - soal yang pernah salah, supaya tidak salah lagi
// ============================================================
const KNOWN = {
  // RPOW / Bitcoin / Crypto
  "hal finney":           0,
  "rpow":                 0,
  "satoshi":              0,
  "bitcoin":              0,
  "sha-256":              1,
  "proof of work":        0,

  // Sudah pernah salah - sekarang dihardcode
  "whistler":             0,   // Windows XP
  "windows xp":           0,
  "kuwait":               1,   // 9 islands
  "how many islands":     1,
  "not exclusively female": 0, // Clefairy
  "clefairy":             0,
  "battery coined":       2,   // Benjamin Franklin
  "benjamin franklin":    2,
  "electrical storage":   2,
  "sonic 2 level select": 2,   // Lead Programmer's birthday
  "intel hd":             1,   // HD Graphics 500 (after Broadwell)
  "intel hd broadwell":   1,
  "zeptometre":           0,   // 1,000,000
  "femtometre":           0,
  "rebadged":             3,   // Isuzu Trooper
  "badge-engineered":     3,
  "warframe":             3,   // Mawfish
  "mawfish":              3,
  "cetus":                3,
  "eats unwary natives":  3,

  // Umum
  "sega mascot":          2,   // Sonic
  "sonic the hedgehog":   2,
  "linus torvalds":       0,   // Linux
  "linux":                0,
  "van gogh":             3,
  "cut off his ear":      3,
  "coulrophobia":         3,   // Clowns
  "spanish flu":          1,   // 1-3%
  "ridley scott":         2,
  "alien":                2,
  "evangelion":           0,   // A Cruel Angel's Thesis
};

// ============================================================
// HTTP helpers (pakai credentials:"include" biar Cloudflare ga block)
// ============================================================
async function req(method, path, body) {
  const res = await fetch(api + path, {
    method,
    credentials: "include",
    headers: {
      "content-type": "application/json",
      "accept":       "application/json"
    },
    body: body ? JSON.stringify(body) : undefined
  });
  return res.json();
}

// ============================================================
// Answer engine
//   1. KNOWN dict (instant)
//   2. Groq AI (llama-3.3-70b, fast & decent)
//   3. Fallback [0]
// ============================================================
async function askAI(question, choices) {
  // 1. KNOWN dict check
  const ql = question.toLowerCase();
  for (const [kw, idx] of Object.entries(KNOWN)) {
    if (ql.includes(kw) && idx < choices.length) {
      console.log(`  known[${idx}] ${choices[idx]}`);
      return idx;
    }
  }

  // 2. Groq AI
  try {
    const res = await fetch("https://api.groq.com/openai/v1/chat/completions", {
      method: "POST",
      headers: {
        "content-type":  "application/json",
        "authorization": `Bearer ${GROQ_KEY}`
      },
      body: JSON.stringify({
        model: "llama-3.3-70b-versatile",
        messages: [{
          role: "user",
          content: `Trivia question. Reply ONLY with 0, 1, 2, or 3. No explanation.

Q: ${question}
[0] ${choices[0]}
[1] ${choices[1]}
[2] ${choices[2]}
[3] ${choices[3]}`
        }],
        max_tokens: 3,
        temperature: 0
      })
    });
    const d = await res.json();
    const idx = parseInt(d.choices?.[0]?.message?.content?.trim());
    if (idx >= 0 && idx <= 3) {
      console.log(`  AI[${idx}] ${choices[idx]}`);
      return idx;
    }
  } catch (e) {
    console.log("  AI err:", e.message);
  }

  console.log("  fallback[0]");
  return 0;
}

async function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// ============================================================
// Main match loop
// ============================================================
async function playOne() {
  const lobby = await req("GET", "/api/trivia/lobby");

  // Filter: bet >= MIN_BET, bukan akun sendiri
  let players = (lobby.players || [])
    .filter(p => p.account_email !== myEmail && p.bet_base_units >= MIN_BET)
    .sort((a, b) => b.bet_base_units - a.bet_base_units);

  // Fallback: kalau tidak ada yang memenuhi MIN_BET, ambil bet terbesar yang ada
  if (!players.length) {
    const all = (lobby.players || [])
      .filter(p => p.account_email !== myEmail)
      .sort((a, b) => b.bet_base_units - a.bet_base_units);
    if (!all.length) { console.log("lobby empty..."); await sleep(5000); return; }
    players = [all[0]];
  }

  const t   = players[0];
  const bet = t.bet_base_units / 1e9;
  console.log(`\nvs @${t.x_handle} bet=${bet} W/L=${t.matches_won}/${t.matches_lost}`);

  // Start match
  const match = await req("POST", "/api/trivia/matches/start", { session_id: t.session_id });
  if (!match.match_id && !match.id) {
    console.log("start fail:", match);
    await sleep(3000);
    return;
  }

  const mid = match.match_id || match.id;
  const q   = match.question || "";
  const ch  = match.choices  || [];
  console.log(`Q: ${q}`);
  ch.forEach((c, i) => console.log(`  [${i}] ${c}`));

  // Pick & submit answer
  const idx = await askAI(q, ch);
  console.log(`>>> [${idx}] ${ch[idx]}`);
  await req("POST", `/api/trivia/matches/${mid}/answer`, { choice_idx: idx });

  // Poll hasil (500ms × 40 = 20 detik max)
  let winner = null, ci = null;
  for (let i = 0; i < 40; i++) {
    await sleep(500);
    const st = await req("GET", `/api/trivia/matches/${mid}`);
    winner = st.winner_email;
    ci     = st.correct_choice_idx;
    if (winner) break;
  }

  if (ci !== null && ci !== undefined) console.log(`correct:[${ci}] ${ch[ci]}`);

  if (winner === myEmail) {
    wins++;
    console.log(`✅ WON +${bet * 2} RPOW (W=${wins} L=${losses})`);
  } else if (winner) {
    losses++;
    console.log(`❌ LOST (W=${wins} L=${losses})`);
    // Auto-learn: simpan keyword dari soal yang salah → jawaban benar
    if (ci !== null && ci !== undefined) {
      const words = (q.toLowerCase().match(/[a-z]{5,}/g) || []).slice(0, 2);
      words.forEach(w => {
        KNOWN[w] = ci;
        console.log(`  learned:"${w}"→[${ci}]`);
      });
    }
  } else {
    timeouts++;
    console.log(`⏳ timeout`);
  }

  const me = await req("GET", "/me");
  console.log(`balance: ${(me.balance_base_units / 1e9).toFixed(3)} RPOW`);
}

// ============================================================
// Main loop
// ============================================================
(async () => {
  const me = await req("GET", "/me");
  console.log(`=== RPOW2 TRIVIA BOT ===`);
  console.log(`${me.email}  balance: ${(me.balance_base_units / 1e9).toFixed(3)} RPOW`);
  console.log(`KNOWN: ${Object.keys(KNOWN).length} entries | MIN_BET: ${MIN_BET / 1e9} RPOW`);
  console.log(`stop: stopBot()`);

  while (running) {
    try {
      await playOne();
    } catch (e) {
      console.error("err:", e.message);
      await sleep(2000);
    }
  }
})();
