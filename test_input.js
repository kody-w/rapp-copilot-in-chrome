#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const { webcrypto: crypto } = require("node:crypto");

const plain = (value) => JSON.parse(JSON.stringify(value));
const flush = () => new Promise((resolve) => setImmediate(resolve));
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
function event() {
  const listeners = new Set();
  return {
    listeners,
    addListener: (fn) => listeners.add(fn),
    removeListener: (fn) => listeners.delete(fn),
    emit: (...args) => { for (const fn of [...listeners]) fn(...args); },
  };
}
function element(document, options = {}) {
  const attributes = { ...options.attributes };
  return {
    nodeType: 1, localName: "button", ownerDocument: document,
    isConnected: true, parentElement: null, disabled: false, inert: false,
    style: { visibility: "visible", display: "block", opacity: "1", contentVisibility: "visible", ...options.style },
    rect: { left: 10, top: 20, right: 110, bottom: 60, width: 100, height: 40 },
    getClientRects() { return this.rects || [this.rect]; },
    getBoundingClientRect() { return this.rect; },
    getAttribute: (name) => attributes[name] ?? null,
    hasAttribute: (name) => Object.hasOwn(attributes, name),
    matches(selector) {
      assert.equal(selector, ":disabled");
      return this.nativeDisabled || this.disabled;
    },
    ...Object.fromEntries(Object.entries(options).filter(([key]) => key !== "style")),
  };
}

function harness(options = {}) {
  const state = {
    documentId: "document-a", loaderId: "loader-a", backendRoot: 100, frameId: "main",
    calls: [], scripts: [], warnings: [], acquired: 0, released: 0,
    objects: new Map(), leases: new Map(), nextObject: 0, pressed: new Set(), mouse: null,
    ...options,
  };
  const updated = event(), removed = event(), navigated = event();
  const document = {
    documentElement: { clientWidth: 800, clientHeight: 600 },
    body: {}, activeElement: null,
    querySelectorAll(selector) {
      if (selector === "[") throw new SyntaxError("invalid CSS selector");
      return state.selectors[selector] || [];
    },
    elementFromPoint(x, y) {
      if (x < 0 || y < 0 || x >= 800 || y >= 600) return null;
      return typeof state.hit === "function" ? state.hit(x, y) : state.hit;
    },
  };
  document.activeElement = document.body;
  const target = element(document);
  state.selectors = { "#target": [target], button: [target] };
  state.backends = new Map([[101, target]]);
  state.hit = target;
  const window = { innerWidth: 800, innerHeight: 600 };
  window.top = window;
  const page = vm.createContext({ document, window, getComputedStyle: (node) => node.style });
  const chrome = {
    runtime: {
      getPlatformInfo: async () => {
        state.platformReads = (state.platformReads || 0) + 1;
        return { os: state.os || "mac" };
      },
    },
    tabs: { onUpdated: updated, onRemoved: removed },
    debugger: { onEvent: navigated },
    scripting: {
      executeScript: async (args) => {
        state.scripts.push(args);
        await state.scriptHook?.(state);
        if (state.scriptError) throw new Error(state.scriptError);
        const result = vm.runInNewContext(`(${args.func})()`, { location: { href: "https://example.test/" } });
        return [{ frameId: 0, documentId: state.documentId, result, ...state.scriptResult }];
      },
    },
  };
  function remote(node, objectGroup) {
    if (!node) return { type: "undefined" };
    const objectId = `object-${++state.nextObject}`;
    state.objects.set(objectId, { node, objectGroup });
    return { objectId, type: "object", subtype: "node" };
  }
  const cdpSessions = {
    acquire: async (tabId) => {
      state.acquired++;
      await state.acquireHook?.(tabId);
      if (state.acquireError) throw new Error(state.acquireError);
      const lease = {};
      state.leases.set(lease, tabId);
      return lease;
    },
    release: async (tabId, lease) => {
      assert.equal(state.leases.get(lease), tabId, "release uses its explicit live lease");
      state.leases.delete(lease);
      state.released++;
      if (state.releaseError) throw new Error(state.releaseError);
    },
    send: async (tabId, method, params, lease) => {
      assert.equal(state.leases.get(lease), tabId, "every CDP command uses an explicit live lease");
      state.calls.push({ tabId, method, params: plain(params) });
      const override = await state.commandHook?.(method, params, state, tabId);
      if (override !== undefined) return override;
      switch (method) {
        case "Page.enable": return {};
        case "Page.getFrameTree":
          return { frameTree: { frame: { id: state.frameId, loaderId: state.loaderId } } };
        case "DOM.getDocument": return { root: { backendNodeId: state.backendRoot } };
        case "DOM.resolveNode":
          assert.match(params.objectGroup, /^rappter-input-/);
          return { object: remote(state.backends.get(params.backendNodeId), params.objectGroup) };
        case "Runtime.evaluate":
          assert.match(params.objectGroup, /^rappter-input-/);
          assert.equal(params.returnByValue, false);
          try {
            return { result: remote(vm.runInContext(params.expression, page), params.objectGroup) };
          } catch (error) {
            return { exceptionDetails: { exception: { description: error.message } } };
          }
        case "Runtime.callFunctionOn": {
          assert.equal(params.returnByValue, true);
          const node = state.objects.get(params.objectId)?.node;
          assert.ok(node);
          try {
            const fn = vm.runInContext(`(${params.functionDeclaration})`, page);
            return { result: { value: fn.apply(node, params.arguments.map((arg) => arg.value)) } };
          } catch (error) {
            return { exceptionDetails: { exception: { description: error.message } } };
          }
        }
        case "Runtime.releaseObjectGroup":
          for (const [id, object] of state.objects) {
            if (object.objectGroup === params.objectGroup) state.objects.delete(id);
          }
          return {};
        case "Input.dispatchMouseEvent":
          if (params.type === "mousePressed") state.mouse = document.elementFromPoint(params.x, params.y);
          if (params.type === "mouseReleased") {
            if (!state.noFocus && state.mouse) document.activeElement = state.focusOverride || state.mouse;
            state.mouse = null;
          }
          return {};
        case "Input.dispatchKeyEvent":
          if (params.type === "keyUp") state.pressed.delete(params.key);
          else state.pressed.add(params.key);
          return {};
        case "Input.insertText":
          state.inserted = params.text;
          return {};
        default: throw new Error(`unexpected CDP command ${method}`);
      }
    },
  };
  const context = vm.createContext({
    chrome, cdpSessions, crypto, TextEncoder,
    console: { warn: (message) => state.warnings.push(message) },
  });
  for (const name of ["perception.js", "input.js"]) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, "extension", name), "utf8"), context, { filename: name });
  }
  return {
    state, context, target, document, window, updated, removed, navigated,
    run: (args = {}) => {
      const mouse = !args.action || ["click", "hover", "scroll"].includes(args.action);
      const explicit = ["x", "y", "selector", "index", "ref", "target"].some((key) => Object.hasOwn(args, key));
      return context.browserInput({
        tabId: 7, action: "click", ...(mouse && !explicit ? { selector: "#target" } : {}), ...args,
      });
    },
    inputs: () => state.calls.filter((call) => call.method.startsWith("Input.")),
    clean() {
      assert.equal(state.acquired, state.released);
      assert.equal(state.leases.size, 0);
      assert.equal(state.objects.size, 0);
      assert.equal(state.pressed.size, 0);
      assert.equal(state.mouse, null);
      for (const evt of [updated, removed, navigated]) assert.equal(evt.listeners.size, 0);
      assert.equal(vm.runInContext("inputQueues.size", context), 0);
    },
  };
}

