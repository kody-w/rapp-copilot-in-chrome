#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "extension/observe.js"), "utf8");
const coreSource = fs.readFileSync(path.join(__dirname, "extension/cdp.js"), "utf8");
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
    listeners,
    addListener: (fn) => listeners.add(fn),
    removeListener: (fn) => listeners.delete(fn),
    emit: (...args) => { for (const fn of [...listeners]) fn(...args); },
  };
}
function harness(realCore = false) {
  const h = {
    now: 10000, calls: [], acquisitions: [], releases: [], leases: new Map(),
    warnings: [], attached: new Set(), timers: new Map(), nextTimer: 0,
    onEvent: event(), onDetach: event(), onRemoved: event(), onUpdated: event(),
  };
  const command = async (tabId, method, params) => {
    h.calls.push({ tabId, method, params: plain(params) });
    const result = await h.sendHook?.(tabId, method, params);
    if (result !== undefined) return result;
    if (method === "Page.getFrameTree") {
      return { frameTree: { frame: { id: `main-${tabId}`, loaderId: "initial", url: "https://example.test/" } } };
    }
    if (method === "Network.getResponseBody") return { body: "body", base64Encoded: false };
    return {};
  };
  const chrome = {
    debugger: {
      onEvent: h.onEvent, onDetach: h.onDetach,
      attach: async ({ tabId }) => { h.attached.add(tabId); h.acquisitions.push(tabId); },
      detach: async ({ tabId }) => { h.attached.delete(tabId); },
      sendCommand: async ({ tabId }, method, params) => command(tabId, method, params),
    },
    tabs: { onRemoved: h.onRemoved, onUpdated: h.onUpdated },
  };
  const context = vm.createContext({
    chrome, Date: { now: () => h.now },
    atob: (data) => Buffer.from(data, "base64").toString("binary"),
    btoa: (data) => Buffer.from(data, "binary").toString("base64"),
    console: { warn: (message) => h.warnings.push(message) },
    setTimeout: (fn, delay) => {
      const id = ++h.nextTimer;
      h.timers.set(id, { fn, at: h.now + delay });
      return id;
    },
    clearTimeout: (id) => h.timers.delete(id),
  });
  if (realCore) vm.runInContext(coreSource, context, { filename: "cdp.js" });
  else context.cdpSessions = {
    acquire: async (tabId) => {
      h.acquisitions.push(tabId);
      await h.acquireHook?.(tabId);
      const lease = {};
      h.leases.set(lease, tabId);
      return lease;
    },
    send: async (tabId, method, params, lease) => {
      assert.equal(h.leases.get(lease), tabId, "send requires the exact tab's acquired lease");
      return command(tabId, method, params);
    },
    release: async (tabId, lease) => {
      assert.equal(h.leases.get(lease), tabId, "release requires the acquired lease exactly once");
      h.releases.push(tabId);
      await h.releaseHook?.(tabId, lease);
      h.leases.delete(lease);
      return true;
    },
  };
  vm.runInContext(source, context, { filename: "observe.js" });
  h.context = context;
  h.start = (kind = "console", extra = {}) => context.observeStart({ tabId: 7, kind, ...extra });
  h.read = (kind = "console", extra = {}) => context.observeRead({ tabId: 7, kind, ...extra });
  h.stop = (kind = "console", extra = {}) => context.observeStop({ tabId: 7, kind, ...extra });
  h.emit = (method, params, tabId = 7, sourceExtra = {}) =>
    h.onEvent.emit({ tabId, ...sourceExtra }, method, params);
  h.log = (message, extra = {}, tabId = 7) => h.emit("Runtime.consoleAPICalled", {
    type: "log", args: [{ type: "string", value: message }], timestamp: h.now, ...extra,
  }, tabId);
  h.request = (id, extra = {}, tabId = 7) => h.emit("Network.requestWillBeSent", {
    requestId: id, timestamp: 10, type: "Fetch",
    request: { url: `https://example.test/${id}`, method: "GET" }, ...extra,
  }, tabId);
  h.response = (id, extra = {}, tabId = 7) => h.emit("Network.responseReceived", {
    requestId: id, timestamp: 11,
    response: { url: `https://example.test/${id}`, status: 200, mimeType: "text/plain" }, ...extra,
  }, tabId);
  h.finish = (id, tabId = 7) => h.emit("Network.loadingFinished", {
    requestId: id, timestamp: 12, encodedDataLength: 99,
  }, tabId);
  h.navigate = (loaderId = "next", extra = {}) => {
    h.now++;
    h.emit("Page.frameNavigated", {
      frame: { id: "main-7", loaderId, url: "https://example.test/", ...extra },
    });
  };
  h.bodyCalls = () => h.calls.filter((call) => call.method === "Network.getResponseBody");
  h.advance = async (ms) => {
    h.now += ms;
    for (const [id, timer] of h.timers) {
      if (timer.at <= h.now) { h.timers.delete(id); timer.fn(); }
    }
    await flush();
  };
  return h;
}

