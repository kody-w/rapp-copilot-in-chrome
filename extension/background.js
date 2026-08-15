// background.js — the browser half of the bridge.
//
// DESIGN: THE EXTENSION DIALS OUT.
//
// Anthropic's claude-in-chrome goes the other way: Chrome spawns a native
// messaging host, which means a manifest in a browser-specific directory, a
// vendor binary to host it, and an account login to authorise it. Measured on
// this machine, that chain was broken at two links and reported
// "Browser extension is not connected."
//
// Dialling out to a localhost WebSocket removes all three. There is no manifest
// to register, nothing to restart Chrome for, and no account anywhere. The
// extension polls for a server; when a script starts one, work happens.
//
// SECURITY. This drives your real, logged-in browser, so the socket is not
// open house:
//   * the server binds 127.0.0.1 only — nothing off-box can reach it;
//   * a shared token generated on the server, pasted in once here, must match;
//   * the server rejects any Origin that is not chrome-extension://, so a web
//     page that guesses the port cannot drive your browser.
// Any one of those alone would be thin. Together they mean an attacker needs
// local code execution AND the token file, at which point they did not need
// this extension.

const DEFAULTS = { port: 8777, token: "" };
const RETRY_MS = 2000;

let ws = null;
let connecting = false;

// ── keepalive ───────────────────────────────────────────────────────────────
// An MV3 service worker is evicted when idle, which would silently end the
// session: the socket dies, the server sees a disconnect, and a cron job that
// expected a browser gets nothing. The alarm wakes us at least every 30s, and
// the reconnect loop re-establishes the socket if it was torn down.
chrome.alarms.create("rappter-keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => ensureConnected());
chrome.runtime.onStartup.addListener(() => ensureConnected());
chrome.runtime.onInstalled.addListener(() => ensureConnected());

// The popup's "Save & connect" pokes us here. Without this listener the button
// means "connect within the next 30 seconds, when the alarm fires", which reads
// as broken the first time you set it up.
chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg && msg.wake) { ensureConnected(); reply({ ok: true }); }
  return true;
});

async function cfg() {
  const s = await chrome.storage.local.get(DEFAULTS);
  return { port: s.port || DEFAULTS.port, token: s.token || "" };
}

async function ensureConnected() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  if (connecting) return;
  const { port, token } = await cfg();
  if (!token) return;                      // unconfigured is OFF, not open
  connecting = true;
  try {
    const sock = new WebSocket(`ws://127.0.0.1:${port}/?token=${encodeURIComponent(token)}`);
    sock.onopen = () => {
      ws = sock;
      connecting = false;
      setStatus("connected");
      sock.send(JSON.stringify({ hello: "rappter-chrome", version: 1 }));
    };
    sock.onclose = () => { ws = null; connecting = false; setStatus("waiting"); };
    sock.onerror = () => { connecting = false; };
    sock.onmessage = (ev) => handleMessage(sock, ev.data);
  } catch (e) {
    connecting = false;
  }
}

function setStatus(s) { chrome.storage.local.set({ status: s, statusAt: Date.now() }); }
setInterval(ensureConnected, RETRY_MS);
ensureConnected();

async function handleMessage(sock, raw) {
  let msg;
  try { msg = JSON.parse(raw); } catch { return; }
  const { id, cmd, args } = msg;
  try {
    const result = await dispatch(cmd, args || {});
    sock.send(JSON.stringify({ id, ok: true, result }));
  } catch (e) {
    sock.send(JSON.stringify({ id, ok: false, error: String(e && e.message || e) }));
  }
}

// ── page-side functions ─────────────────────────────────────────────────────
// These are DECLARED functions handed to chrome.scripting, never strings run
// through eval. MV3 forbids unsafe-eval in the extension, and a page's own CSP
// commonly forbids it in the MAIN world, so a string-eval design fails exactly
// on the hardened sites worth automating. Declared functions sidestep both and
// do not attach the debugger, so no "being debugged" banner appears for
// ordinary work.

function _pageText() {
  const drop = ["script", "style", "noscript", "svg"];
  const art = document.querySelector("article, main, [role=main]") || document.body;
  const clone = art.cloneNode(true);
  drop.forEach((sel) => clone.querySelectorAll(sel).forEach((n) => n.remove()));
  return (clone.innerText || "").replace(/\n{3,}/g, "\n\n").trim();
}

function _click(selector, index) {
  const els = document.querySelectorAll(selector);
  const el = els[index || 0];
  if (!el) throw new Error(`no element for ${selector} [${index || 0}] (${els.length} matched)`);
  el.scrollIntoView({ block: "center" });
  el.click();
  return { clicked: selector, matched: els.length };
}

function _type(selector, text, submit) {
  const el = document.querySelector(selector);
  if (!el) throw new Error(`no element for ${selector}`);
  el.focus();
  const proto = el instanceof HTMLTextAreaElement
    ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
  // React and friends listen for the native setter, not for .value =. Without
  // this, text appears in the box and the app never sees it — the field looks
  // filled and submits empty.
  if (setter && (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement)) {
    setter.call(el, text);
  } else if (el.isContentEditable) {
    el.textContent = text;
  } else {
    el.value = text;
  }
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  if (submit) {
    el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, bubbles: true }));
    el.dispatchEvent(new KeyboardEvent("keyup", { key: "Enter", code: "Enter", keyCode: 13, bubbles: true }));
  }
  return { typed: text.length, selector };
}

