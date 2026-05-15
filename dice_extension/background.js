// Background service worker - cookie-based polling (no content script dependency)
const BET = 100000000; // 0.1 RPOW
const HAL = "https://halstavern.net";
const RPOW_API = "https://api.rpow2.com";
let running = false, wins = 0, losses = 0;

// Keep service worker alive via alarm
chrome.alarms.create("keepAlive", { periodInMinutes: 0.4 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "keepAlive") {
    chrome.storage.local.get(["running","wins","losses"], d => {
      if (d.running && !running) {
        running = true; wins = d.wins||0; losses = d.losses||0;
        console.log("[DiceBot] Resumed after SW restart");
        runBot();
      }
    });
  }
});

function rndSeed() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)))
    .map(b => b.toString(16).padStart(2,"0")).join("");
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// Get cookie string for a domain using chrome.cookies API
async function getCookies(domain) {
  return new Promise(resolve => {
    chrome.cookies.getAll({ domain }, cookies => {
      resolve(cookies.map(c => c.name+"="+c.value).join("; "));
    });
  });
}

// Fetch halstavern directly from background (no content script!)
async function halFetch(method, path, body) {
  try {
    const cookies = await getCookies("halstavern.net");
    const url = path.startsWith("http") ? path : HAL + path;
    const r = await fetch(url, {
      method,
      headers: {
        "accept": "application/json",
        "content-type": "application/json",
        "cookie": cookies
      },
      body: body ? JSON.stringify(body) : undefined
    });
    const t = await r.text();
    try { return { ok: r.ok, status: r.status, data: JSON.parse(t) }; }
    catch(e) { return { ok: false, data: { raw: t.slice(0,200) } }; }
  } catch(e) {
    return { ok: false, error: e.message };
  }
}

// Fetch rpow2 API directly from background (no content script!)
async function rpowFetch(method, path, body) {
  try {
    const cookies = await getCookies("rpow2.com");
    const r = await fetch(RPOW_API + path, {
      method,
      headers: {
        "accept": "application/json",
        "content-type": "application/json",
        "cookie": cookies,
        "origin": "https://rpow2.com",
        "referer": "https://rpow2.com/"
      },
      body: body ? JSON.stringify(body) : undefined
    });
    const t = await r.text();
    try { return { ok: r.ok, status: r.status, data: JSON.parse(t) }; }
    catch(e) { return { ok: false, data: { raw: t.slice(0,200) } }; }
  } catch(e) {
    return { ok: false, error: e.message };
  }
}

async function trackBet(betId) {
  for (let i = 0; i < 60; i++) {
    await sleep(3000);
    try {
      const poll = await halFetch("GET", "/api/bets/"+betId);
      const b = poll && poll.data && poll.data.bet;
      if (!b) continue;
      console.log("[DiceBot] poll", i, b.status);
      if (b.status !== "pending") {
        const payout = parseInt(b.payout_base_units || 0);
        if (payout > BET) {
          wins++;
          console.log("[DiceBot] WIN +"+(payout-BET)/1e9+" RPOW (W="+wins+" L="+losses+")");
        } else {
          losses++;
          console.log("[DiceBot] LOSS (W="+wins+" L="+losses+")");
        }
        chrome.storage.local.set({ wins, losses, running });
        return;
      }
    } catch(e) { /* retry */ }
  }
  console.log("[DiceBot] bet "+betId.slice(0,8)+" timeout");
}

async function runBot() {
  running = true;
  console.log("[DiceBot] Starting - under 96, bet=0.1 RPOW");

  while (running) {
    try {
      const betRes = await halFetch("POST", "/api/bets", {
        game_slug: "dice",
        stake_base_units: String(BET),
        client_seed: rndSeed(),
        params: { target: 96, direction: "under" }
      });

      if (!betRes.ok || !betRes.data || !betRes.data.ok) {
        const reason = (betRes.data && betRes.data.reason) || betRes.error || "unknown";
        if (reason.includes("house can cover up to 0")) { await sleep(500); continue; }
        console.log("[DiceBot] Bet fail:", reason);
        await sleep(3000);
        continue;
      }

      const betId = betRes.data.bet.id;
      const memo = betRes.data.bet.memo;
      console.log("[DiceBot] Bet:", betId.slice(0,8));

      const sendRes = await rpowFetch("POST", "/send", {
        recipient_email: "halstavern56@gmail.com",
        amount_base_units: String(BET),
        idempotency_key: memo
      });
      const tid = sendRes.data && sendRes.data.transfer_id;
      console.log("[DiceBot] Send:", sendRes.ok ? "OK "+tid : "FAIL "+sendRes.error);

      // Fire-and-forget track
      trackBet(betId);

    } catch(e) {
      console.error("[DiceBot] Error:", e.message);
      await sleep(3000);
    }
    await sleep(300);
  }
  console.log("[DiceBot] Stopped. W="+wins+" L="+losses);
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg.type === "START") {
    if (!running) runBot();
    chrome.storage.local.set({ running: true });
    reply({ running: true, wins, losses });
  }
  if (msg.type === "STOP") {
    running = false;
    chrome.storage.local.set({ running: false });
    reply({ running: false, wins, losses });
  }
  if (msg.type === "STATUS") { reply({ running, wins, losses }); }
  return true;
});
