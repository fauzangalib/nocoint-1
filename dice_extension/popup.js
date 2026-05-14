document.getElementById("s").onclick = () =>
  chrome.runtime.sendMessage({ type: "START" }, r => {
    document.getElementById("status").textContent = "running...";
  });

document.getElementById("x").onclick = () =>
  chrome.runtime.sendMessage({ type: "STOP" }, r => {
    document.getElementById("status").textContent = "stopped";
  });

setInterval(() => {
  chrome.runtime.sendMessage({ type: "STATUS" }, r => {
    if (!r) return;
    document.getElementById("status").textContent = r.running ? "running" : "stopped";
    document.getElementById("w").textContent = r.wins || 0;
    document.getElementById("l").textContent = r.losses || 0;
  });
  chrome.storage.local.get(["wins","losses"], d => {
    document.getElementById("w").textContent = d.wins || 0;
    document.getElementById("l").textContent = d.losses || 0;
  });
}, 1000);