test("selectors, coordinates, and document DOM refs produce native clicks and clean leases", async () => {
  for (const target of [{ selector: "#target" }, { x: 15.5, y: 30.25 }, { ref: "ax:7:document-a:d:101" }]) {
    const h = harness();
    const result = plain(await h.run(target));
    const point = "x" in target ? target : { x: 60, y: 40 };
    assert.deepEqual(result, {
      tabId: 7, action: "click", dispatched: true, documentId: "document-a",
      point, modifiers: [], button: "left", clickCount: 1,
    });
    const inputs = h.inputs();
    assert.deepEqual(inputs.map((call) => call.params.type), ["mouseMoved", "mousePressed", "mouseReleased"]);
    assert.deepEqual(inputs[1].params, {
      type: "mousePressed", ...point, button: "left", clickCount: 1, buttons: 1, modifiers: 0,
    });
    assert.equal(inputs[2].params.buttons, 0);
    assert.equal(h.state.calls.at(-1).method, "Runtime.releaseObjectGroup");
    assert.ok(h.state.scripts.length >= 3);
    h.clean();
  }
});

test("selector index is zero based and selectors remain quoted data, never executable input", async () => {
  const h = harness();
  const second = element(h.document, { rect: { left: 120, right: 220, top: 20, bottom: 60, width: 100, height: 40 } });
  const selector = "[data-text='\"; throw new Error(\"injected\"); //']";
  h.state.selectors[selector] = [h.target, second];
  h.state.hit = second;
  const result = await h.run({ selector, index: 1 });
  assert.deepEqual(plain(result.point), { x: 170, y: 40 });
  h.clean();
  for (const target of [{ selector: "#missing" }, { selector: "#target", index: 20 }, { selector: "[" }]) {
    const missing = harness();
    await assert.rejects(missing.run(target), /not found|invalid CSS/);
    assert.equal(missing.inputs().length, 0);
    missing.clean();
  }
});