test("classic globals, defaults, explicit metadata, lease lifetime and snapshot isolation", async () => {
  const h = harness();
  for (const name of ["observeStart", "observeRead", "observeStop"]) {
    assert.equal(typeof h.context[name], "function");
  }
  const started = plain(await h.start());
  assert.equal(started.status, "active");
  assert.equal(started.generation, 0);
  assert.deepEqual(started.options, { maxEvents: 500, maxBytes: 1048576 });
  assert.equal(h.leases.size, 1);
  assert.deepEqual(h.calls.map((call) => call.method), [
    "Page.enable", "Page.getFrameTree", "Runtime.enable", "Log.enable",
  ]);
  h.log("hello");
  const read = await h.read();
  assert.equal(read.events[0].receivedAt, 10000);
  assert.equal(read.events[0].cdpTimestamp, 10000);
  assert.equal(read.events[0].cdpTimestampUnit, "epochMilliseconds");
  read.events[0].args[0].value = "mutated";
  read.counters.receivedEvents = 999;
  assert.equal((await h.read()).events[0].args[0].value, "hello");
  assert.equal((await h.read()).counters.receivedEvents, 1);
  const stopped = await h.stop();
  assert.equal(stopped.events.length, 1);
  assert.equal(stopped.status, "stopped");
  assert.equal(stopped.leaseReleased, true);
  assert.equal(h.leases.size, 0);
  assert.match(stopped.semantics.cleanup, /idle grace/);
  assert.ok(h.calls.every((call) => !call.method.endsWith(".disable")));
  await assert.rejects(h.read(), /No active/);
  await assert.rejects(h.stop(), /No active/);
  await h.start();
  assert.equal((await h.read()).events.length, 0);
  await h.stop();
});

test("strict options fail before browser calls, including false console privacy flags", async () => {
  const h = harness();
  for (const value of [null, undefined, [], "console", {}, { tabId: 7 }]) {
    await assert.rejects(h.context.observeStart(value));
  }
  const invalid = [
    ...[-1, 0.1, "7", NaN, Infinity, Number.MAX_SAFE_INTEGER + 1].map((tabId) => ({ tabId })),
    { kind: "other" }, { clear: false }, { extra: 1 },
    ...[0, 5001, 1.1, "2", null].map((maxEvents) => ({ maxEvents })),
    ...[1023, 8388609, 1.1, "2048", null].map((maxBytes) => ({ maxBytes })),
    { includeHeaders: false }, { includeBodies: false }, { maxBodyBytes: 10 },
  ];
  for (const args of invalid) await assert.rejects(h.start("console", args));
  for (const args of [
    { includeHeaders: "false" }, { includeBodies: 1 }, { includeBodies: null },
    { maxBodyBytes: 4 }, { includeBodies: false, maxBodyBytes: 4 },
    { maxBodyBytes: undefined },
    ...[0, 1048577, 1.1, null, "4"].map((maxBodyBytes) => ({ includeBodies: true, maxBodyBytes })),
  ]) await assert.rejects(h.start("network", args));
  assert.equal(h.acquisitions.length, 0);
  await h.start("console", { tabId: 0, maxEvents: 5000, maxBytes: 8388608 });
  await h.stop("console", { tabId: 0 });
  await h.start();
  for (const args of [{ clear: "false" }, { maxBytes: 1024 }, { includeHeaders: false }]) {
    await assert.rejects(h.read("console", args));
  }
  for (const args of [{ clear: false }, { maxEvents: 1 }, { includeBodies: false }]) {
    await assert.rejects(h.stop("console", args));
  }
  await h.stop();
});

test("count ring retains latest events; clear returns the old snapshot and preserves counters", async () => {
  const h = harness();
  await h.start("console", { maxEvents: 2 });
  h.log("one"); h.log("two"); h.log("three");
  let read = await h.read("console", { clear: true });
  assert.deepEqual(plain(read.events.map((item) => item.args[0].value)), ["two", "three"]);
  assert.equal(read.counters.droppedEvents, 1);
  assert.equal(read.counters.evictedEvents, 1);
  assert.equal(read.buffer.eventCount, 2);
  assert.equal(read.cleared, true);
  read = await h.read();
  assert.equal(read.events.length, 0);
  assert.equal(read.buffer.bytes, 0);
  assert.equal(read.counters.receivedEvents, 3);
  assert.equal(read.counters.clearedEvents, 2);
  h.log("four");
  assert.equal((await h.stop()).events.length, 1);
});

