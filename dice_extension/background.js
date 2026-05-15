// Background service worker
const BET = 100000000; // 0.1 RPOW
let running = false, wins = 0, losses = 0;

function rndSeed() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)))
    .map(b => b.toString(16).padStart(2,"0")).join("");
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function halFetch(method, path, body) {
  return new Promise(resolve => {
    chrome.tabs.query({ url: "https://halstavern.net/*" }, tabs => {
      if (!tabs.length) { resolve({ ok: false, error: "no halstavern tab open!" }); return; }
      chrome.tabs.sendMessage(tabs[0].id, { type: "HAL_FETCH", method, path, body }, r => resolve(r || { ok: false }));
    });
  });
}

async function rpowFetch(method, path, body) {
  return new Promise(resolve => {
    chrome.tabs.query({ url: "https://rpow2.com/*" }, tabs => {
      if (!tabs.length) { resolve({ ok: false, error: "no rpow2.com tab open!" }); return; }
      chrome.tabs.sendMessage(tabs[0].id, { type: "RPOW_FETCH", method, path, body }, r => resolve(r || { ok: false }));
    });
  });
}

async function trackBet(betId) {
  for (let i = 0; i < 60; i++) {
    await sleep(3000);
    try {
      const poll = await halFetch("GET", `/api/bets/${betId}`);
      const b = poll?.data?.bet;
      if (!b) continue;
      if (b.status !== "pending") {
        const payout = parseInt(b.payout_base_units || 0);
        if (payout > BET) {
          wins++;
          console.log(`[DiceBot] WIN +${(payout-BET)/1e9} RPOW (W=${wins} L=${losses})`);
        } else {
          losses++;
          console.log(`[DiceBot] LOSS (W=${wins} L=${losses})`);
        }
        chrome.storage.local.set({ wins, losses, running });
        return;
      }
    } catch(e) { /* retry silently */ }
  }
  console.log(`[DiceBot] bet ${betId.slice(0,8)} timeout`);
}

async function runBot() {
  running = true;
  console.log("[DiceBot] Starting - over 9, bet=0.1 RPOW");

  while (running) {
    try {
      // 1. Place bet
      const betRes = await halFetch("POST", "/api/bets", {
        game_slug: "dice",
        stake_base_units: String(BET),
        client_seed: rndSeed(),
        params: { target: 96, direction: "under" }
      });

      if (!betRes.ok || !betRes.data?.ok) {
        const reason = betRes.data?.reason || betRes.error || "unknown";
        if (reason.includes("house can cover up to 0")) { await sleep(500); continue; }
        console.log("[DiceBot] Bet fail:", reason);
        await sleep(3000);
        continue;
      }

      const betId = betRes.data.bet.id;
      const memo = betRes.data.bet.memo;
      console.log("[DiceBot] Bet:", betId.slice(0,8), "memo:", memo);

      // 2. Ambil house email dari halstavern
      const pageRes = await halFetch("GET", `/bets/${betId}/__data.json?x-sveltekit-invalidated=01`);
      const houseEmail = pageRes?.data?.nodes?.[1]?.data?.[12] || "halstavern56@gmail.com";
      console.log("[DiceBot] House:", houseEmail);

      // 3. Kirim RPOW langsung via rpow2 content script (no popup!)
      const sendRes = await rpowFetch("POST", "/send", {
        recipient_email: houseEmail,
        amount_base_units: String(BET),
        idempotency_key: memo
      });
      console.log("[DiceBot] Send:", sendRes.ok ? "OK" : "FAIL", sendRes.data);

      // 4. Track bet di background (fire-and-forget, tidak block loop utama)
      trackBet(betId);

    } catch(e) {
      console.error("[DiceBot] Error:", e.message);
      await sleep(3000);
    }
    await sleep(300);
  }
  console.log(`[DiceBot] Stopped. W=${wins} L=${losses}`);
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg.type === "START") { if (!running) runBot(); reply({ running: true, wins, losses }); }
  if (msg.type === "STOP")  { running = false; reply({ running: false, wins, losses }); }
  if (msg.type === "STATUS") { reply({ running, wins, losses }); }
  return true;
});