test("click buttons and real double/triple sequences use progressive click counts", async () => {
  for (const [button, buttons] of [["left", 1], ["right", 2], ["middle", 4]]) {
    for (const clickCount of [1, 2, 3]) {
      const h = harness();
      await h.run({ button, clickCount });
      const presses = h.inputs().filter((call) => call.params.type === "mousePressed");
      const releases = h.inputs().filter((call) => call.params.type === "mouseReleased");
      assert.equal(presses.length, clickCount);
      assert.deepEqual(presses.map((call) => call.params.clickCount), Array.from({ length: clickCount }, (_, i) => i + 1));
      for (const call of presses) {
        assert.equal(call.params.button, button);
        assert.equal(call.params.buttons, buttons);
      }
      assert.deepEqual(releases.map((call) => call.params.clickCount), presses.map((call) => call.params.clickCount));
      h.clean();
    }
  }
});

test("hover and wheel use viewport CSS coordinates, signed deltas, and modifier masks", async () => {
  const h = harness();
  await h.run({ action: "hover", modifiers: ["shift", "ctrl"] });
  let inputs = h.inputs();
  assert.deepEqual(inputs.map((call) => call.params.type), ["rawKeyDown", "rawKeyDown", "mouseMoved", "keyUp", "keyUp"]);
  assert.equal(inputs[2].params.modifiers, 10);
  h.clean();
  h.state.calls.length = 0;
  assert.equal((await h.run({ action: "scroll", deltaY: -240.5, modifiers: ["alt"] })).deltaX, 0);
  inputs = h.inputs();
  assert.deepEqual(inputs[1].params, {
    type: "mouseWheel", x: 60, y: 40, modifiers: 1, deltaX: 0, deltaY: -240.5,
  });
  h.clean();
  const boundary = harness();
  await boundary.run({ action: "scroll", deltaX: 1000000, deltaY: -1000000 });
  boundary.clean();
});

test("Unicode typing uses insertText only, with optional native focus and a UTF-8 byte result", async () => {
  for (const targeted of [false, true]) {
    const h = harness();
    const text = "Hello, 世界 👩🏽‍💻\nمرحبا";
    const result = await h.run({ action: "type", ...(targeted ? { selector: "#target" } : {}), text });
    assert.equal(result.textBytes, Buffer.byteLength(text));
    assert.equal(result.text, undefined, "result must not echo potentially sensitive text");
    assert.equal(h.state.inserted, text);
    assert.deepEqual(h.inputs().map((call) => call.method), targeted
      ? ["Input.dispatchMouseEvent", "Input.dispatchMouseEvent", "Input.dispatchMouseEvent", "Input.insertText"]
      : ["Input.insertText"]);
    assert.equal(h.inputs().at(-1).params.text, text);
    h.clean();
  }
  const h = harness();
  await h.run({ action: "type", text: "" });
  await h.run({ action: "type", text: "é".repeat(512 * 1024) });
  h.clean();
});

test("targeted typing and keys reject unfocused or redirected native clicks", async () => {
  for (const action of ["type", "key"]) {
    for (const options of [{ noFocus: true }, { focusOverride: {} }]) {
      const h = harness(options);
      await assert.rejects(h.run({
        action, selector: "#target", ...(action === "type" ? { text: "private" } : { key: "Enter" }),
      }), /did not receive focus/);
      assert.ok(h.inputs().every((call) => call.method === "Input.dispatchMouseEvent"));
      h.clean();
    }
  }
});

test("keys dispatch named and Unicode keys, with deterministic modifiers and reverse releases", async () => {
  const h = harness();
  await h.run({ action: "key", key: "a", modifiers: ["cmd", "shift", "alt", "ctrl"] });
  const keys = h.inputs().map((call) => call.params);
  assert.deepEqual(keys.map((event) => [event.type, event.key, event.modifiers]), [
    ["rawKeyDown", "Control", 2], ["rawKeyDown", "Alt", 3],
    ["rawKeyDown", "Shift", 11], ["rawKeyDown", "Meta", 15],
    ["rawKeyDown", "A", 15], ["keyUp", "A", 15],
    ["keyUp", "Meta", 11], ["keyUp", "Shift", 3],
    ["keyUp", "Alt", 2], ["keyUp", "Control", 0],
  ]);
  assert.equal(keys[4].text, undefined);
  assert.equal(keys[4].unmodifiedText, "A");
  assert.equal(keys[4].code, "KeyA");
  h.clean();
  for (const [key, code, virtualKey, text] of [
    ["Enter", "Enter", 13, "\r"], ["Tab", "Tab", 9, undefined],
    ["ArrowLeft", "ArrowLeft", 37, undefined], ["F24", "F24", 135, undefined],
    ["Space", "Space", 32, " "], ["z", "KeyZ", 90, "z"],
    ["!", "Digit1", 49, "!"], ["+", "Equal", 187, "+"],
    ["😀", "", 0, "😀"], ["é", "", 0, "é"],
  ]) {
    const one = harness();
    await one.run({ action: "key", key });
    const [down, up] = one.inputs().map((call) => call.params);
    assert.equal(down.code, code);
    assert.equal(down.windowsVirtualKeyCode, virtualKey);
    assert.equal(down.text, text);
    assert.equal(down.type, text ? "keyDown" : "rawKeyDown");
    assert.equal(up.type, "keyUp");
    assert.equal(up.text, undefined);
    one.clean();
  }
  const modifier = harness();
  await modifier.run({ action: "key", key: "Control" });
  assert.deepEqual(modifier.inputs().map((call) => call.params.modifiers), [2, 0]);
  modifier.clean();
});