function _query(selector, limit) {
  return [...document.querySelectorAll(selector)].slice(0, limit || 40).map((el, i) => ({
    i,
    tag: el.tagName.toLowerCase(),
    text: (el.innerText || el.value || "").trim().slice(0, 200),
    href: el.getAttribute("href") || undefined,
    aria: el.getAttribute("aria-label") || undefined,
  }));
}

function _waitFor(selector, timeoutMs) {
  return new Promise((resolve, reject) => {
    if (document.querySelector(selector)) return resolve({ found: true, waitedMs: 0 });
    const t0 = Date.now();
    const obs = new MutationObserver(() => {
      if (document.querySelector(selector)) {
        obs.disconnect();
        resolve({ found: true, waitedMs: Date.now() - t0 });
      }
    });
    obs.observe(document.documentElement, { childList: true, subtree: true });
    setTimeout(() => {
      obs.disconnect();
      reject(new Error(`timeout after ${timeoutMs}ms waiting for ${selector}`));
    }, timeoutMs || 15000);
  });
}

async function inPage(tabId, func, args) {
  const [res] = await chrome.scripting.executeScript({
    target: { tabId }, world: "MAIN", func, args: args || [],
  });
  return res?.result;
}

// ── arbitrary JS via the debugger ───────────────────────────────────────────
// The one operation declared functions cannot cover. Runtime.evaluate runs
// outside the page's CSP, so it works on sites that forbid eval — which is most
// of the ones with a login. It attaches the debugger for the duration and
// detaches after, so the banner is scoped to the call that needed it.
async function evalJs(tabId, code, awaitPromise) {
  const target = { tabId };
  await chrome.debugger.attach(target, "1.3");
  try {
    const r = await chrome.debugger.sendCommand(target, "Runtime.evaluate", {
      expression: code,
      returnByValue: true,
      awaitPromise: awaitPromise !== false,
      userGesture: true,
    });
    if (r.exceptionDetails) {
      throw new Error(r.exceptionDetails.exception?.description || "JS exception");
    }
    return r.result?.value;
  } finally {
    try { await chrome.debugger.detach(target); } catch { /* already gone */ }
  }
}

function waitForLoad(tabId, timeoutMs) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const cleanup = () => chrome.tabs.onUpdated.removeListener(fn);
    const done = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      cleanup();
      resolve(true);
    };
    const fail = (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      cleanup();
      reject(error);
    };
    const fn = (id, info) => {
      if (id === tabId && info.status === "complete") done();
    };
    chrome.tabs.onUpdated.addListener(fn);
    chrome.tabs.get(tabId)
      .then((tab) => { if (tab.status === "complete") done(); })
      .catch(fail);
    const timer = setTimeout(
      () => fail(new Error(`tab ${tabId} did not finish loading within ${timeoutMs || 20000}ms`)),
      timeoutMs || 20000,
    );
  });
}

async function dispatch(cmd, a) {
  switch (cmd) {
    case "ping":
      return { pong: true, at: Date.now() };

    case "tabs": {
      const tabs = await chrome.tabs.query({});
      return tabs.map((t) => ({
        tabId: t.id, title: t.title, url: t.url,
        active: t.active, windowId: t.windowId, status: t.status,
      }));
    }

    case "find_tab": {
      const tabs = await chrome.tabs.query({});
      const needle = (a.match || "").toLowerCase();
      const hit = tabs.find((t) => (t.url || "").toLowerCase().includes(needle)
                                || (t.title || "").toLowerCase().includes(needle));
      return hit ? { tabId: hit.id, title: hit.title, url: hit.url } : null;
    }

    case "create": {
      const t = await chrome.tabs.create({ url: a.url || "about:blank", active: !!a.active });
      if (a.url) await waitForLoad(t.id, a.timeout);
      return { tabId: t.id, url: t.url };
    }

    case "close":
      await chrome.tabs.remove(a.tabId);
      return { closed: a.tabId };

    case "activate":
      await chrome.tabs.update(a.tabId, { active: true });
      return { active: a.tabId };

    case "navigate": {
      await chrome.tabs.update(a.tabId, { url: a.url });
      await waitForLoad(a.tabId, a.timeout);
      const t = await chrome.tabs.get(a.tabId);
      return { tabId: t.id, url: t.url, title: t.title };
    }

    case "text":
      return await inPage(a.tabId, _pageText);

    case "html":
      return await inPage(a.tabId, () => document.documentElement.outerHTML);

    case "query":
      return await inPage(a.tabId, _query, [a.selector, a.limit]);

    case "click":
      return await inPage(a.tabId, _click, [a.selector, a.index]);

    case "type":
      return await inPage(a.tabId, _type, [a.selector, a.text, !!a.submit]);

    case "waitfor":
      return await inPage(a.tabId, _waitFor, [a.selector, a.timeout]);

    case "eval":
      return await evalJs(a.tabId, a.code, a.awaitPromise);

    case "screenshot": {
      const t = await chrome.tabs.get(a.tabId);
      await chrome.tabs.update(a.tabId, { active: true });
      return await chrome.tabs.captureVisibleTab(t.windowId, { format: "png" });
    }

    case "batch": {
      // One round trip for a whole sequence, in order, stopping at the first
      // error — the same shape as browser_batch, because a five-step login
      // flow should not cost five round trips.
      const out = [];
      for (const step of a.actions || []) {
        out.push({ cmd: step.cmd, result: await dispatch(step.cmd, step.args || {}) });
      }
      return out;
    }

    default:
      throw new Error(`unknown command: ${cmd}`);
  }
}
