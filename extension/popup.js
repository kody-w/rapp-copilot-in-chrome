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

const defaults = {
  port: 8777,
  token: "",
  profileName: "",
  instanceId: "",
  status: "",
  statusAt: 0,
};

function load() {
  return chrome.storage.local.get(defaults).then((s) => {
  $("port").value = s.port;
  $("token").value = s.token;
  $("profileName").value = s.profileName;
  $("instanceId").value = s.instanceId || "(assigned when service worker wakes)";
  render(s);
  });
}

load();

$("save").addEventListener("click", async () => {
  await chrome.storage.local.set({
    port: parseInt($("port").value, 10) || 8777,
    token: $("token").value.trim(),
    profileName: $("profileName").value.trim(),
  });
  // The service worker may be asleep; poking it makes "Save & connect" mean
  // what it says instead of "connect within the next 30 seconds".
  chrome.runtime.sendMessage({ wake: true }).catch(() => {});
  setTimeout(() => chrome.storage.local.get(null).then(render), 600);
});

chrome.storage.onChanged.addListener(() => load());
