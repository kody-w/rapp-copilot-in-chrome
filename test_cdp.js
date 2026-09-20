#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { execFileSync } = require("node:child_process");

const PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC";
const URL = `data:image/png;base64,${PNG}`;
const plain = (value) => JSON.parse(JSON.stringify(value));
const flush = () => new Promise((resolve) => setImmediate(resolve));

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function event() {
  const listeners = new Set();
  return {
    addListener: (fn) => listeners.add(fn),
    emit: (...args) => { for (const fn of listeners) fn(...args); },
  };
}

function harness() {
  let now = 0, nextTimer = 0;
  const timers = new Map();
  const attached = new Set(), calls = [], warnings = [];
  const active = new Map(), peak = new Map();
  const hooks = {};
  const onDetach = event(), onRemoved = event();
  const metrics = {
    cssContentSize: { x: 0, y: 0, width: 1200, height: 5000 },
    cssVisualViewport: { pageX: 20, pageY: 300, clientWidth: 800, clientHeight: 600 },
  };
  const chrome = {
    debugger: {
      onDetach,
      attach: async ({ tabId }, version) => {
        calls.push(["attach", tabId, version]);
        if (hooks.attach) await hooks.attach(tabId);
        if (attached.has(tabId)) throw new Error("Another debugger is already attached");
        attached.add(tabId);
      },
      detach: async ({ tabId }) => {
        calls.push(["detach", tabId]);
        if (hooks.detach) await hooks.detach(tabId);
        attached.delete(tabId);
      },
      sendCommand: async ({ tabId }, method, params) => {
        calls.push(["send", tabId, method, plain(params)]);
        assert.ok(attached.has(tabId), "commands require an attached debugger");
        active.set(tabId, (active.get(tabId) || 0) + 1);
        peak.set(tabId, Math.max(peak.get(tabId) || 0, active.get(tabId)));
        try {
          if (hooks.send) return await hooks.send(tabId, method, params);
          if (method === "Page.getLayoutMetrics") return metrics;
          if (method === "Page.captureScreenshot") return { data: PNG };
          return { ok: method };
        } finally {
          active.set(tabId, active.get(tabId) - 1);
        }
      },
    },
    tabs: {
      onRemoved,
      get: async (tabId) => { calls.push(["get", tabId]); return { windowId: 4 }; },
      update: async (tabId, data) => { calls.push(["update", tabId, plain(data)]); },
      captureVisibleTab: async (windowId, data) => {
        calls.push(["visible", windowId, plain(data)]);
        return hooks.visible ? hooks.visible() : URL;
      },
    },
  };
  const context = vm.createContext({
    chrome,
    console: { warn: (value) => warnings.push(value) },
    atob: (value) => Buffer.from(value, "base64").toString("binary"),
    setTimeout: (fn, delay) => {
      const id = ++nextTimer;
      timers.set(id, { fn, at: now + delay });
      return id;
    },
    clearTimeout: (id) => timers.delete(id),
  });
  for (const name of ["cdp.js", "capture.js"]) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, "extension", name), "utf8"), context, {
      filename: name,
    });
  }
  const cdp = vm.runInContext("cdpSessions", context);
  const limits = vm.runInContext("SCREENSHOT_LIMITS", context);
  const advance = async (ms = cdp.IDLE_GRACE_MS) => {
    now += ms;
    for (;;) {
      const due = [...timers].find(([, timer]) => timer.at <= now);
      if (!due) break;
      timers.delete(due[0]);
      due[1].fn();
      await flush();
    }
    await flush();
  };
  return {
    cdp, limits, calls, hooks, attached, peak, metrics, warnings, timers, advance,
    capture: context.captureScreenshot,
    detach(tabId) { attached.delete(tabId); onDetach.emit({ tabId }, "canceled_by_user"); },
    childDetach(tabId) { onDetach.emit({ tabId, sessionId: "child" }, "target_closed"); },
    close(tabId) { onRemoved.emit(tabId); },
    count(kind) { return calls.filter(([name]) => name === kind).length; },
  };
}

const tests = [];
function test(name, fn) { tests.push([name, fn]); }

