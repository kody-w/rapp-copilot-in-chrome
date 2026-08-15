const $ = (id) => document.getElementById(id);

function render(s) {
  const el = $("status");
  const fresh = s.statusAt && (Date.now() - s.statusAt < 15000);
  if (!s.token) { el.className = "status off"; el.textContent = "not configured"; return; }
  if (s.status === "connected" && fresh) {
    el.className = "status connected"; el.textContent = "connected";
  } else {
    el.className = "status waiting"; el.textContent = "waiting for a local server…";
  }
}

chrome.storage.local.get({ port: 8777, token: "", status: "", statusAt: 0 }).then((s) => {
  $("port").value = s.port;
  $("token").value = s.token;
  render(s);
});

$("save").addEventListener("click", async () => {
  await chrome.storage.local.set({
    port: parseInt($("port").value, 10) || 8777,
    token: $("token").value.trim(),
  });
  // The service worker may be asleep; poking it makes "Save & connect" mean
  // what it says instead of "connect within the next 30 seconds".
  chrome.runtime.sendMessage({ wake: true }).catch(() => {});
  setTimeout(() => chrome.storage.local.get(null).then(render), 600);
});

chrome.storage.onChanged.addListener(() => chrome.storage.local.get(null).then(render));