test("Shift modifies printable US-keyboard keys but leaves named keys intact", async () => {
  const ordinary = "`1234567890-=[]\\;',./";
  const shiftedASCII = "~!@#$%^&*()_+{}|:\"<>?";
  const cases = [
    ...Array.from("abcdefghijklmnopqrstuvwxyz", (key) => [key, key.toUpperCase()]),
    ...Array.from(ordinary, (key, index) => [key, shiftedASCII[index]]),
    ["é", "é"],
  ];
  for (const [key, shifted] of cases) {
    const h = harness();
    const result = await h.run({ action: "key", key, modifiers: ["shift"] });
    const down = h.inputs()[1].params;
    assert.equal(down.type, "keyDown");
    assert.equal(down.key, shifted);
    assert.equal(down.text, shifted);
    assert.equal(down.unmodifiedText, shifted, "CDP unmodifiedText retains Shift");
    assert.equal(result.key, shifted);
    const unshifted = harness();
    await unshifted.run({ action: "key", key });
    assert.equal(down.code, unshifted.inputs()[0].params.code);
    assert.equal(down.windowsVirtualKeyCode, unshifted.inputs()[0].params.windowsVirtualKeyCode);
    unshifted.clean();
    h.clean();
  }
  const h = harness();
  await h.run({ action: "key", key: "Enter", modifiers: ["shift"] });
  assert.equal(h.inputs()[1].params.key, "Enter");
  assert.equal(h.inputs()[1].params.text, "\r");
  h.clean();
});

test("macOS Cmd editing combos supply native CDP commands only on the actual key-down", async () => {
  for (const [key, modifiers, command] of [
    ["a", ["cmd"], "selectAll"], ["c", ["cmd"], "copy"], ["x", ["cmd"], "cut"],
    ["v", ["cmd"], "paste"], ["z", ["cmd"], "undo"],
    ["z", ["cmd", "shift"], "redo"], ["v", ["cmd", "shift"], "pasteAndMatchStyle"],
  ]) {
    const h = harness();
    const result = await h.run({ action: "key", key, modifiers });
    const events = h.inputs().filter((call) => call.method === "Input.dispatchKeyEvent").map((call) => call.params);
    const commands = events.filter((event) => event.commands);
    assert.equal(commands.length, 1);
    assert.deepEqual(commands[0].commands, [command]);
    assert.deepEqual(plain(result.editingCommands), [command]);
    assert.equal(commands[0].type, "rawKeyDown");
    assert.equal(commands[0].text, undefined, "Cmd shortcuts must not insert characters");
    assert.equal(commands[0].unmodifiedText, modifiers.includes("shift") ? key.toUpperCase() : key);
    assert.equal(h.state.platformReads, 1);
    h.clean();
  }
  for (const options of [
    { os: "win", modifiers: ["cmd"] }, { os: "linux", modifiers: ["cmd"] },
    { os: "mac", modifiers: ["ctrl"] }, { os: "mac", modifiers: ["cmd", "alt"] },
    { os: "mac", modifiers: ["cmd", "ctrl"] }, { os: "mac", modifiers: ["cmd", "shift"] },
  ]) {
    const h = harness({ os: options.os });
    const result = await h.run({ action: "key", key: "a", modifiers: options.modifiers });
    assert.ok(h.inputs().every((call) => call.params.commands === undefined));
    assert.deepEqual(plain(result.editingCommands), []);
    h.clean();
  }
});