test("UTF-8 byte limits count serialized Unicode/escaping and oversized events preserve useful data", async () => {
  const h = harness();
  await h.start("console", { maxEvents: 100, maxBytes: 1024 });
  for (let i = 0; i < 10; i++) h.log(`${i}:漢😀".\n`.repeat(10));
  let read = await h.read();
  assert.ok(read.events.length > 0);
  assert.ok(read.events.length < 10);
  assert.ok(read.buffer.bytes <= 1024);
  assert.equal(read.buffer.bytes, read.events.reduce((sum, item) =>
    sum + Buffer.byteLength(JSON.stringify(item), "utf8"), 0));
  const previous = JSON.stringify(read.events);
  h.log("漢😀".repeat(50000));
  read = await h.read();
  assert.equal(read.counters.oversizedEvents, 1);
  assert.equal(read.counters.truncatedEvents, 1);
  assert.equal(JSON.stringify(read.events), previous);
  assert.equal(read.counters.droppedEvents, 11 - read.events.length);
  await h.stop();
});

test("console projections bound arguments/stack/fields and never retain remote handles or nested values", async () => {
  const h = harness();
  await h.start();
  h.log("", {
    args: Array.from({ length: 100 }, () => ({
      type: "object", objectId: "secret-handle", value: { private: "nested-secret" },
      description: "😀".repeat(5000), preview: { properties: [{ value: "preview-secret" }] },
    })),
    stackTrace: {
      callFrames: Array.from({ length: 50 }, () => ({ functionName: "fn", url: "x".repeat(5000) })),
      parent: { callFrames: [{ functionName: "hidden-parent" }] },
    },
  });
  h.emit("Runtime.exceptionThrown", {
    timestamp: h.now, exceptionDetails: {
      text: "Uncaught", exception: { type: "object", description: "Error: broken", objectId: "handle" },
      url: "https://example.test/", lineNumber: 2,
    },
  });
  h.emit("Log.entryAdded", { entry: { timestamp: h.now, level: "warning", text: "browser log" } });
  const read = await h.read();
  const consoleEvent = read.events[0];
  assert.equal(consoleEvent.args.length, 32);
  assert.equal(consoleEvent.stack.length, 8);
  assert.equal(Buffer.byteLength(consoleEvent.args[0].description), 4096);
  assert.equal(consoleEvent.truncated, true);
  assert.equal(read.events[1].exception.description, "Error: broken");
  assert.equal(read.events[2].text, "browser log");
  assert.ok(!/secret-handle|nested-secret|preview-secret|hidden-parent|objectId/.test(JSON.stringify(read)));
  await h.stop();
});

test("cached Runtime/Log startup events and old timestamps are excluded, with explicit counters", async () => {
  const h = harness();
  h.sendHook = (_tabId, method) => {
    if (method === "Runtime.enable") h.log("startup");
    if (method === "Log.enable") h.emit("Log.entryAdded", { entry: { text: "cached", timestamp: 1 } });
  };
  await h.start();
  h.log("replayed", { timestamp: 9999 });
  h.emit("Runtime.exceptionThrown", { timestamp: 9999, exceptionDetails: { text: "cached exception" } });
  h.emit("Log.entryAdded", { entry: { timestamp: 9999, text: "cached log" } });
  h.log("live");
  const read = await h.read();
  assert.equal(read.events.length, 1);
  assert.equal(read.counters.ignoredStartupEvents, 2);
  assert.equal(read.counters.ignoredReplayEvents, 3);
  await h.stop();
});

test("network privacy defaults omit headers/postData/body and never retrieve response bodies", async () => {
  const h = harness();
  const start = await h.start("network");
  assert.deepEqual(plain(start.options), {
    maxEvents: 500, maxBytes: 1048576, includeHeaders: false, includeBodies: false,
  });
  assert.deepEqual(h.calls.find((call) => call.method === "Network.enable").params, {
    maxTotalBufferSize: 1024, maxResourceBufferSize: 1024, maxPostDataSize: 0,
  });
  assert.equal(start.limits.maxNetworkBufferBytes, 1024);
  assert.equal(start.limits.maxNetworkResourceBufferBytes, 1024);
  h.request("one", { request: {
    url: "https://example.test/", method: "POST", headers: { Authorization: "private-token" },
    postData: "private-request", postDataEntries: [{ bytes: "secret-bytes" }],
  } });
  h.response("one", { response: {
    status: 201, headers: { "Set-Cookie": "private-cookie" },
    requestHeaders: { Cookie: "private-request-cookie" }, securityDetails: { anything: "secret-details" },
  } });
  h.finish("one");
  await flush();
  const read = await h.read("network");
  const serialized = JSON.stringify(read.events);
  assert.ok(!/headers|postData|private-|secret-|body/i.test(serialized));
  assert.equal(h.bodyCalls().length, 0);
  assert.equal(read.events[1].matched, true);
  assert.equal(read.events[0].requestSequence, read.events[2].requestSequence);
  assert.equal(read.events[1].cdpTimestampUnit, "monotonicSeconds");
  await h.stop("network");
});

