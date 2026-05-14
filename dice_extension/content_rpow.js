// Content script rpow2.com - fetch dengan cookie rpow2
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg.type !== "RPOW_FETCH") return false;
  const url = msg.path.startsWith("http") ? msg.path : "https://api.rpow2.com" + msg.path;
  fetch(url, {
    method: msg.method,
    credentials: "include",
    headers: {
      "content-type": "application/json",
      "accept": "application/json",
      "origin": "https://rpow2.com",
      "referer": "https://rpow2.com/"
    },
    body: msg.body ? JSON.stringify(msg.body) : undefined
  }).then(r => r.text()).then(t => {
    try { reply({ ok: true, data: JSON.parse(t) }); }
    catch(e) { reply({ ok: false, data: { raw: t.slice(0,200) } }); }
  }).catch(e => reply({ ok: false, error: e.message }));
  return true;
});
console.log("[DiceBot] rpow2 content script loaded");
