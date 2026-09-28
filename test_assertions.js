#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "extension/assertions.js"), "utf8");

class ChromeEvent {
  listeners = new Set();
  addListener(fn) { this.listeners.add(fn); }
  removeListener(fn) { this.listeners.delete(fn); }
  emit(...args) { for (const fn of [...this.listeners]) fn(...args); }
}

class Clock {
  now = 0;
  nextId = 1;
  timers = new Map();
  setTimeout = (fn, delay) => {
    const id = this.nextId++;
    this.timers.set(id, { fn, at: this.now + delay });
    return id;
  };
  clearTimeout = (id) => { this.timers.delete(id); };
  async flush() {
    for (let i = 0; i < 12; i++) await Promise.resolve();
  }
  async tick(milliseconds) {
    const end = this.now + milliseconds;
    await this.flush();
    while (true) {
      const next = [...this.timers.entries()]
        .filter(([, timer]) => timer.at <= end)
        .sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
      if (!next) break;
      const [id, timer] = next;
      this.now = timer.at;
      this.timers.delete(id);
      timer.fn();
      await this.flush();
    }
    this.now = end;
    await this.flush();
  }
}

function element(options = {}) {
  const attributes = { ...options.attributes };
  return {
    tagName: "DIV",
    isConnected: true,
    disabled: false,
    inert: false,
    innerText: "Ready for launch",
    textContent: "Fallback text",
    parentElement: null,
    style: {
      display: "block", visibility: "visible", opacity: "1",
      contentVisibility: "visible", ...options.style,
    },
    rects: [{ width: 100, height: 20 }],
    getClientRects() { return this.rects; },
    getAttribute(name) { return attributes[name] ?? null; },
    hasAttribute(name) { return Object.hasOwn(attributes, name); },
    matches(selector) {
      assert.equal(selector, ":disabled");
      return options.nativeDisabled || this.disabled;
    },
    ...Object.fromEntries(Object.entries(options).filter(([key]) => key !== "style")),
  };
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function harness(options = {}) {
  const clock = new Clock();
  const onUpdated = new ChromeEvent();
  const onRemoved = new ChromeEvent();
  const h = {
    clock, onUpdated, onRemoved,
    element: options.element === undefined ? element() : options.element,
    injections: [],
    gets: [],
    url: "https://example.test/start",
    inject: options.inject,
    get: options.get,
  };
  const page = vm.createContext({
    document: {
      querySelector(selector) {
        if (selector === "[") throw new SyntaxError("invalid CSS selector");
        return h.element;
      },
    },
    getComputedStyle: (node) => node.style,
  });
  const context = vm.createContext({
    chrome: { tabs: {
      onUpdated, onRemoved,
      get(tabId) {
        h.gets.push(tabId);
        return h.get ? h.get(tabId) : Promise.resolve({ id: tabId, url: h.url });
      },
    } },
    Date: { now: () => clock.now },
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
    async inPage(tabId, fn, args) {
      h.injections.push({ tabId, fn, args });
      assert.ok(args.every((value) => typeof value === "string"),
        "injection arguments must be serializable without undefined values");
      if (h.inject) return h.inject(tabId, fn, args);
      // Serialization deliberately discards the worker closure, as executeScript
      // does. The assertion must depend only on its arguments and page globals.
      const injected = vm.runInContext(`(${fn.toString()})`, page);
      return injected(...args);
    },
  });
  vm.runInContext(source, context, { filename: "assertions.js" });
  h.assertPage = (args) => context.assertPage(args);
  h.clean = () => {
    assert.equal(clock.timers.size, 0, "all assertion timers must be removed");
    assert.equal(onUpdated.listeners.size, 0, "onUpdated must be removed");
    assert.equal(onRemoved.listeners.size, 0, "onRemoved must be removed");
  };
  return h;
}

const args = (condition, extra = {}) => ({
  tabId: 7, condition,
  ...(condition === "url-matches" ? { pattern: "/start$" } : { selector: "#target" }),
  ...(condition === "text-contains" ? { text: "Ready" } : {}),
  ...extra,
});

for (const condition of ["visible", "enabled", "text-contains", "url-matches"]) {
  test(`${condition}: immediate pass and explicit false result`, async () => {
    const h = harness();
    const passed = await h.assertPage(args(condition));
    assert.equal(passed.passed, true);
    assert.equal(passed.condition, condition);
    assert.equal(passed.tabId, 7);
    assert.equal(passed.timedOut, false);
    assert.equal(passed.waitedMs, 0);
    assert.equal(passed.expected,
      condition === "url-matches" ? "/start$" : condition === "text-contains" ? "Ready" : true);
    h.clean();

    h.element.style.display = "none";
    h.element.disabled = true;
    h.element.innerText = "Loading";
    h.url = "https://example.test/other";
    const failed = await h.assertPage(args(condition));
    assert.equal(failed.passed, false);
    assert.equal(failed.timedOut, false);
    assert.equal(failed.waitedMs, 0);
    assert.equal(typeof failed.reason, "string");
    assert.ok(failed.reason.length > 0);
    assert.equal(failed.actual,
      condition === "url-matches" ? h.url : condition === "text-contains" ? "Loading" : false);
    assert.equal(h.injections.length, condition === "url-matches" ? 0 : 2);
    h.clean();
  });
}

test("missing elements are unmet conditions, including empty expected text", async () => {
  const h = harness({ element: null });
  for (const condition of ["visible", "enabled", "text-contains"]) {
    const result = await h.assertPage(args(condition, { text: "" }));
    assert.equal(result.passed, false);
    assert.equal(result.found, false);
    assert.equal(result.actual, condition === "text-contains" ? null : false);
    assert.match(result.reason, /no element/);
    h.clean();
  }
});

test("visibility checks computed styles, ancestor effects, connection, and geometry", async () => {
  const h = harness();
  const cases = [
    { style: { display: "none" } },
    { style: { visibility: "hidden" } },
    { style: { visibility: "collapse" } },
    { style: { opacity: "0" } },
    { style: { contentVisibility: "hidden" } },
    { isConnected: false },
    { rects: [] },
    { rects: [{ width: 0, height: 20 }] },
    { rects: [{ width: 20, height: 0 }] },
    { parentElement: element({ style: { display: "none" } }) },
    { parentElement: element({ style: { opacity: "0" } }) },
    { parentElement: element({ style: { contentVisibility: "hidden" } }) },
  ];
  for (const options of cases) {
    h.element = element(options);
    const result = await h.assertPage(args("visible"));
    assert.equal(result.passed, false, JSON.stringify(options));
    assert.equal(result.found, true);
    h.clean();
  }
  h.element = element({
    parentElement: element({ style: { visibility: "hidden" } }),
    rects: [{ width: 0, height: 0 }, { width: 1, height: 1, top: -500 }],
  });
  assert.equal((await h.assertPage(args("visible"))).passed, true,
    "an override of inherited visibility and an offscreen layout box are visible");
  h.clean();
});

test("enabled respects native disabled fieldsets, aria-disabled and inert ancestors", async () => {
  const h = harness();
  for (const options of [
    { disabled: true },
    { nativeDisabled: true, parentElement: element({ tagName: "FIELDSET", disabled: true }) },
    { nativeDisabled: true, parentElement: element({ tagName: "OPTGROUP", disabled: true }) },
    { attributes: { "aria-disabled": " true " } },
    { parentElement: element({ attributes: { "aria-disabled": "true" } }) },
    { inert: true },
    { parentElement: element({ attributes: { inert: "" } }) },
  ]) {
    h.element = element(options);
    const result = await h.assertPage(args("enabled"));
    assert.equal(result.passed, false, JSON.stringify(options));
    assert.equal(result.actual, false);
    h.clean();
  }
  h.element = element({
    attributes: { "aria-disabled": "false" },
    parentElement: element({
      tagName: "LEGEND",
      parentElement: element({ tagName: "FIELDSET", disabled: true }),
    }),
  });
  assert.equal((await h.assertPage(args("enabled"))).passed, true,
    "the native :disabled selector preserves the first-legend exception");
  h.clean();
});

test("text uses current input/textarea/select values and exact, case-sensitive substrings", async () => {
  const h = harness();
  for (const tagName of ["INPUT", "TEXTAREA", "SELECT"]) {
    h.element = element({ tagName, value: "Current value", innerText: "Old text" });
    const result = await h.assertPage(args("text-contains", { text: "Current" }));
    assert.equal(result.passed, true);
    assert.equal(result.actual, "Current value");
    h.element.value = "";
    assert.equal((await h.assertPage(args("text-contains", { text: "Old" }))).passed, false);
    assert.equal((await h.assertPage(args("text-contains", { text: "" }))).passed, true);
  }
  h.element = element({ innerText: undefined, textContent: "Fallback text" });
  assert.equal((await h.assertPage(args("text-contains", { text: "Fallback" }))).passed, true);
  assert.equal((await h.assertPage(args("text-contains", { text: "fallback" }))).passed, false);
  h.element = element({ innerText: "", textContent: "Hidden text" });
  assert.equal((await h.assertPage(args("text-contains", { text: "Hidden" }))).passed, false);
  h.clean();
});

for (const condition of ["visible", "enabled", "text-contains"]) {
  test(`${condition}: polling observes changes without DOM mutation and cleans up on pass`, async () => {
    const h = harness();
    h.element.style.opacity = "0";
    h.element.disabled = true;
    h.element.innerText = "Loading";
    const pending = h.assertPage(args(condition, { timeout: 250 }));
    await h.clock.tick(49);
    assert.equal(h.injections.length, 1);
    h.element.style.opacity = "1";
    h.element.disabled = false;
    h.element.innerText = "Ready";
    await h.clock.tick(1);
    const result = await pending;
    assert.equal(result.passed, true);
    assert.equal(result.waitedMs, 50);
    assert.equal(result.timedOut, false);
    assert.equal(h.injections.length, 2);
    h.clean();
    await h.clock.tick(1000);
    assert.equal(h.injections.length, 2, "no checks after resolution");
  });
}

for (const condition of ["visible", "enabled", "text-contains", "url-matches"]) {
  test(`${condition}: bounded timeout returns explicit false with last observation`, async () => {
    const h = harness({ element: null });
    h.url = "https://example.test/loading";
    const pending = h.assertPage(args(condition, { timeout: 125 }));
    await h.clock.tick(125);
    const result = await pending;
    assert.equal(result.passed, false);
    assert.equal(result.timedOut, true);
    assert.equal(result.waitedMs, 125);
    assert.match(result.reason, /timeout after 125ms/);
    assert.equal(result.actual, condition === "url-matches"
      ? h.url : condition === "text-contains" ? null : false);
    h.clean();
    const checks = h.injections.length;
    await h.clock.tick(1000);
    assert.equal(h.injections.length, checks);
  });
}

test("URL waits observe navigation events, ignore other tabs and clean listeners", async () => {
  const h = harness();
  const pending = h.assertPage(args("url-matches", { pattern: "/done$", timeout: 200 }));
  await h.clock.tick(20);
  h.onUpdated.emit(99, { url: "https://example.test/done" });
  h.onRemoved.emit(99);
  assert.equal(h.onUpdated.listeners.size, 1);
  h.onUpdated.emit(7, { status: "loading" }, { url: "https://example.test/next" });
  await h.clock.tick(10);
  h.onUpdated.emit(7, { url: "https://example.test/done" }, { url: h.url });
  const result = await pending;
  assert.equal(result.passed, true);
  assert.equal(result.actual, "https://example.test/done");
  assert.equal(result.waitedMs, 30);
  assert.equal(h.injections.length, 0);
  h.clean();
});

test("URL reads subscribe first and cannot overwrite a newer event with a stale URL", async () => {
  const h = harness();
  const read = deferred();
  h.get = () => {
    assert.equal(h.onUpdated.listeners.size, 1);
    assert.equal(h.onRemoved.listeners.size, 1);
    h.onUpdated.emit(7, { url: "https://example.test/new" });
    return read.promise;
  };
  const pending = h.assertPage(args("url-matches", { timeout: 100 }));
  read.resolve({ url: h.url });
  await h.clock.tick(100);
  const result = await pending;
  assert.equal(result.passed, false, "stale initial URL must not produce a pass");
  assert.equal(result.actual, "https://example.test/new");
  h.clean();
});

test("URL subscription race can pass before initial read and ignore a late rejection", async () => {
  const h = harness();
  const read = deferred();
  h.get = () => {
    h.onUpdated.emit(7, { url: "https://example.test/done" });
    return read.promise;
  };
  const result = await h.assertPage(args("url-matches", { pattern: "/done$", timeout: 100 }));
  assert.equal(result.passed, true);
  read.reject(new Error("late stale read"));
  await h.clock.flush();
  h.clean();
});

test("URL immediate check preserves newest event and accepts an empty regex source", async () => {
  const h = harness();
  h.get = () => {
    h.onUpdated.emit(7, { url: "https://example.test/new" });
    return Promise.resolve({ url: h.url });
  };
  const result = await h.assertPage(args("url-matches"));
  assert.equal(result.passed, false);
  assert.equal(result.actual, "https://example.test/new");
  assert.equal(result.timedOut, false);
  h.clean();
  h.get = null;
  assert.equal((await h.assertPage(args("url-matches", { pattern: "" }))).passed, true);
  assert.equal((await h.assertPage(args("url-matches", { pattern: "START" }))).passed, false);
  h.clean();
});

test("already matching current URL completes a wait without waiting for a load event", async () => {
  const h = harness();
  const result = await h.assertPage(args("url-matches", { timeout: 30000 }));
  assert.equal(result.passed, true);
  assert.equal(result.waitedMs, 0);
  assert.equal(result.timedOut, false);
  assert.equal(h.gets.length, 1);
  h.clean();
});

test("invalid inputs and regex fail before injecting, reading tabs, or creating resources", async () => {
  const h = harness();
  const cases = [
    undefined, null, [], "visible", {},
    args("missing"),
    args("visible", { tabId: -1 }),
    args("visible", { tabId: 1.5 }),
    args("visible", { tabId: "7" }),
    args("visible", { tabId: Number.MAX_SAFE_INTEGER + 1 }),
    args("visible", { selector: "" }),
    args("enabled", { selector: " " }),
    args("text-contains", { selector: 1 }),
    args("text-contains", { text: undefined }),
    args("text-contains", { text: null }),
    args("url-matches", { pattern: undefined }),
    args("url-matches", { pattern: 1 }),
    args("url-matches", { pattern: "[" }),
    args("url-matches", { flags: "i" }),
    ...[-1, 30001, Infinity, NaN, "100", null].map((timeout) => args("visible", { timeout })),
  ];
  for (const invalid of cases) {
    await assert.rejects(h.assertPage(invalid));
    h.clean();
  }
  assert.equal(h.injections.length, 0);
  assert.equal(h.gets.length, 0);
  assert.equal((await h.assertPage(args("visible", { tabId: 0, timeout: 30000 }))).passed, true);
  h.clean();
});

test("invalid CSS, blocked injection, absent results, and closed tabs throw rather than return false", async () => {
  const h = harness();
  await assert.rejects(h.assertPage(args("visible", { selector: "[", timeout: 100 })), /invalid CSS/);
  h.clean();
  h.inject = () => { throw new Error("Cannot access contents of this URL"); };
  await assert.rejects(h.assertPage(args("enabled", { timeout: 100 })), /Cannot access/);
  h.clean();
  h.inject = () => undefined;
  await assert.rejects(h.assertPage(args("visible", { timeout: 100 })), /no valid result/);
  h.clean();
  h.get = () => Promise.reject(new Error("No tab with id: 7"));
  await assert.rejects(h.assertPage(args("url-matches", { timeout: 100 })), /No tab/);
  h.clean();
  h.get = () => Promise.resolve({ id: 7 });
  await assert.rejects(h.assertPage(args("url-matches")), /URL is unavailable/);
  h.clean();
});

for (const condition of ["visible", "url-matches"]) {
  test(`${condition}: closing target tab rejects and cleans pending resources`, async () => {
    const h = harness({ element: null });
    h.url = "https://example.test/loading";
    const pending = h.assertPage(args(condition, { timeout: 1000 }));
    const rejection = assert.rejects(pending, /tab 7 was closed/);
    await h.clock.tick(20);
    h.onRemoved.emit(7, { isWindowClosing: false });
    await rejection;
    h.clean();
    await h.clock.tick(2000);
    h.clean();
  });
}

test("polling errors clean the deadline and all listeners", async () => {
  const h = harness({ element: null });
  const pending = h.assertPage(args("visible", { timeout: 200 }));
  const rejection = assert.rejects(pending, /page disappeared/);
  await h.clock.tick(20);
  h.inject = () => Promise.reject(new Error("page disappeared"));
  await h.clock.tick(30);
  await rejection;
  h.clean();
});

test("synchronous tab API failures clean listeners", async () => {
  const h = harness({ get: () => { throw new Error("tab API failed"); } });
  await assert.rejects(h.assertPage(args("url-matches", { timeout: 100 })), /tab API failed/);
  h.clean();
  h.onUpdated.addListener = () => { throw new Error("subscription failed"); };
  await assert.rejects(h.assertPage(args("url-matches", { timeout: 100 })), /subscription failed/);
  h.clean();
});

test("deadline bounds a pending operation and consumes late rejection without more polling", async () => {
  for (const condition of ["visible", "url-matches"]) {
    const operation = deferred();
    const h = harness({ inject: () => operation.promise, get: () => operation.promise });
    const pending = h.assertPage(args(condition, { timeout: 100 }));
    await h.clock.tick(100);
    const result = await pending;
    assert.equal(result.passed, false);
    assert.equal(result.timedOut, true);
    assert.equal(result.actual, null);
    assert.match(result.reason, /could not be observed/);
    h.clean();
    operation.reject(new Error("late failure"));
    await h.clock.flush();
    h.clean();
  }
});

test("concurrent assertions keep their timers and event listeners isolated", async () => {
  const h = harness({ element: null });
  const elementWait = h.assertPage(args("visible", { timeout: 100 }));
  const urlWait = h.assertPage(args("url-matches", { pattern: "/done$", timeout: 200 }));
  await h.clock.tick(30);
  h.onUpdated.emit(7, { url: "https://example.test/done" });
  assert.equal((await urlWait).passed, true);
  assert.equal(h.onRemoved.listeners.size, 1);
  await h.clock.tick(70);
  assert.equal((await elementWait).passed, false);
  h.clean();
});