test("macOS word/line/document motion and deletion use bounded native editing bindings", async () => {
  for (const [modifier, key, command] of [
    ["cmd", "ArrowLeft", "moveToLeftEndOfLine"],
    ["cmd", "ArrowRight", "moveToRightEndOfLine"],
    ["cmd", "ArrowUp", "moveToBeginningOfDocument"],
    ["cmd", "ArrowDown", "moveToEndOfDocument"],
    ["alt", "ArrowLeft", "moveWordLeft"],
    ["alt", "ArrowRight", "moveWordRight"],
    ["cmd", "Backspace", "deleteToBeginningOfLine"],
    ["cmd", "Delete", "deleteToEndOfLine"],
    ["alt", "Backspace", "deleteWordBackward"],
    ["alt", "Delete", "deleteWordForward"],
  ]) {
    for (const shift of command.startsWith("move") ? [false, true] : [false]) {
      const h = harness();
      const modifiers = [modifier, ...(shift ? ["shift"] : [])];
      const expected = command + (shift ? "AndModifySelection" : "");
      const result = await h.run({ action: "key", key, modifiers });
      assert.deepEqual(plain(result.editingCommands), [expected]);
      const sent = h.inputs().filter((call) => call.params.commands);
      assert.equal(sent.length, 1);
      assert.equal(sent[0].params.type, "rawKeyDown");
      assert.equal(sent[0].params.key, key, "application keyboard listeners still receive their key event");
      assert.deepEqual(sent[0].params.commands, [expected]);
      h.clean();
      const nonMac = harness({ os: "linux" });
      const dispatched = await nonMac.run({ action: "key", key, modifiers });
      assert.deepEqual(plain(dispatched.editingCommands), []);
      assert.ok(nonMac.inputs().every((call) => call.params.commands === undefined));
      nonMac.clean();
    }
  }
});

test("targeted Enter focuses with a native click then sends keyDown with carriage-return text", async () => {
  const h = harness();
  h.target.localName = "input";
  h.state.commandHook = (method, params) => {
    if (method === "Input.dispatchKeyEvent") {
      assert.equal(h.document.activeElement, h.target, "focus must precede keyboard dispatch");
      assert.ok(h.state.calls.some((call) =>
        call.method === "Runtime.callFunctionOn" && call.params.arguments[1].value === true),
      "the native click must be followed by focus validation");
    }
  };
  const result = await h.run({ action: "key", selector: "#target", key: "Enter" });
  assert.equal(result.key, "Enter");
  assert.deepEqual(h.inputs().map((call) => call.params.type), [
    "mouseMoved", "mousePressed", "mouseReleased", "keyDown", "keyUp",
  ]);
  assert.deepEqual(h.inputs()[3].params, {
    type: "keyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13,
    modifiers: 0, text: "\r", unmodifiedText: "\r",
  });
  assert.deepEqual(h.inputs()[4].params, {
    type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, modifiers: 0,
  });
  h.clean();
});

test("validation rejects malformed or mixed arguments before any browser side effect", async () => {
  const h = harness();
  for (const args of [null, [], {}, "click", undefined]) await assert.rejects(h.context.browserInput(args));
  const invalid = [
    ...[-1, 0.5, "7", Infinity, Number.MAX_SAFE_INTEGER + 1].map((tabId) => ({ tabId })),
    { action: "drag" }, { target: null }, { target: [] }, { target: {} }, { target: undefined },
    { target: { selector: "#target" } },
    { x: 1 }, { y: 1 }, { index: 0 }, { x: 1, y: 1, selector: "a" },
    { selector: "a", ref: "ax:7:document-a:d:101" }, { x: 1, y: 1, ref: "ax:7:document-a:d:101" },
    { x: 1, y: 1, index: 0 }, { ref: "ax:7:document-a:d:101", index: 0 },
    { selector: "", index: 0 }, { selector: " " }, { selector: undefined },
    { selector: 1 }, { selector: "a", typo: 0 },
    ...[-1, 0.5, "0", null, Number.MAX_SAFE_INTEGER + 1].map((index) => ({ selector: "a", index })),
    ...[-1, NaN, Infinity, 1000001, "1"].flatMap((v) => [{ x: v, y: 1 }, { x: 1, y: v }]),
    ...["ax:8:document-a:d:101", "ax:7:document-a:d:101:v:button:0", "ax:7:document-a:d:0",
      "ax:7:document-a:d:9007199254740992", "ax:7:document-a:d:01", "invalid", 101].map((ref) => ({ ref })),
    ...["primary", null, 0].map((button) => ({ button })),
    ...[0, 4, 1.5, "2", null].map((clickCount) => ({ clickCount })),
    ...[null, "ctrl", ["control"], ["ctrl", "ctrl"], [1], [["ctrl"]]].map((modifiers) => ({ modifiers })),
    { action: "hover", button: "left" }, { action: "type", text: "x", modifiers: [] },
    { action: "type", text: "x", deltaY: 1 }, { action: "click", text: "x" },
    { action: "scroll" }, { action: "scroll", deltaX: 0, deltaY: 0 },
    ...[Infinity, NaN, 1000001, -1000001, "1", null].map((deltaY) => ({ action: "scroll", deltaY })),
    ...[null, 1, undefined, "x".repeat(1024 * 1024 + 1), "😀".repeat(262145)].map((text) => ({ action: "type", text })),
    ...[null, 1, "", "ab", "UnknownKey", "F25", "\n", "\ud800"].map((key) => ({ action: "key", key })),
    { action: "key", key: "Control", modifiers: ["ctrl"] },
  ];
  for (const args of invalid) await assert.rejects(h.run(args), undefined, JSON.stringify(args).slice(0, 200));
  for (const action of ["click", "hover", "scroll"]) {
    await assert.rejects(h.context.browserInput({ tabId: 7, action }), /requires a target/);
  }
  assert.equal(h.state.acquired, 0);
  assert.equal(h.state.calls.length, 0);
  assert.equal(h.state.scripts.length, 0);
  h.clean();
});