test("Network.enable buffer ceilings track opt-in body/ring limits and expose eviction limitations", async () => {
  for (const [maxBytes, maxBodyBytes] of [[1024, 65536], [1048576, 65536], [8388608, 1048576]]) {
    const h = harness();
    const started = await h.start("network", { includeBodies: true, maxBytes, maxBodyBytes });
    assert.deepEqual(h.calls.find((call) => call.method === "Network.enable").params, {
      maxTotalBufferSize: maxBytes,
      maxResourceBufferSize: Math.min(maxBytes, maxBodyBytes),
      maxPostDataSize: 0,
    });
    assert.equal(started.limits.maxNetworkBufferBytes, maxBytes);
    assert.equal(started.limits.maxNetworkResourceBufferBytes, Math.min(maxBytes, maxBodyBytes));
    assert.match(started.semantics.networkBuffers, /unavailable rather than truncated/);
    assert.match(started.semantics.privacy, /transient CDP payloads/);
    await h.stop("network");
  }
});

test("headers require independent opt-in and have entry, byte, and field bounds", async () => {
  const h = harness();
  await h.start("network", { includeHeaders: true, maxBytes: 100000 });
  const headers = Object.fromEntries(Array.from({ length: 100 }, (_, i) => [`x-${i}`, "😀".repeat(2000)]));
  h.request("one", { request: { url: "u", method: "POST", headers, postData: "never-save-me" } });
  h.response("one", { response: { status: 200, headers: { "Set-Cookie": "explicitly-opted-in" } } });
  h.finish("one");
  const read = await h.read("network");
  assert.ok(Object.keys(read.events[0].request.headers).length <= 64);
  assert.ok(Buffer.byteLength(JSON.stringify(read.events[0].request.headers)) <= 16384);
  assert.equal(read.events[0].truncated, true);
  assert.equal(read.events[1].response.headers["Set-Cookie"], "explicitly-opted-in");
  assert.ok(!JSON.stringify(read.events).includes("never-save-me"));
  assert.equal(h.bodyCalls().length, 0);
  await h.stop("network");
});

test("request IDs match interleaved requests, redirect hops, and failures without mixing metadata", async () => {
  const h = harness();
  await h.start("network", { includeHeaders: true });
  h.request("a"); h.request("b");
  h.request("a", {
    request: { url: "https://example.test/redirected", method: "GET" },
    redirectResponse: { url: "https://example.test/a", status: 302, headers: { Location: "/redirected" } },
  });
  h.response("b"); h.finish("b");
  h.response("a");
  h.emit("Network.loadingFailed", { requestId: "a", timestamp: 14, errorText: "net::ERR_FAILED" });
  const read = await h.read("network");
  const [a, b, redirect, nextA, responseB, finishedB, responseA, failedA] = read.events;
  assert.equal(redirect.type, "Network.redirectResponse");
  assert.equal(redirect.requestSequence, a.requestSequence);
  assert.equal(redirect.response.status, 302);
  assert.equal(nextA.hop, 1);
  assert.equal(nextA.redirectFromSequence, a.requestSequence);
  assert.notEqual(nextA.requestSequence, a.requestSequence);
  assert.equal(responseA.requestSequence, nextA.requestSequence);
  assert.equal(failedA.requestSequence, nextA.requestSequence);
  assert.equal(responseB.requestSequence, b.requestSequence);
  assert.equal(finishedB.requestSequence, b.requestSequence);
  assert.equal(read.trackedRequests, 0);
  assert.equal(read.trackedRequestBytes, 0);
  await h.stop("network");
});

test("request tracking has independent count/byte caps and oversized IDs are rejected, not truncated", async () => {
  const h = harness();
  await h.start("network", { maxEvents: 2, maxBytes: 1024, includeBodies: true });
  for (let i = 0; i < 20; i++) h.request(`${i}-${"x".repeat(500)}`);
  const read = await h.read("network");
  assert.ok(read.trackedRequests <= 2);
  assert.ok(read.trackedRequestBytes <= 1024);
  assert.ok(read.counters.droppedRequests > 0);
  h.request("x".repeat(1025));
  h.request("漢".repeat(400));
  h.response("unknown");
  h.finish("unknown");
  const ended = await h.read("network");
  assert.equal(ended.counters.invalidRequestIds, 2);
  assert.equal(ended.events.at(-1).matched, false);
  assert.equal(ended.events.at(-1).requestSequence, null);
  assert.match(ended.events.at(-1).bodySkipped, /not tracked/);
  assert.equal(h.bodyCalls().length, 0);
  await h.stop("network");
});

