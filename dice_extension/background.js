// Background service worker - navigate to fundingUrl + auto-click Send
const BET = 100000000; // 0.1 RPOW
const HAL = "https://halstavern.net";
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

// Fetch via halstavern tab content script (for placing bets)
async function halFetchTab(method, path, body) {
  return new Promise(resolve => {
    chrome.tabs.query({ url: "https://halstavern.net/*" }, tabs => {
      if (!tabs.length) { resolve({ ok: false, error: "no halstavern tab open!" }); return; }
      chrome.tabs.sendMessage(tabs[0].id, { type: "HAL_FETCH", method, path, body }, r => {
        if (chrome.runtime.lastError) { resolve({ ok: false, error: chrome.runtime.lastError.message }); return; }
        resolve(r || { ok: false });
      });
    });
  });
}

// Get fundingUrl from halstavern bet page
async function getFundingUrl(betId) {
  return new Promise(resolve => {
    chrome.tabs.query({ url: "https://halstavern.net/*" }, tabs => {
      if (!tabs.length) { resolve(null); return; }
      chrome.tabs.sendMessage(tabs[0].id, {
        type: "HAL_FETCH", method: "GET",
        path: "/bets/"+betId+"/__data.json?x-sveltekit-invalidated=01"
      }, r => {
        if (chrome.runtime.lastError) { resolve(null); return; }
        const fu = r && r.data && r.data.nodes && r.data.nodes[1] &&
                   r.data.nodes[1].data && r.data.nodes[1].data[11];
        resolve(fu || null);
      });
    });
  });
}

// Navigate rpow2 tab to fundingUrl then auto-click Send button
async function payViaFundingUrl(fundingUrl) {
  return new Promise(resolve => {
    chrome.tabs.query({ url: "https://rpow2.com/*" }, tabs => {
      if (!tabs.length) { resolve(false); return; }
      const tabId = tabs[0].id;

      // Navigate to fundingUrl (memo already in URL hash)
      chrome.tabs.update(tabId, { url: fundingUrl }, () => {
        // Poll for send button (handles auto-redirect to /#/ledger)
        let attempts = 0;
        const maxAttempts = 15;
        const interval = setInterval(() => {
          attempts++;
          chrome.scripting.executeScript({
            target: { tabId },
            func: (expectedHash) => {
              // Make sure we're on the right page (send page)
              if (!location.hash.includes("/send")) {
                // Still on wrong page, navigate again
                location.href = expectedHash;
                return { status: "navigating" };
              }
              // Find and click [ SEND ] button
              const btns = Array.from(document.querySelectorAll("button,a"));
              const sendBtn = btns.find(b =>
                b.textContent.includes("SEND") ||
                b.textContent.includes("send") ||
                b.className.includes("active")
              );
              if (sendBtn) {
                sendBtn.click();
                return { status: "clicked", text: sendBtn.textContent.trim() };
              }
              return { status: "waiting", btns: btns.map(b=>b.textContent.trim()).slice(0,5) };
            },
            args: [fundingUrl]
          }, result => {
            if (chrome.runtime.lastError) return; // tab still loading
            const r = result && result[0] && result[0].result;
            console.log("[DiceBot] click attempt", attempts, r);
            if (r && r.status === "clicked") {
              clearInterval(interval);
              resolve(true);
            } else if (attempts >= maxAttempts) {
              clearInterval(interval);
              resolve(false);
            }
          });
        }, 1000); // check every 1s
      });
    });
  });
}

async function trackBet(betId) {
  for (let i = 0; i < 60; i++) {
    await sleep(3000);
    try {
      const poll = await halFetchTab("GET", "/api/bets/"+betId);
      const b = poll && poll.data && poll.data.bet;
      if (!b) { console.log("[DiceBot] poll "+i+": empty"); continue; }
      console.log("[DiceBot] poll "+i+": "+b.status);
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
    } catch(e) { console.log("[DiceBot] poll err:", e.message); }
  }
  console.log("[DiceBot] bet "+betId.slice(0,8)+" timeout");
}

async function runBot() {
  running = true;
  console.log("[DiceBot] Starting - under 96, bet=0.1 RPOW");

  while (running) {
    try {
      // 1. Place bet
      const betRes = await halFetchTab("POST", "/api/bets", {
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
      console.log("[DiceBot] Bet:", betId.slice(0,8));

      // 2. Get fundingUrl (has memo in URL hash)
      const fundingUrl = await getFundingUrl(betId);
      if (!fundingUrl) {
        console.log("[DiceBot] No fundingUrl, skip");
        continue;
      }
      console.log("[DiceBot] fundingUrl:", fundingUrl.slice(0,80));

      // 3. Navigate rpow2 tab to fundingUrl + auto-click Send
      const paid = await payViaFundingUrl(fundingUrl);
      console.log("[DiceBot] Payment clicked:", paid);

      // 4. Track bet result (fire-and-forget)
      trackBet(betId);

    } catch(e) {
      console.error("[DiceBot] Error:", e.message);
      await sleep(3000);
    }
    await sleep(500);
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