test("concurrent and consecutive operations reuse one attachment until idle grace expires", async () => {
  const h = harness();
  const [one, two] = await Promise.all([h.cdp.acquire(7), h.cdp.acquire(7)]);
  assert.notEqual(one, two);
  assert.equal(h.count("attach"), 1);
  h.cdp.release(7, one);
  await h.advance(1000);
  assert.ok(h.attached.has(7), "remaining user retains the banner/session");
  h.cdp.release(7, two);
  await h.advance(249);
  assert.equal(h.count("detach"), 0);
  const three = await h.cdp.acquire(7);
  await h.advance();
  assert.equal(h.count("attach"), 1);
  assert.equal(h.count("detach"), 0);
  h.cdp.release(7, three);
  await h.advance();
  assert.equal(h.count("detach"), 1);
  assert.equal(h.attached.size, 0);
  const four = await h.cdp.acquire(7);
  assert.equal(h.count("attach"), 2);
  h.cdp.release(7, four);
  await h.advance();
});

test("commands serialize per tab, not across tabs, and recover after a command error", async () => {
  const h = harness(), gate = deferred();
  const [one, two] = await Promise.all([h.cdp.acquire(1), h.cdp.acquire(2)]);
  h.hooks.send = async (tabId, method) => {
    if (method === "first") { await gate.promise; throw new Error("command failed"); }
    return { tabId, method };
  };
  const first = assert.rejects(h.cdp.send(1, "first", {}, one), /command failed/);
  const second = h.cdp.send(1, "second", {}, one);
  const other = await h.cdp.send(2, "other", {}, two);
  assert.equal(other.tabId, 2);
  assert.deepEqual(h.calls.filter(([kind]) => kind === "send").map((call) => call[2]), ["first", "other"]);
  gate.resolve();
  await first;
  assert.equal((await second).method, "second");
  assert.equal(h.peak.get(1), 1);
  h.cdp.release(1, one); h.cdp.release(2, two);
  await h.advance();
  assert.equal(h.attached.size, 0);
});

test("release while a command is pending does not detach before it completes", async () => {
  const h = harness(), gate = deferred();
  const lease = await h.cdp.acquire(7);
  h.hooks.send = () => gate.promise;
  const command = h.cdp.send(7, "pending", {}, lease);
  h.cdp.release(7, lease);
  await h.advance(1000);
  assert.equal(h.count("detach"), 0);
  gate.resolve({ ok: true });
  await command;
  await h.advance(249);
  assert.equal(h.count("detach"), 0);
  await h.advance(1);
  assert.equal(h.count("detach"), 1);
});

test("concurrent attach failures propagate and a later acquisition can retry", async () => {
  const h = harness(), gate = deferred();
  h.hooks.attach = () => gate.promise;
  const first = assert.rejects(h.cdp.acquire(7), /attach refused/);
  const second = assert.rejects(h.cdp.acquire(7), /attach refused/);
  gate.reject(new Error("attach refused"));
  await Promise.all([first, second]);
  assert.equal(h.count("attach"), 1);
  assert.equal(h.count("detach"), 0);
  delete h.hooks.attach;
  const lease = await h.cdp.acquire(7);
  assert.equal(h.count("attach"), 2);
  h.cdp.release(7, lease);
  await h.advance();
  assert.equal(h.attached.size, 0);
});

test("external detach invalidates old leases without touching their replacements", async () => {
  const h = harness();
  const old = await h.cdp.acquire(7);
  h.detach(7);
  const fresh = await h.cdp.acquire(7);
  await assert.rejects(h.cdp.send(7, "stale", {}, old), /active lease/);
  assert.equal(h.cdp.release(7, old), false);
  await h.advance(1000);
  assert.ok(h.attached.has(7));
  assert.equal((await h.cdp.send(7, "fresh", {}, fresh)).ok, "fresh");
  h.cdp.release(7, fresh);
  await h.advance();
  assert.equal(h.attached.size, 0);
});