test("an oversized retained body is dropped by the serialized event-byte budget", async () => {
  const h = harness();
  h.sendHook = (_tab, method) => method === "Network.getResponseBody"
    ? { body: Buffer.alloc(2000, 42).toString("base64"), base64Encoded: true } : undefined;
  await h.start("network", { includeBodies: true, maxBodyBytes: 1500, maxBytes: 1024 });
  h.request("one"); h.response("one"); h.finish("one");
  await flush();
  const read = await h.read("network");
  assert.equal(read.counters.oversizedEvents, 1);
  assert.ok(read.buffer.bytes <= 1024);
  assert.ok(read.events.every((item) => item.type !== "Network.responseBody"));
  assert.equal(read.pendingBodies, 0);
  await h.stop("network");
});

test("body capture is opt-in, UTF-8 truncation does not split characters, and bodies need not imply headers", async () => {
  const h = harness();
  h.sendHook = (_tab, method) => method === "Network.getResponseBody"
    ? { body: "😀漢x", base64Encoded: false } : undefined;
  await h.start("network", { includeBodies: true, maxBodyBytes: 6 });
  h.request("one"); h.response("one"); h.finish("one");
  await flush();
  const read = await h.read("network");
  const body = read.events.at(-1);
  assert.equal(body.type, "Network.responseBody");
  assert.deepEqual(plain(body.body), {
    data: "😀", base64Encoded: false, originalBytes: 8, retainedBytes: 4, truncated: true,
  });
  assert.equal(body.requestSequence, read.events[0].requestSequence);
  assert.equal(read.pendingBodies, 0);
  assert.ok(!JSON.stringify(read.events).includes("headers"));
  await h.stop("network");
});

test("base64 bodies are truncated by decoded bytes and returned as independently valid base64", async () => {
  for (const limit of [1, 2, 3, 4, 8, 64]) {
    const h = harness();
    const input = Buffer.from("😀漢\0binary");
    h.sendHook = (_tab, method) => method === "Network.getResponseBody"
      ? { body: input.toString("base64"), base64Encoded: true } : undefined;
    await h.start("network", { includeBodies: true, maxBodyBytes: limit });
    h.request("one"); h.response("one"); h.finish("one");
    await flush();
    const body = (await h.read("network")).events.at(-1).body;
    const expected = input.subarray(0, limit);
    assert.equal(body.data, expected.toString("base64"));
    assert.equal(body.retainedBytes, expected.length);
    assert.equal(body.originalBytes, input.length);
    assert.equal(body.truncated, input.length > limit);
    await h.stop("network");
  }
});

test("body errors and malformed responses are observable without unhandled rejection or retained queues", async () => {
  for (const response of [
    new Error("body evicted"), { body: "not base64!", base64Encoded: true }, {}, { body: "x" },
  ]) {
    const h = harness();
    h.sendHook = (_tab, method) => {
      if (method !== "Network.getResponseBody") return;
      if (response instanceof Error) throw response;
      return response;
    };
    await h.start("network", { includeBodies: true });
    h.request("one"); h.response("one"); h.finish("one");
    await flush();
    const read = await h.read("network");
    assert.equal(read.counters.bodyErrors, 1);
    assert.equal(typeof read.events.at(-1).error, "string");
    assert.equal(read.lastError.message, read.events.at(-1).error);
    assert.equal(read.pendingBodies, 0);
    assert.equal(read.workerPendingBodyCommands, 0);
    await h.stop("network");
  }
});

