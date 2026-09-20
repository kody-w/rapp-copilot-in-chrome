#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function event() {
  const listeners = new Set();
  return {
    addListener: (fn) => listeners.add(fn),
    removeListener: (fn) => listeners.delete(fn),
    emit: (...args) => { for (const fn of listeners) fn(...args); },
  };
}

async function main() {
  const imported = [];
  const commands = [];
  let attaches = 0;
  const context = vm.createContext({
    console, setTimeout, clearTimeout, setInterval: () => 0, clearInterval,
    crypto: require("node:crypto").webcrypto,
    WebSocket: { OPEN: 1, CONNECTING: 0 },
    chrome: {
      alarms: { create() {}, onAlarm: event() },
      runtime: { onStartup: event(), onInstalled: event(), onMessage: event() },
      storage: { local: {
        get: async () => ({ instanceId: "profile-7", profileName: "Work", token: "" }),
        set: async () => {},
      } },
      tabs: { onUpdated: event(), onRemoved: event() },
      scripting: {
        executeScript: async ({ target, world, func, args }) => [{
          result: { tabId: target.tabId, world, function: func.name, args },
        }],
      },
      debugger: {
        onDetach: event(), onEvent: event(),
        attach: async () => { attaches++; },
        detach: async (target) => context.chrome.debugger.onDetach.emit(target, "canceled_by_user"),
        sendCommand: async (target, method, params) => {
          commands.push({ target, method, params });
          return params.expression === "throw"
            ? { exceptionDetails: { exception: { description: "page exception" } } }
            : { result: { value: 42 } };
        },
      },
    },
  });
  context.importScripts = (...files) => {
    for (const file of files) {
      imported.push(file);
      vm.runInContext(fs.readFileSync(path.join(__dirname, "extension", file), "utf8"), context, {
        filename: file,
      });
    }
  };
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, "extension/manifest.json")));
  assert.notEqual(manifest.background.type, "module");
  vm.runInContext(fs.readFileSync(path.join(__dirname, "extension", manifest.background.service_worker), "utf8"), context);
  assert.deepEqual(imported, ["cdp.js", "capture.js", "perception.js", "assertions.js"]);
  const plain = (value) => JSON.parse(JSON.stringify(value));

  assert.equal((await context.dispatch("ping", {})).pong, true);
  assert.deepEqual(plain(await context.dispatch("browser_info", {})), [{
    name: "local Chromium", instanceId: "profile-7", profileName: "Work", connected: false,
  }]);
  vm.runInContext("ws = {readyState: WebSocket.OPEN}", context);
  assert.equal((await context.dispatch("browser_info", {}))[0].connected, true);

  for (const [cmd, functionName, args] of [
    ["text", "_pageText", []],
    ["query", "_query", ["button", 3]],
    ["click", "_click", ["button", 2]],
    ["type", "_type", ["button", "hello", true]],
    ["waitfor", "_waitFor", ["button", 50]],
  ]) {
    const result = await context.dispatch(cmd, {
      tabId: 7, selector: "button", limit: 3, index: 2, text: "hello", submit: true, timeout: 50,
    });
    assert.deepEqual(plain(result), { tabId: 7, world: "MAIN", function: functionName, args });
  }
  assert.equal(await context.dispatch("eval", { tabId: 7, code: "6 * 7" }), 42);
  assert.equal(await context.dispatch("eval", { tabId: 7, code: "6 * 7", awaitPromise: false }), 42);
  assert.equal(attaches, 1);
  assert.equal(commands[0].params.awaitPromise, true);
  assert.equal(commands[1].params.awaitPromise, false);
  await assert.rejects(context.dispatch("eval", { tabId: 7, code: "throw" }), /page exception/);

  let present = false;
  let mutation;
  let observation;
  let disconnected = 0;
  context.document = { querySelector: () => present, documentElement: {} };
  context.MutationObserver = class {
    constructor(callback) { mutation = callback; }
    observe(_root, options) { observation = options; }
    disconnect() { disconnected++; }
  };
  const waiting = context._waitFor(".ready", 50);
  assert.equal(observation.attributes, true);
  present = true;
  mutation();
  assert.equal((await waiting).found, true);
  assert.equal(disconnected, 1);
  assert.equal((await context._waitFor(".ready", 50)).waitedMs, 0);
  present = false;
  await assert.rejects(context._waitFor(".missing", 5), /timeout/);
  assert.equal(disconnected, 2);

  const seen = [];
  for (const [cmd, func] of [
    ["read_page_ax", "readPageAX"], ["find", "findElements"],
    ["assert", "assertPage"], ["screenshot", "captureScreenshot"],
  ]) {
    context[func] = async (args) => {
      seen.push(cmd);
      if (args.fail) throw new Error("stop batch");
      return { route: cmd, args };
    };
    assert.deepEqual(plain(await context.dispatch(cmd, { tabId: 7 })), {
      route: cmd, args: { tabId: 7 },
    });
  }
  seen.length = 0;
  const result = await context.dispatch("batch", { actions: [
    { cmd: "read_page_ax", args: { tabId: 7 } },
    { cmd: "find", args: { tabId: 7, role: "button" } },
    { cmd: "assert", args: { tabId: 7, condition: "visible", selector: "button" } },
    { cmd: "screenshot", args: { tabId: 7, fullPage: true } },
  ] });
  assert.deepEqual(seen, ["read_page_ax", "find", "assert", "screenshot"]);
  assert.equal(result.length, 4);
  assert.equal(result[3].result.args.fullPage, true);
  seen.length = 0;
  await assert.rejects(context.dispatch("batch", { actions: [
    { cmd: "find", args: { fail: true } }, { cmd: "screenshot" },
  ] }), /stop batch/);
  assert.deepEqual(seen, ["find"]);
  context.assertPage = async () => ({ passed: false, condition: "visible" });
  seen.length = 0;
  const failedAssertion = await context.dispatch("batch", { actions: [
    { cmd: "assert", args: { tabId: 7, condition: "visible", selector: "button" } },
    { cmd: "screenshot", args: { tabId: 7 } },
  ] });
  assert.equal(failedAssertion.length, 1);
  assert.equal(failedAssertion[0].result.passed, false);
  assert.deepEqual(seen, []);
  await assert.rejects(context.dispatch("not-a-command", {}), /unknown command/);
  console.log("Extension: classic-worker imports, legacy routes, shared eval, identity, and new batch routes passed");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