test("detach with queued work drains the old generation before reattaching", async () => {
  const h = harness(), gate = deferred();
  const old = await h.cdp.acquire(7);
  h.hooks.send = () => gate.promise;
  const first = assert.rejects(h.cdp.send(7, "inflight", {}, old), /session ended/);
  const second = assert.rejects(h.cdp.send(7, "queued", {}, old), /session ended/);
  await flush();
  h.detach(7);
  const acquiring = h.cdp.acquire(7);
  await flush();
  assert.equal(h.count("attach"), 1);
  gate.resolve({});
  await Promise.all([first, second]);
  const fresh = await acquiring;
  assert.equal(h.count("send"), 1, "stale queued command was never sent");
  assert.equal(h.count("attach"), 2);
  h.cdp.release(7, fresh);
  await h.advance();
});

test("detach during attachment cleans the late attachment before replacement", async () => {
  const h = harness(), gate = deferred();
  h.hooks.attach = () => gate.promise;
  const old = assert.rejects(h.cdp.acquire(7), /session ended/);
  await flush();
  h.detach(7);
  const acquiring = h.cdp.acquire(7);
  gate.resolve();
  await old;
  const fresh = await acquiring;
  assert.deepEqual(h.calls.filter(([kind]) => kind !== "send").map(([kind]) => kind),
    ["attach", "detach", "attach"]);
  h.cdp.release(7, fresh);
  await h.advance();
});

test("acquire racing idle detach waits for detach, even with onDetach event", async () => {
  const h = harness(), gate = deferred();
  const old = await h.cdp.acquire(7);
  h.hooks.detach = async (tabId) => { h.detach(tabId); await gate.promise; };
  h.cdp.release(7, old);
  await h.advance();
  const acquiring = h.cdp.acquire(7);
  await flush();
  assert.equal(h.count("attach"), 1);
  gate.resolve();
  const fresh = await acquiring;
  assert.equal(h.count("attach"), 2);
  assert.throws(() => h.cdp.release(7, old), /already been released/);
  delete h.hooks.detach;
  h.cdp.release(7, fresh);
  await h.advance();
});

test("tab close clears holders and child-target detach leaves the root alive", async () => {
  const h = harness();
  const lease = await h.cdp.acquire(7);
  h.childDetach(7);
  assert.equal((await h.cdp.send(7, "root", {}, lease)).ok, "root");
  h.close(7);
  await flush();
  assert.equal(h.attached.size, 0);
  await assert.rejects(h.cdp.send(7, "closed", {}, lease), /active lease/);
  assert.equal(h.cdp.release(7, lease), false);
  assert.equal(h.timers.size, 0);
});

test("tab close during attach retires its late attachment", async () => {
  const h = harness(), gate = deferred();
  h.hooks.attach = () => gate.promise;
  const acquiring = assert.rejects(h.cdp.acquire(7), /tab closed/);
  await flush();
  h.close(7);
  gate.resolve();
  await acquiring;
  await flush();
  assert.equal(h.attached.size, 0);
  assert.equal(h.count("detach"), 1);
});

test("transient cleanup errors retry; persistent errors fail closed and are reported", async () => {
  const h = harness();
  let failures = 2;
  h.hooks.detach = async () => { if (failures-- > 0) throw new Error("temporary failure"); };
  const lease = await h.cdp.acquire(7);
  h.cdp.release(7, lease);
  await h.advance();
  assert.equal(h.count("detach"), 3);
  assert.equal(h.attached.size, 0);
  h.hooks.detach = async () => { throw new Error("persistent failure"); };
  const second = await h.cdp.acquire(7);
  h.cdp.release(7, second);
  await h.advance();
  assert.match(h.warnings[0], /cleanup failed/);
  await assert.rejects(h.cdp.acquire(7), /cleanup failed/);
  h.detach(7);
  delete h.hooks.detach;
  const third = await h.cdp.acquire(7);
  h.cdp.release(7, third);
  await h.advance();
  assert.equal(h.attached.size, 0);
});