test("body work has a global four-command cap, no queue, and remains bounded over navigation and restart", async () => {
  const h = harness();
  const gates = [];
  h.sendHook = (_tab, method) => {
    if (method === "Network.getResponseBody") {
      const gate = deferred(); gates.push(gate); return gate.promise;
    }
  };
  await h.start("network", { includeBodies: true });
  for (let i = 0; i < 20; i++) { h.request(String(i)); h.response(String(i)); h.finish(String(i)); }
  let read = await h.read("network");
  assert.equal(h.bodyCalls().length, 4);
  assert.equal(read.pendingBodies, 4);
  assert.equal(read.counters.skippedBodies, 16);
  assert.equal(read.limits.maxQueuedBodies, 0);
  h.navigate();
  read = await h.read("network");
  assert.equal(read.pendingBodies, 0);
  assert.equal(read.workerPendingBodyCommands, 4);
  assert.equal(read.counters.discardedBodies, 4);
  await h.stop("network");
  await h.start("network", { includeBodies: true });
  await h.start("network", { tabId: 8, includeBodies: true });
  for (const tabId of [7, 8]) {
    h.request("new", {}, tabId); h.response("new", {}, tabId); h.finish("new", tabId);
  }
  assert.equal(h.bodyCalls().length, 4, "stopped sessions cannot spawn more unsettled commands");
  assert.equal((await h.read("network")).counters.skippedBodies, 1);
  for (const gate of gates) gate.resolve({ body: "stale-private-body", base64Encoded: false });
  await flush();
  for (const tabId of [7, 8]) {
    const current = await h.read("network", { tabId });
    assert.equal(current.workerPendingBodyCommands, 0);
    assert.ok(!JSON.stringify(current).includes("stale-private-body"));
  }
  h.request("fresh"); h.response("fresh"); h.finish("fresh");
  assert.equal(h.bodyCalls().length, 5);
  gates[4].resolve({ body: "fresh-body", base64Encoded: false });
  await flush();
  assert.equal((await h.read("network")).events.at(-1).body.data, "fresh-body");
  await h.stop("network");
  await h.stop("network", { tabId: 8 });
});

test("stop returns buffered snapshot, discards late body rejection, and does not await body completion", async () => {
  const h = harness(), gate = deferred();
  h.sendHook = (_tab, method) => method === "Network.getResponseBody" ? gate.promise : undefined;
  await h.start("network", { includeBodies: true });
  h.request("one"); h.response("one"); h.finish("one");
  const stopped = await h.stop("network");
  assert.equal(stopped.events.length, 3);
  assert.equal(stopped.counters.discardedBodies, 1);
  assert.equal(stopped.pendingBodies, 0);
  assert.equal(stopped.workerPendingBodyCommands, 1);
  await h.start("network", { includeBodies: true });
  gate.reject(new Error("late body error"));
  await flush();
  const current = await h.read("network");
  assert.equal(current.events.length, 0);
  assert.equal(current.counters.bodyErrors, 0);
  assert.equal(current.workerPendingBodyCommands, 0);
  await h.stop("network");
});

test("same-ID reuse cancels stale body matching even within one generation", async () => {
  const h = harness(), gate = deferred();
  h.sendHook = (_tab, method) => method === "Network.getResponseBody" ? gate.promise : undefined;
  await h.start("network", { includeBodies: true });
  h.request("same"); h.response("same"); h.finish("same");
  h.request("same");
  gate.resolve({ body: "wrong-request", base64Encoded: false });
  await flush();
  const read = await h.read("network");
  assert.ok(!JSON.stringify(read).includes("wrong-request"));
  assert.equal(read.counters.discardedBodies, 1);
  assert.notEqual(read.events[0].requestSequence, read.events.at(-1).requestSequence);
  await h.stop("network");
});

test("navigation resets generations on same-URL reload, not subframes, child sessions or tab loading hints", async () => {
  const h = harness();
  await h.start();
  await h.start("network");
  h.log("previous document"); h.request("old");
  h.emit("Page.frameNavigated", { frame: { id: "child", parentId: "main-7", loaderId: "child-new" } });
  h.emit("Page.frameNavigated", { frame: { id: "main-7", loaderId: "other" } }, 8);
  h.emit("Page.frameNavigated", { frame: { id: "child", loaderId: "child-new" } }, 7, { sessionId: "child-session" });
  h.onUpdated.emit(7, { status: "loading" });
  h.emit("Page.navigatedWithinDocument", { frameId: "main-7", url: "https://example.test/#hash" });
  assert.equal((await h.read()).generation, 0);
  assert.equal((await h.read()).events.length, 1);
  h.navigate("initial");
  assert.equal((await h.read()).generation, 0, "duplicate current-loader frame event is not a new navigation");
  h.navigate("reload-same-url");
  for (const kind of ["console", "network"]) {
    const read = await h.read(kind);
    assert.equal(read.generation, 1);
    assert.equal(read.events.length, 0);
    assert.equal(read.trackedRequests, 0);
    assert.equal(read.counters.navigationClearedEvents, 1);
    assert.equal(read.frame.loaderId, "reload-same-url");
  }
  h.log("stale replay", { timestamp: 10000 });
  h.log("new document");
  assert.equal((await h.read()).events[0].generation, 1);
  h.navigate("third-document");
  assert.equal((await h.read()).generation, 2);
  assert.equal(h.leases.size, 2, "navigation does not end observation");
  await h.stop();
  await h.stop("network");
});