test("geometry rejects hidden, zero-area, outside-viewport, detached, foreign, inert and disabled elements", async () => {
  const variants = [
    { isConnected: false }, { nodeType: 3 }, { ownerDocument: {} },
    { style: { display: "none" } }, { style: { visibility: "hidden" } },
    { style: { visibility: "collapse" } }, { style: { opacity: "0" } },
    { style: { contentVisibility: "hidden" } }, { disabled: true }, { nativeDisabled: true },
    { inert: true }, { attributes: { inert: "" } }, { attributes: { "aria-disabled": " true " } },
    { rects: [] }, { rects: [{ width: 0, height: 10 }] },
    { rect: { left: 900, right: 1000, top: 20, bottom: 60, width: 100, height: 40 } },
    { rect: { left: 10, right: 110, top: -60, bottom: -20, width: 100, height: 40 } },
    { rect: { left: NaN, right: 110, top: 20, bottom: 60, width: 100, height: 40 } },
  ];
  for (const options of variants) {
    const h = harness();
    const target = element(h.document, options);
    h.state.selectors["#target"] = [target];
    h.state.hit = target;
    await assert.rejects(h.run(), /target|document/);
    assert.equal(h.inputs().length, 0);
    h.clean();
  }
  for (const options of [
    { style: { display: "none" } }, { style: { opacity: "0" } },
    { style: { contentVisibility: "hidden" } }, { inert: true },
    { attributes: { "aria-disabled": "true" } }, { disabled: true },
  ]) {
    const h = harness();
    h.target.parentElement = element(h.document, options);
    await assert.rejects(h.run(), /hidden|inert|disabled/);
    assert.equal(h.inputs().length, 0);
    h.clean();
  }
});

test("hit tests allow descendants and partial viewport visibility but refuse occlusion and blocked descendants", async () => {
  const h = harness();
  h.target.rect = { left: -10, right: 20, top: -10, bottom: 20, width: 30, height: 30 };
  h.state.hit = element(h.document, { parentElement: h.target });
  assert.deepEqual(plain((await h.run()).point), { x: 10, y: 10 });
  h.clean();
  for (const kind of ["overlay", "null", "disabled-descendant", "inert-descendant"]) {
    const one = harness();
    one.state.hit = kind === "null" ? null : element(one.document, {
      ...(kind.includes("descendant") ? { parentElement: one.target } : {}),
      disabled: kind === "disabled-descendant", inert: kind === "inert-descendant",
    });
    await assert.rejects(one.run(), /obscured|hit-testable|disabled|inert/);
    assert.equal(one.inputs().length, 0);
    one.clean();
  }
  const child = harness();
  child.window.top = {};
  await assert.rejects(child.run({ ref: "ax:7:document-a:d:101" }), /main document/);
  child.clean();
});

test("DOM refs reject stale documents, missing backends, and detached or foreign resolved nodes", async () => {
  for (const target of [{ ref: "ax:7:old-document:d:101" }, { ref: "ax:7:document-a:d:999" }]) {
    const h = harness();
    await assert.rejects(h.run(target), /stale|not found/);
    assert.equal(h.inputs().length, 0);
    h.clean();
  }
  for (const property of ["isConnected", "ownerDocument"]) {
    const h = harness();
    h.target[property] = property === "isConnected" ? false : {};
    await assert.rejects(h.run({ ref: "ax:7:document-a:d:101" }), /connected|document/);
    assert.equal(h.inputs().length, 0);
    h.clean();
  }
});

test("documentId, root, loader, and frame identity are rechecked after target resolution", async () => {
  for (const property of ["documentId", "backendRoot", "loaderId", "frameId"]) {
    const h = harness();
    h.state.commandHook = (method, _params, state) => {
      if (method === "Runtime.evaluate") state[property] = property === "backendRoot" ? 999 : "changed";
    };
    await assert.rejects(h.run(), /Navigation/);
    assert.equal(h.inputs().length, 0);
    h.clean();
  }
});