test("explicit leases are tab-scoped and invalid or duplicate releases are errors", async () => {
  const h = harness(), lease = await h.cdp.acquire(7);
  for (const token of [null, {}, 1]) {
    await assert.rejects(h.cdp.send(7, "bad", {}, token), /active lease/);
    assert.throws(() => h.cdp.release(7, token), /invalid lease/);
  }
  await assert.rejects(h.cdp.send(8, "bad", {}, lease), /active lease/);
  assert.throws(() => h.cdp.release(8, lease), /invalid lease/);
  assert.equal(h.cdp.release(7, lease), true);
  assert.throws(() => h.cdp.release(7, lease), /already been released/);
  await assert.rejects(h.cdp.send(7, "released", {}, lease), /active lease/);
  for (const id of [-1, 0.5, NaN, Infinity, "7", Number.MAX_SAFE_INTEGER + 1]) {
    await assert.rejects(h.cdp.acquire(id), /tabId/);
  }
  await h.advance();
});

test("tokenless core API balances concurrent users and rejects unbalanced releases", async () => {
  const h = harness();
  await assert.rejects(h.cdp.send(7, "missing"), /active acquisition/);
  assert.throws(() => h.cdp.release(7), /without an active acquisition/);
  await Promise.all([h.cdp.acquire(7), h.cdp.acquire(7)]);
  assert.equal((await h.cdp.send(7, "legacy")).ok, "legacy");
  assert.equal(h.cdp.release(7), true);
  await h.advance();
  assert.equal(h.count("detach"), 0);
  assert.equal(h.cdp.release(7), true);
  assert.throws(() => h.cdp.release(7), /without an active acquisition/);
  await assert.rejects(h.cdp.send(7, "released"), /active acquisition/);
  await h.advance();
  assert.equal(h.count("detach"), 1);
});

test("mixed legacy and leased users do not invalidate each other's active work", async () => {
  const h = harness();
  const lease = await h.cdp.acquire(7);
  for (let i = 0; i < 3; i++) {
    await h.cdp.acquire(7);
    h.cdp.release(7);
    assert.equal((await h.cdp.send(7, "leased", {}, lease)).ok, "leased");
  }
  const discarded = await h.cdp.acquire(7);
  h.cdp.release(7, lease);
  assert.equal((await h.cdp.send(7, "legacy")).ok, "legacy");
  h.cdp.release(7);
  const fresh = await h.cdp.acquire(7);
  await assert.rejects(h.cdp.send(7, "discarded", {}, discarded), /active lease/);
  assert.throws(() => h.cdp.release(7, discarded), /without an active lease/);
  assert.equal(h.count("attach"), 1);
  h.cdp.release(7, fresh);
  await h.advance();
  assert.equal(h.attached.size, 0);
});

test("ambiguous tokenless calls after detach cannot send on or release a replacement", async () => {
  const h = harness();
  const discarded = await h.cdp.acquire(7);
  h.cdp.release(7);
  const old = await h.cdp.acquire(7);
  h.detach(7);
  const fresh = await h.cdp.acquire(7);
  assert.throws(() => h.cdp.release(7, discarded), /without an active lease/);
  await assert.rejects(h.cdp.send(7, "ambiguous"), /lease required after detach/);
  assert.throws(() => h.cdp.release(7), /lease required after detach/);
  await h.advance(1000);
  assert.ok(h.attached.has(7));
  assert.equal((await h.cdp.send(7, "explicit", {}, fresh)).ok, "explicit");
  assert.equal(h.cdp.release(7, old), false);
  assert.equal((await h.cdp.send(7, "unambiguous")).ok, "unambiguous");
  h.cdp.release(7, fresh);
  await h.advance();
  assert.equal(h.attached.size, 0);
});

test("multiple detached generations remain ambiguous until all stale leases settle", async () => {
  const h = harness();
  const oldest = await h.cdp.acquire(7);
  h.detach(7);
  const older = await h.cdp.acquire(7);
  h.detach(7);
  const fresh = await h.cdp.acquire(7);
  h.cdp.release(7, older);
  assert.throws(() => h.cdp.release(7), /lease required after detach/);
  h.cdp.release(7, oldest);
  assert.equal((await h.cdp.send(7, "legacy")).ok, "legacy");
  h.cdp.release(7, fresh);
  await h.advance();
  assert.equal(h.attached.size, 0);
});