test("pending navigation body work is discarded while the new generation remains usable", async () => {
  const h = harness(), gate = deferred();
  h.sendHook = (_tab, method) => method === "Network.getResponseBody" ? gate.promise : undefined;
  await h.start("network", { includeBodies: true });
  h.request("one"); h.response("one"); h.finish("one");
  h.navigate();
  h.request("two");
  gate.resolve({ body: "old-navigation-secret", base64Encoded: false });
  await flush();
  const read = await h.read("network");
  assert.equal(read.generation, 1);
  assert.equal(read.events.length, 1);
  assert.equal(read.events[0].requestId, "two");
  assert.ok(!JSON.stringify(read).includes("old-navigation-secret"));
  await h.stop("network");
});

test("read(clear) clears only buffered events, preserving in-flight network matching", async () => {
  const h = harness();
  await h.start("network");
  h.request("one");
  assert.equal((await h.read("network", { clear: true })).events.length, 1);
  h.response("one"); h.finish("one");
  const read = await h.read("network");
  assert.equal(read.events.length, 2);
  assert.ok(read.events.every((item) => item.matched));
  await h.stop("network");
});

test("sessions and root events remain isolated across kinds/tabs; child detach is ignored", async () => {
  const h = harness();
  await Promise.all([h.start(), h.start("network"), h.start("console", { tabId: 8 })]);
  h.log("seven"); h.log("eight", {}, 8); h.request("request");
  h.emit("Runtime.consoleAPICalled", { args: [{ type: "string", value: "child" }] }, 7, { sessionId: "child" });
  h.onDetach.emit({ tabId: 7, sessionId: "child" }, "child closed");
  assert.equal((await h.read()).events.length, 1);
  assert.equal((await h.read("console", { tabId: 8 })).events[0].args[0].value, "eight");
  assert.equal((await h.read("network")).events.length, 1);
  await h.stop();
  h.request("still-active");
  assert.equal((await h.read("network")).events.length, 2);
  assert.equal(h.leases.size, 2);
  await h.stop("network");
  await h.stop("console", { tabId: 8 });
});

test("duplicate start, start/stop and stop/start races fail explicitly without a second acquisition", async () => {
  const h = harness(), acquire = deferred(), release = deferred();
  h.acquireHook = () => acquire.promise;
  const starting = h.start();
  await assert.rejects(h.start(), /already exists/);
  await assert.rejects(h.stop(), /starting/);
  await assert.rejects(h.read(), /starting/);
  acquire.resolve();
  await starting;
  await assert.rejects(h.start(), /already exists/);
  assert.equal(h.acquisitions.length, 1);
  h.releaseHook = () => release.promise;
  const stopping = h.stop();
  await assert.rejects(h.start(), /already exists/);
  await assert.rejects(h.stop(), /stopping/);
  await assert.rejects(h.read(), /stopping/);
  release.resolve();
  await stopping;
  await h.start();
  await h.stop();
  assert.equal(h.acquisitions.length, 2);
  assert.equal(h.releases.length, 2);
});

test("acquire/domain/identity failures clean up and never silently report success", async () => {
  for (const method of ["acquire", "Page.enable", "Page.getFrameTree", "Runtime.enable", "Log.enable", "Network.enable"]) {
    const h = harness();
    const kind = method === "Network.enable" ? "network" : "console";
    if (method === "acquire") h.acquireHook = () => { throw new Error("acquire unsupported"); };
    else h.sendHook = (_tab, called) => { if (called === method) throw new Error(`${method} unsupported`); };
    await assert.rejects(h.start(kind), /unsupported/);
    assert.equal(h.releases.length, method === "acquire" ? 0 : 1);
    assert.equal(h.leases.size, 0);
    await assert.rejects(h.read(kind), /No active/);
    h.acquireHook = null; h.sendHook = null;
    await h.start(kind);
    await h.stop(kind);
  }
  const h = harness();
  h.sendHook = (_tab, method) => method === "Page.getFrameTree" ? { frameTree: {} } : undefined;
  await assert.rejects(h.start(), /identity is unavailable/);
  assert.equal(h.leases.size, 0);
});

test("cleanup errors reject stop and combine with startup errors instead of disappearing", async () => {
  const h = harness();
  h.releaseHook = () => { throw new Error("release failure"); };
  await h.start();
  await assert.rejects(h.stop(), /cleanup failed: release failure/);
  await assert.rejects(h.read(), /No active/);
  h.sendHook = (_tab, method) => { if (method === "Log.enable") throw new Error("original enable failure"); };
  await assert.rejects(h.start(), /original enable failure; observe cleanup failed: release failure/);
});