test("navigation listeners reject away-and-back races but ignore other tabs and child frames", async () => {
  for (const kind of ["tab", "frame", "frameDetached", "closed"]) {
    const h = harness();
    h.state.commandHook = (method) => {
      if (method !== "Runtime.callFunctionOn") return;
      if (kind === "tab") h.updated.emit(7, { status: "loading" });
      if (kind === "frame") h.navigated.emit({ tabId: 7 }, "Page.frameNavigated", { frame: { id: "main" } });
      if (kind === "frameDetached") h.navigated.emit({ tabId: 7 }, "Page.frameDetached", { frameId: "main" });
      if (kind === "closed") h.removed.emit(7);
    };
    await assert.rejects(h.run(), /Navigation/);
    assert.equal(h.inputs().length, 0);
    h.clean();
  }
  const h = harness();
  h.state.commandHook = (method) => {
    if (method !== "Runtime.evaluate") return;
    h.updated.emit(8, { status: "loading" });
    h.removed.emit(8);
    h.navigated.emit({ tabId: 8 }, "Page.frameNavigated", { frame: {} });
    h.navigated.emit({ tabId: 7 }, "Page.frameNavigated", { frame: { id: "child", parentId: "main" } });
    h.navigated.emit({ tabId: 7, sessionId: "child" }, "Page.frameNavigated", { frame: {} });
  };
  await h.run();
  h.clean();
});

test("hover-driven layout changes are remeasured before pressing and navigation never reuses coordinates", async () => {
  const h = harness();
  h.state.commandHook = (method, params) => {
    if (method === "Input.dispatchMouseEvent" && params.type === "mouseMoved") {
      h.target.rect = { left: 200, right: 300, top: 20, bottom: 60, width: 100, height: 40 };
    }
  };
  assert.equal((await h.run()).point.x, 250);
  assert.equal(h.inputs()[1].params.x, 250);
  h.clean();
  for (const stage of ["mouseMoved", "mouseReleased"]) {
    const one = harness();
    one.state.commandHook = (method, params) => {
      if (method === "Input.dispatchMouseEvent" && params.type === stage) one.updated.emit(7, { status: "loading" });
    };
    await assert.rejects(one.run({ clickCount: 2 }), /Navigation/);
    assert.equal(one.inputs().filter((call) => call.params.type === "mousePressed").length, stage === "mouseMoved" ? 0 : 1);
    one.clean();
  }
});

test("navigation caused by a final click or Enter still releases buttons, keys and modifiers", async () => {
  for (const action of ["click", "key"]) {
    const h = harness();
    h.state.commandHook = (method, params) => {
      if ((action === "click" && method === "Input.dispatchMouseEvent" && params.type === "mousePressed")
          || (action === "key" && params.key === "Enter" && params.type !== "keyUp")) {
        h.updated.emit(7, { status: "loading" });
      }
    };
    const result = await h.run({
      action, modifiers: ["shift"],
      ...(action === "key" ? { key: "Enter" } : {}),
    });
    assert.equal(result.dispatched, true);
    assert.equal(h.inputs().at(-1).params.key, "Shift");
    assert.equal(h.inputs().at(-1).params.type, "keyUp");
    h.clean();
  }
});

test("failure during mouse press or release attempts native button release and preserves the original error", async () => {
  for (const failType of ["mousePressed", "mouseReleased"]) {
    const h = harness();
    let failed = false;
    h.state.commandHook = (method, params, state) => {
      if (method === "Input.dispatchMouseEvent" && params.type === failType && !failed) {
        failed = true;
        state.mouse = h.target; // The browser may deliver input before a failed response.
        throw new Error("original mouse failure");
      }
    };
    await assert.rejects(h.run({ modifiers: ["ctrl"] }), /^Error: original mouse failure$/);
    const releases = h.inputs().filter((call) => call.params.type === "mouseReleased");
    assert.ok(releases.length >= 1);
    assert.equal(releases.at(-1).params.clickCount, 0);
    h.clean();
  }
});

test("every possibly pressed key is released after down/up failure, including failed modifier downs", async () => {
  for (const [failKey, failType] of [["a", "rawKeyDown"], ["a", "keyUp"], ["Alt", "rawKeyDown"]]) {
    const h = harness();
    let failed = false;
    h.state.commandHook = (method, params, state) => {
      if (method === "Input.dispatchKeyEvent" && params.key === failKey && params.type === failType && !failed) {
        failed = true;
        state.pressed.add(params.key);
        throw new Error("original key failure");
      }
    };
    await assert.rejects(h.run({ action: "key", key: "a", modifiers: ["ctrl", "alt"] }), /^Error: original key failure$/);
    const keys = h.inputs().filter((call) => call.params.type === "keyUp").map((call) => call.params.key);
    assert.deepEqual(keys.slice(-2), ["Alt", "Control"]);
    h.clean();
  }
});