test("failed attachment does not leave an implicit tombstone blocking tokenless retry", async () => {
  const h = harness();
  h.hooks.attach = () => { throw new Error("cannot attach"); };
  await assert.rejects(h.cdp.acquire(7), /cannot attach/);
  delete h.hooks.attach;
  await h.cdp.acquire(7);
  assert.equal((await h.cdp.send(7, "legacy")).ok, "legacy");
  h.cdp.release(7);
  await h.advance();
  assert.equal(h.attached.size, 0);
});

test("default screenshot preserves legacy visible-tab capture without attaching", async () => {
  const h = harness();
  assert.equal(await h.capture({ tabId: 7 }), URL);
  assert.equal(await h.capture({ tabId: 7, fullPage: false, scale: 1 }), URL);
  assert.deepEqual(h.calls.slice(0, 3), [
    ["get", 7], ["update", 7, { active: true }], ["visible", 4, { format: "png" }],
  ]);
  assert.equal(h.count("attach"), 0);
});

test("full-page, region and scaled viewport use precise CSS clips and shared CDP", async () => {
  const h = harness();
  assert.equal(await h.capture({ tabId: 7, fullPage: true }), URL);
  const region = { x: 500, y: 3000, width: 400, height: 300 };
  assert.equal(await h.capture({ tabId: 7, region, scale: 0.5 }), URL);
  assert.equal(await h.capture({ tabId: 7, scale: 2 }), URL);
  assert.equal(h.count("attach"), 1);
  const screenshots = h.calls.filter((call) => call[2] === "Page.captureScreenshot");
  assert.deepEqual(screenshots.map((call) => call[3]), [
    { format: "png", fromSurface: true, captureBeyondViewport: true,
      clip: { x: 0, y: 0, width: 1200, height: 5000, scale: 1 } },
    { format: "png", fromSurface: true, captureBeyondViewport: true,
      clip: { ...region, scale: 0.5 } },
    { format: "png", fromSurface: true, captureBeyondViewport: true,
      clip: { x: 20, y: 300, width: 800, height: 600, scale: 2 } },
  ]);
  assert.equal(h.count("visible"), 0);
  await h.advance();
  assert.equal(h.attached.size, 0);
});

test("actual capture results render as MCP images standalone and inside batches", async () => {
  const h = harness();
  const visible = await h.capture({ tabId: 7 });
  const full = await h.capture({ tabId: 7, fullPage: true });
  const results = JSON.parse(execFileSync("python3", ["-c", [
    "import json, sys",
    "from rappter_chrome_mcp import text_result",
    "visible, full = json.load(sys.stdin)",
    "print(json.dumps([text_result(visible), text_result([{'cmd': 'screenshot', 'result': full}])]))",
  ].join("\n")], { cwd: __dirname, input: JSON.stringify([visible, full]), encoding: "utf8" }));
  for (const result of results) {
    assert.deepEqual(result.content[1], { type: "image", data: PNG, mimeType: "image/png" });
    assert.ok(!result.content[0].text.includes(PNG));
  }
  await h.advance();
  assert.equal(h.attached.size, 0);
});

test("invalid screenshot inputs and oversize regions fail before any browser side effects", async () => {
  const h = harness();
  const base = { x: 0, y: 0, width: 100, height: 100 };
  const invalid = [
    { tabId: -1 }, { tabId: "7" }, { fullPage: "true" },
    { fullPage: true, region: base }, { region: null }, { region: [] },
    ...[0, -1, 2.01, Infinity, NaN, "1"].map((scale) => ({ scale })),
    ...["x", "y", "width", "height"].flatMap((key) => [NaN, Infinity, -1, "5", 1000001]
      .map((value) => ({ region: { ...base, [key]: value } }))),
    { region: { ...base, width: 0 } },
    { region: { ...base, height: 0 } },
    { region: { ...base, x: 999950 } },
    { region: { ...base, width: 16385 } },
    { region: { ...base, width: 8000, height: 8000 } },
    { region: { ...base, width: 9000 }, scale: 2 },
  ];
  for (const args of invalid) await assert.rejects(h.capture({ tabId: 7, ...args }));
  assert.equal(h.calls.length, 0);
});