test("detach and tab removal erase sessions, release leases and expose termination warnings", async () => {
  for (const cause of ["detach", "removed"]) {
    const h = harness();
    await h.start(); await h.start("network");
    h.log("private"); h.request("private");
    if (cause === "detach") h.onDetach.emit({ tabId: 7 }, "canceled_by_user");
    else h.onRemoved.emit(7);
    await flush();
    assert.equal(h.leases.size, 0);
    await assert.rejects(h.read(), /No active/);
    await assert.rejects(h.stop("network"), /No active/);
    assert.equal(h.warnings.length, 2);
    assert.match(h.warnings[0], /session ended/);
    await h.start();
    assert.equal((await h.read()).events.length, 0);
    await h.stop();
  }
});

test("detach while acquiring or enabling fails start and cleans the late lease exactly once", async () => {
  for (const phase of ["acquire", "enable"]) {
    const h = harness(), gate = deferred();
    if (phase === "acquire") h.acquireHook = () => gate.promise;
    else h.sendHook = (_tab, method) => method === "Runtime.enable" ? gate.promise : undefined;
    const starting = h.start();
    const failure = assert.rejects(starting, /session ended: detached/);
    await flush();
    h.onDetach.emit({ tabId: 7 }, "detached");
    await assert.rejects(h.start(), /already exists/);
    gate.resolve({});
    await failure;
    assert.equal(h.releases.length, 1);
    assert.equal(h.leases.size, 0);
  }
});

test("event-processing failures are observable and auto-clean; asynchronous cleanup failures are logged", async () => {
  const h = harness();
  await h.start();
  h.releaseHook = () => { throw new Error("cannot release"); };
  h.emit("Runtime.consoleAPICalled", {
    timestamp: h.now,
    args: [{ get type() { throw new Error("bad payload"); } }],
  });
  await flush();
  await assert.rejects(h.read(), /No active/);
  assert.match(h.warnings[0], /event processing failed: bad payload/);
  assert.match(h.warnings[1], /cleanup failed: cannot release/);
});

test("oversized frame identities fail rather than creating truncated identity collisions", async () => {
  const h = harness();
  h.sendHook = (_tab, method) => method === "Page.getFrameTree" ? {
    frameTree: { frame: { id: "main-7", loaderId: "x".repeat(1025) } },
  } : undefined;
  await assert.rejects(h.start(), /loaderId.*exceeds/);
  assert.equal(h.leases.size, 0);
  h.sendHook = null;
  await h.start();
  h.navigate("x".repeat(1025));
  await flush();
  await assert.rejects(h.read(), /No active/);
  assert.equal(h.leases.size, 0);
  assert.match(h.warnings[0], /loaderId.*exceeds/);
});

test("real shared CDP leases retain one attachment for both kinds without disabling shared domains", async () => {
  const h = harness(true);
  await Promise.all([h.start(), h.start("network")]);
  assert.equal(h.acquisitions.length, 1);
  await h.advance(1000);
  assert.ok(h.attached.has(7));
  await h.stop();
  await h.advance(1000);
  assert.ok(h.attached.has(7), "network lease keeps the shared session attached");
  h.request("one");
  assert.equal((await h.read("network")).events.length, 1);
  await h.stop("network");
  await h.advance(250);
  assert.equal(h.attached.size, 0);
  assert.ok(h.calls.every((call) => !call.method.endsWith(".disable")));
});

test("real CDP detach invalidation cannot release or contaminate a replacement observer", async () => {
  const h = harness(true), gate = deferred();
  h.sendHook = (_tab, method) => method === "Network.getResponseBody" ? gate.promise : undefined;
  await h.start("network", { includeBodies: true });
  h.request("one"); h.response("one"); h.finish("one");
  await flush();
  h.attached.delete(7);
  h.onDetach.emit({ tabId: 7 }, "external detach");
  await flush();
  const starting = h.start("network", { includeBodies: true });
  gate.resolve({ body: "old-secret", base64Encoded: false });
  await starting;
  await flush();
  const read = await h.read("network");
  assert.equal(read.events.length, 0);
  assert.equal(read.workerPendingBodyCommands, 0);
  assert.equal(h.acquisitions.length, 2);
  await h.stop("network");
  await h.advance(250);
  assert.equal(h.attached.size, 0);
});

test("worker reset has no persisted observations or implicit restart", async () => {
  const old = harness();
  await old.start(); old.log("memory-only");
  const restarted = harness();
  await assert.rejects(restarted.read(), /No active/);
  await restarted.start();
  assert.equal((await restarted.read()).events.length, 0);
  await old.stop();
  await restarted.stop();
});
