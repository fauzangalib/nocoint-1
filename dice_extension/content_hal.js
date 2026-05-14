// Content script halstavern.net - fetch dengan cookie halstavern
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg.type !== "HAL_FETCH") return false;
  const url = msg.path.startsWith("http") ? msg.path : "https://halstavern.net" + msg.path;
  fetch(url, {
    method: msg.method,
    credentials: "include",
    headers: { "content-type": "application/json", "accept": "application/json" },
    body: msg.body ? JSON.stringify(msg.body) : undefined
  }).then(r => r.text()).then(t => {
    try { reply({ ok: true, data: JSON.parse(t) }); }
    catch(e) { reply({ ok: false, data: { raw: t.slice(0,200) } }); }
  }).catch(e => reply({ ok: false, error: e.message }));
  return true;
});
console.log("[DiceBot] hal content script loaded");