test("cleanup retries failed releases and continues releasing other modifiers on persistent failure", async () => {
  for (const persistent of [false, true]) {
    const h = harness();
    let failures = persistent ? 2 : 1;
    h.state.commandHook = (method, params, state) => {
      if (method === "Input.dispatchKeyEvent" && params.type === "keyUp" && params.key === "Alt" && failures-- > 0) {
        state.pressed.delete("Alt");
        throw new Error("Alt release failure");
      }
    };
    const pending = h.run({ action: "key", key: "a", modifiers: ["ctrl", "alt"] });
    if (persistent) await assert.rejects(pending, /input cleanup failed: Alt release failure/);
    else await pending;
    assert.equal(h.inputs().at(-1).params.key, "Control");
    assert.equal(h.inputs().at(-1).params.modifiers, 0);
    h.clean();
  }
});

test("cleanup releases remote objects and explicit lease without masking operational failure", async () => {
  const h = harness({ releaseError: "lease release failure" });
  h.state.commandHook = (method, _params, state) => {
    if (method === "Runtime.evaluate") throw new Error("original resolution failure");
    if (method === "Runtime.releaseObjectGroup") {
      state.objects.clear();
      throw new Error("object release failure");
    }
  };
  await assert.rejects(h.run(), /^Error: original resolution failure$/);
  assert.equal(h.state.warnings.length, 2);
  h.clean();
  for (const kind of ["object", "lease"]) {
    const one = harness({ ...(kind === "lease" ? { releaseError: "cleanup transport failure" } : {}) });
    if (kind === "object") one.state.commandHook = (method, _params, state) => {
      if (method === "Runtime.releaseObjectGroup") {
        state.objects.clear();
        throw new Error("cleanup transport failure");
      }
    };
    await assert.rejects(one.run(), /input cleanup failed: cleanup transport failure/);
    one.clean();
  }
});

test("destroyed remote contexts are logged; acquisition and scripting failures leave no listeners", async () => {
  const h = harness();
  h.state.commandHook = (method, _params, state) => {
    if (method === "Runtime.releaseObjectGroup") {
      state.objects.clear();
      throw new Error("Execution context was destroyed");
    }
  };
  await h.run();
  assert.equal(h.state.warnings.length, 1);
  h.clean();
  for (const options of [
    { acquireError: "another debugger is attached" },
    { scriptError: "Cannot access chrome:// URL" },
    { scriptResult: { documentId: undefined } },
  ]) {
    const one = harness(options);
    await assert.rejects(one.run(), /another debugger|Cannot access|documentId/);
    if (options.acquireError) {
      assert.equal(one.state.released, 0);
      one.state.acquired = 0;
    }
    one.clean();
  }
});

test("same-tab action sequences serialize completely while other tabs proceed independently", async () => {
  const h = harness(), gate = deferred();
  let blocked = false;
  h.state.commandHook = async (method, params, _state, tabId) => {
    if (tabId === 7 && method === "Input.dispatchKeyEvent" && params.type === "rawKeyDown" && !blocked) {
      blocked = true;
      await gate.promise;
    }
  };
  const first = h.run({ action: "key", key: "a", modifiers: ["ctrl"] });
  await flush();
  const args = { action: "type", text: "queued" };
  const second = h.run(args);
  args.text = "mutated";
  const third = h.run({ tabId: 8, action: "type", text: "other tab" });
  await third;
  assert.equal(h.state.acquired, 2, "waiting same-tab operation has not acquired a lease");
  assert.equal(h.state.inserted, "other tab");
  gate.resolve();
  await Promise.all([first, second]);
  const input = h.inputs();
  const queued = input.findIndex((call) => call.method === "Input.insertText" && call.params.text === "queued");
  const controlUp = input.findIndex((call) => call.params.type === "keyUp" && call.params.key === "Control");
  assert.ok(queued > controlUp);
  h.clean();
});

test("a failed queued operation cannot poison the next action's turn", async () => {
  const h = harness();
  let failures = 1;
  h.state.commandHook = (method) => {
    if (method === "Input.insertText" && failures-- > 0) throw new Error("first failed");
  };
  const first = assert.rejects(h.run({ action: "type", text: "one" }), /first failed/);
  const second = h.run({ action: "type", text: "two" });
  await first;
  assert.equal((await second).dispatched, true);
  assert.equal(h.state.inserted, "two");
  h.clean();
});