test("full-page metric overflow and missing CSS metrics fail explicitly and release", async () => {
  for (const size of [
    undefined, { x: 0, y: 0, width: Infinity, height: 1 },
    { x: 0, y: 0, width: 17000, height: 1 },
    { x: 0, y: 0, width: 8000, height: 8000 },
  ]) {
    const h = harness();
    h.metrics.cssContentSize = size;
    h.metrics.contentSize = { x: 0, y: 0, width: 1, height: 1 };
    await assert.rejects(h.capture({ tabId: 7, fullPage: true }));
    assert.equal(h.calls.filter((call) => call[2] === "Page.captureScreenshot").length, 0);
    assert.equal(h.count("visible"), 0);
    await h.advance();
    assert.equal(h.attached.size, 0);
  }
});

test("capture and layout failures never silently return a viewport and always release", async () => {
  for (const failure of ["Page.getLayoutMetrics", "Page.captureScreenshot"]) {
    for (const args of [{ fullPage: true }, { region: { x: 0, y: 0, width: 10, height: 10 } }]) {
      const h = harness();
      h.hooks.send = async (_, method) => {
        if (method === failure) throw new Error("CDP capture unsupported");
        return h.metrics;
      };
      await assert.rejects(h.capture({ tabId: 7, ...args }), /capture unsupported/);
      assert.equal(h.count("visible"), 0);
      await h.advance();
      assert.equal(h.attached.size, 0);
    }
  }
});

test("PNG output validates encoding, signature, dimensions and encoded size", async () => {
  const hugePixels = Buffer.from(PNG, "base64");
  hugePixels.writeUInt32BE(8000, 16); hugePixels.writeUInt32BE(8000, 20);
  const hugeDimension = Buffer.from(PNG, "base64");
  hugeDimension.writeUInt32BE(16385, 16);
  const zero = Buffer.from(PNG, "base64");
  zero.writeUInt32BE(0, 16);
  const h = harness();
  const invalid = [
    undefined, "", "!".repeat(80), "A".repeat(80),
    PNG.slice(0, -4), hugePixels.toString("base64"), hugeDimension.toString("base64"),
    zero.toString("base64"), "A".repeat(h.limits.MAX_BASE64_LENGTH + 4),
  ];
  for (const data of invalid) {
    h.hooks.send = async (_, method) => method === "Page.getLayoutMetrics" ? h.metrics : { data };
    await assert.rejects(h.capture({ tabId: 7, fullPage: true }));
  }
  assert.equal(h.count("visible"), 0);
  await h.advance();
  assert.equal(h.attached.size, 0);
  for (const dataURL of [null, "data:image/jpeg;base64,abcd", "data:image/png;base64,bad",
    `data:image/png;base64,${hugePixels.toString("base64")}`]) {
    h.hooks.visible = () => dataURL;
    await assert.rejects(h.capture({ tabId: 7 }));
  }
});

test("screenshot detach race fails with cleanup rather than using a replacement session", async () => {
  const h = harness(), gate = deferred();
  h.hooks.send = async (_, method) => {
    if (method === "Page.getLayoutMetrics") { await gate.promise; return h.metrics; }
    return { data: PNG };
  };
  const capture = assert.rejects(h.capture({ tabId: 7, fullPage: true }), /session ended/);
  await flush();
  h.detach(7);
  const acquiring = h.cdp.acquire(7);
  gate.resolve();
  await capture;
  const fresh = await acquiring;
  await h.advance(1000);
  assert.ok(h.attached.has(7));
  assert.equal(h.count("send"), 1);
  h.cdp.release(7, fresh);
  await h.advance();
  assert.equal(h.attached.size, 0);
});

async function main() {
  for (const [name, fn] of tests) {
    await fn();
    console.log(`PASS ${name}`);
  }
  console.log(`CDP sessions and capture: ${tests.length} behavioral tests passed`);
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
