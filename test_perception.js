#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const { webcrypto: crypto } = require("node:crypto");

const source = fs.readFileSync(path.join(__dirname, "extension/perception.js"), "utf8");
const plain = (value) => JSON.parse(JSON.stringify(value));
const axValue = (value) => ({ value });
const property = (name, value) => ({ name, value: axValue(value) });
function axNode(nodeId, role, name, backend, children = [], extra = {}) {
  return {
    nodeId, role: axValue(role), name: axValue(name), ignored: false,
    ...(backend ? { backendDOMNodeId: backend } : {}),
    childIds: children, ...extra,
  };
}
function tree() {
  return [
    axNode("1", "RootWebArea", "Example", 100, ["2", "5", "6", "8", "9", "10"], {
      frameId: "main",
      properties: [property("focusable", true), property("focused", true)],
    }),
    axNode("2", "button", "Send message", 101, ["3"], {
      properties: [property("focusable", true), property("disabled", false)],
    }),
    axNode("3", "StaticText", "Send now", 102, ["4"]),
    axNode("4", "InlineTextBox", "Send now", null),
    axNode("5", "textbox", "Message", 103, [], {
      value: axValue("Hello world"), properties: [property("editable", "plaintext")],
    }),
    axNode("6", "none", "", 104, ["7"], { ignored: true }),
    axNode("7", "heading", "Details", 105),
    axNode("8", "checkbox", "Subscribe", 106, [], {
      properties: [property("checked", "mixed")],
    }),
    axNode("9", "Iframe", "Embedded", 107, ["11"]),
    axNode("10", "slider", "Volume", 108, [], { value: axValue(0) }),
    axNode("11", "RootWebArea", "Foreign", 200, ["12"], { frameId: "child" }),
    axNode("12", "button", "Hidden child-frame button", 201),
    axNode("orphan", "button", "Orphan", 300),
  ];
}
function event() {
  const listeners = new Set();
  return {
    listeners,
    addListener: (fn) => listeners.add(fn),
    removeListener: (fn) => listeners.delete(fn),
    emit: (...args) => { for (const fn of listeners) fn(...args); },
  };
}

function harness(options = {}) {
  const state = {
    documentId: "document-a", url: "https://example.test/",
    backendRoot: 100, loaderId: "loader-a",
    nodes: tree(), calls: [], scripts: [], warnings: [],
    acquired: 0, released: 0, objectIds: new Set(), nextObject: 0,
    pageEnabled: false,
    ...options,
  };
  const updated = event();
  const navigated = event();
  const lease = {};
  const chrome = {
    tabs: { onUpdated: updated },
    debugger: { onEvent: navigated },
    scripting: {
      executeScript: async (args) => {
        state.scripts.push(args);
        await state.scriptHook?.(state, args);
        if (state.scriptError) throw new Error(state.scriptError);
        const url = vm.runInNewContext(`(${args.func})()`, {
          location: { href: state.url },
        });
        return [{
          frameId: 0, documentId: state.documentId, result: url,
          ...state.scriptResult,
        }];
      },
    },
  };
  const cdpSessions = {
    acquire: async (tabId) => {
      assert.equal(tabId, 7);
      state.acquired++;
      if (state.acquireError) throw new Error(state.acquireError);
      return lease;
    },
    release: async (tabId, receivedLease) => {
      assert.equal(tabId, 7);
      assert.equal(receivedLease, lease);
      state.released++;
      if (state.releaseError) throw new Error(state.releaseError);
    },
    send: async (tabId, method, params, receivedLease) => {
      assert.equal(tabId, 7);
      assert.equal(receivedLease, lease);
      state.calls.push({ method, params });
      const override = await state.commandHook?.(method, params, state);
      if (override !== undefined) return override;
      switch (method) {
        case "Page.enable":
          state.pageEnabled = true;
          return {};
        case "Page.getFrameTree":
          assert.equal(state.pageEnabled, true);
          return { frameTree: { frame: { id: "main", loaderId: state.loaderId } } };
        case "DOM.getDocument":
          assert.deepEqual(plain(params), { depth: 0 });
          return { root: { backendNodeId: state.backendRoot, nodeType: 9 } };
        case "Accessibility.enable":
          return {};
        case "Accessibility.getFullAXTree":
          assert.deepEqual(plain(params), { frameId: "main" });
          return { nodes: plain(state.nodes) };
        case "DOM.resolveNode": {
          assert.ok(params.objectGroup.startsWith("rappter-ax-"));
          const objectId = `object-${++state.nextObject}`;
          state.objectIds.add(objectId);
          return { object: { objectId } };
        }
        case "Runtime.callFunctionOn": {
          assert.ok(state.objectIds.has(params.objectId));
          assert.equal(params.returnByValue, true);
          const document = {};
          const window = {};
          window.top = state.childDocument ? {} : window;
          const node = {
            isConnected: state.connected !== false,
            ownerDocument: state.foreignDocument ? {} : document,
            localName: "input", type: "text",
          };
          const value = vm.runInNewContext(`(${params.functionDeclaration}).call(node)`, {
            node, document, window,
          });
          return { result: { value } };
        }
        case "Runtime.releaseObjectGroup":
          state.objectIds.clear();
          return {};
        default:
          throw new Error(`unexpected command: ${method}`);
      }
    },
  };
  const context = vm.createContext({
    chrome, cdpSessions, crypto,
    console: { warn: (message) => state.warnings.push(message) },
  });
  vm.runInContext(source, context, { filename: "perception.js" });
  return {
    state, context, updated, navigated,
    read: (args = {}) => context.readPageAX({ tabId: 7, ...args }),
    find: (args) => context.findElements({ tabId: 7, ...args }),
    assertClean: () => {
      assert.equal(state.acquired, state.released);
      assert.equal(state.objectIds.size, 0);
      assert.equal(updated.listeners.size, 0);
      assert.equal(navigated.listeners.size, 0);
    },
  };
}

test("classic globals, structured AX states, values, hierarchy, and bounded frames", async () => {
  const h = harness();
  assert.equal(typeof h.context.readPageAX, "function");
  assert.equal(typeof h.context.findElements, "function");
  const result = plain(await h.read());
  assert.equal(result.documentId, "document-a");
  assert.equal(result.returned, 9);
  assert.equal(result.total, 9);
  assert.equal(result.truncated, false);
  assert.equal(result.scope.mainFrameOnly, true);
  assert.equal(result.scope.childFramesIncluded, false);
  assert.equal(result.includeIgnored, false);
  assert.equal(h.state.calls[0].method, "Page.enable");
  assert.match(result.textSemantics, /StaticText/);
  const button = result.nodes.find((node) => node.role === "button");
  assert.equal(button.name, "Send message");
  assert.equal(button.value, null);
  assert.deepEqual(button.states, { focusable: true, disabled: false });
  assert.equal(button.text, "Send now");
  assert.deepEqual(button.dom, { tagName: "input", inputType: "text" });
  assert.equal(button.actionable, true);
  const heading = result.nodes.find((node) => node.role === "heading");
  assert.equal(heading.parentRef, result.nodes[0].ref);
  assert.ok(result.nodes[0].childRefs.includes(heading.ref));
  assert.equal(result.nodes.find((node) => node.role === "slider").value, 0);
  assert.equal(result.nodes.find((node) => node.role === "checkbox").states.checked, "mixed");
  assert.ok(!result.nodes.some((node) => /Foreign|child-frame|Orphan/.test(node.name)));
  assert.equal(result.nodes.find((node) => node.role === "Iframe").childRefs.length, 0);
  assert.equal(h.state.calls.filter((call) => call.method === "DOM.resolveNode").length, 4);
  for (const script of h.state.scripts) {
    assert.equal(script.world, "ISOLATED");
    assert.deepEqual(plain(script.target), { tabId: 7, frameIds: [0] });
  }
  h.assertClean();
});

test("ignored nodes are opt-in and their children retain usable hierarchy", async () => {
  const h = harness();
  const result = await h.read({ includeIgnored: true });
  assert.equal(result.returned, 10);
  const ignored = result.nodes.find((node) => node.ignored);
  const heading = result.nodes.find((node) => node.role === "heading");
  assert.equal(heading.parentRef, ignored.ref);
  assert.equal(ignored.actionable, false);
  assert.deepEqual(plain(ignored.childRefs), [heading.ref]);
  h.assertClean();
});

test("focusable document roles are not resolved as actionable DOM elements", async () => {
  const h = harness();
  h.state.nodes[0].childIds.push("document", "webarea", "ignored-document");
  h.state.nodes.push(
    axNode("document", "document", "Document", 109, [], {
      properties: [property("focusable", true)],
    }),
    axNode("webarea", "WebArea", "Web area", 110, [], {
      properties: [property("focusable", true)],
    }),
    axNode("ignored-document", "none", "", 111, [], {
      ignored: true, properties: [property("focusable", true)],
    }),
  );
  const result = await h.read({ includeIgnored: true });
  for (const backend of [100, 109, 110, 111]) {
    const node = result.nodes.find((item) => item.backendDOMNodeId === backend);
    assert.equal(node.actionable, false);
    assert.equal(node.dom, undefined);
  }
  assert.deepEqual(
    h.state.calls.filter((call) => call.method === "DOM.resolveNode")
      .map((call) => call.params.backendNodeId).sort(),
    [101, 103, 106, 108],
  );
  h.assertClean();
});

test("refs survive repeat snapshots, new AX IDs, detach/reattach, and worker reload", async () => {
  const h = harness();
  const first = plain(await h.read());
  const second = plain(await h.read());
  assert.deepEqual(first.nodes.map((node) => node.ref), second.nodes.map((node) => node.ref));
  // Session-local AX node IDs and response order change after reattachment.
  h.state.nodes = h.state.nodes.map((node) => ({
    ...node, nodeId: `new-${node.nodeId}`,
    childIds: node.childIds.map((id) => `new-${id}`),
  })).reverse();
  const third = plain(await h.read());
  assert.deepEqual(first.nodes.map((node) => node.ref), third.nodes.map((node) => node.ref));
  const otherWorker = harness({ nodes: h.state.nodes });
  assert.deepEqual(
    first.nodes.map((node) => node.ref),
    plain(await otherWorker.read()).nodes.map((node) => node.ref),
  );
  assert.equal(h.state.acquired, 3);
  h.assertClean();
  otherWorker.assertClean();
});

test("same-URL reload invalidates every ref even if backend IDs are reused", async () => {
  const h = harness();
  const first = await h.read();
  h.state.documentId = "document-b";
  h.state.loaderId = "loader-b";
  const second = await h.read();
  assert.equal(first.url, second.url);
  assert.notEqual(first.documentId, second.documentId);
  const previous = new Set(first.nodes.map((node) => node.ref));
  assert.ok(second.nodes.every((node) => !previous.has(node.ref)));
  assert.match(second.referencePolicy.invalidatedBy.join(" "), /same-URL reload/);
  h.assertClean();
});

test("find combines literal case-insensitive criteria with exact role and optional exact text/name", async () => {
  const h = harness();
  let result = await h.find({ role: "BUTTON", name: "message", text: "SEND" });
  assert.equal(result.total, 1);
  assert.equal(result.matches[0].name, "Send message");
  assert.equal(result.nodes, undefined);
  result = await h.find({ role: "but" });
  assert.equal(result.total, 0);
  result = await h.find({ name: "SEND MESSAGE", exact: true });
  assert.equal(result.total, 1);
  result = await h.find({ name: "send", exact: true });
  assert.equal(result.total, 0);
  result = await h.find({ role: "button", text: "send now", exact: true });
  assert.equal(result.total, 1);
  result = await h.find({ role: "button", text: "message" });
  assert.equal(result.total, 0, "accessible label is not treated as page text");
  result = await h.find({ role: "textbox", text: "HELLO   world", exact: true });
  assert.equal(result.total, 1);
  result = await h.find({ role: "slider", text: "0", exact: true });
  assert.equal(result.total, 1);
  result = await h.find({ text: "send", limit: 2 });
  assert.equal(result.total, 4);
  assert.equal(result.returned, 2);
  assert.equal(result.truncated, true);
  h.assertClean();
});

test("text uses InlineTextBox fallback without doubling StaticText", async () => {
  const h = harness();
  h.state.nodes.find((node) => node.nodeId === "3").name = axValue("");
  let result = await h.find({ role: "button", text: "send now", exact: true });
  assert.equal(result.total, 1);
  h.state.nodes.find((node) => node.nodeId === "3").name = axValue("Send now");
  result = await h.find({ role: "button", text: "send now send now" });
  assert.equal(result.total, 0);
  h.assertClean();
});

test("limits are applied after filtering and resolve only returned actionable nodes", async () => {
  const h = harness();
  const short = await h.read({ limit: 1 });
  assert.equal(short.nodes.length, 1);
  assert.equal(short.total, 9);
  assert.equal(short.truncated, true);
  assert.equal(h.state.calls.filter((call) => call.method === "DOM.resolveNode").length, 0);
  const result = await h.find({ role: "textbox", limit: 1 });
  assert.equal(result.total, 1);
  assert.equal(h.state.calls.filter((call) => call.method === "DOM.resolveNode").length, 1);
  h.assertClean();
});

test("default and maximum limits are enforced independently for read/find", async () => {
  const h = harness();
  const children = Array.from({ length: 1010 }, (_, i) => String(i + 2));
  h.state.nodes = [
    axNode("1", "RootWebArea", "All", 100, children, { frameId: "main" }),
    ...children.map((id, i) => axNode(id, "heading", `Heading ${i}`, i + 101)),
  ];
  assert.equal((await h.read()).returned, 1000);
  assert.equal((await h.find({ role: "heading" })).returned, 40);
  assert.equal((await h.read({ limit: 5000 })).returned, 1011);
  assert.equal((await h.find({ role: "heading", limit: 5000 })).returned, 1010);
  h.assertClean();
});

test("validation rejects malformed arguments before acquiring a debugger", async () => {
  const h = harness();
  for (const args of [null, [], {}, { tabId: -1 }, { tabId: "7" }, { tabId: 1.5 }]) {
    await assert.rejects(h.context.readPageAX(args), /arguments|tabId/);
  }
  for (const limit of [0, -1, 5001, 1.5, "2", null]) {
    await assert.rejects(h.read({ limit }), /limit/);
    await assert.rejects(h.find({ role: "button", limit }), /limit/);
  }
  for (const includeIgnored of [1, "false", null]) {
    await assert.rejects(h.read({ includeIgnored }), /includeIgnored/);
  }
  await assert.rejects(h.find({}), /at least one nonempty/);
  for (const args of [
    { name: "" }, { role: " ", text: "\n" }, { role: "button", name: "" },
    { role: "button", text: " \t\n" }, { role: "", name: "Send" },
  ]) {
    await assert.rejects(h.find(args), /must be a nonempty string/);
  }
  for (const key of ["role", "name", "text"]) {
    await assert.rejects(h.find({ [key]: 123 }), new RegExp(`${key} must be a string`));
  }
  await assert.rejects(h.find({ role: "button", exact: "false" }), /exact/);
  assert.equal(h.state.acquired, 0);
});

test("document identity, loader identity, and backend-root changes reject navigation races", async () => {
  for (const changed of ["documentId", "loaderId", "backendRoot"]) {
    const h = harness();
    h.state.commandHook = (method, _params, state) => {
      if (method === "Accessibility.getFullAXTree") {
        state[changed] = changed === "backendRoot" ? 999 : "changed";
      }
    };
    await assert.rejects(h.read(), /Navigation|document change/);
    h.assertClean();
  }
});

test("navigation events detect a move away and back even when final identities match", async () => {
  for (const eventKind of ["tabs", "cdp", "detach"]) {
    const h = harness();
    h.state.commandHook = (method) => {
      if (method === "Accessibility.getFullAXTree") {
        if (eventKind === "tabs") h.updated.emit(7, { status: "loading" });
        else if (h.state.pageEnabled && eventKind === "cdp") {
          h.navigated.emit({ tabId: 7 }, "Page.frameNavigated", { frame: { id: "main" } });
        } else if (h.state.pageEnabled) {
          h.navigated.emit({ tabId: 7 }, "Page.frameDetached", { frameId: "main" });
        }
      }
    };
    await assert.rejects(h.read(), /Navigation/);
    h.assertClean();
  }
});

test("foreign-tab and child-frame navigation do not invalidate main-document snapshots", async () => {
  const h = harness();
  h.state.commandHook = (method) => {
    if (method === "Accessibility.getFullAXTree") {
      h.updated.emit(8, { status: "loading" });
      h.navigated.emit({ tabId: 8 }, "Page.frameNavigated", { frame: { id: "other" } });
      h.navigated.emit({ tabId: 7 }, "Page.frameNavigated", { frame: { id: "child", parentId: "main" } });
    }
  };
  assert.equal((await h.read()).returned, 9);
  h.assertClean();
});

test("missing documentId and restricted scripting fail closed and release the lease", async () => {
  for (const options of [
    { scriptResult: { documentId: undefined } },
    { scriptResult: { frameId: 4 } },
    { scriptError: "Cannot access chrome:// URL" },
  ]) {
    const h = harness(options);
    await assert.rejects(h.read(), /documentId|Cannot access/);
    h.assertClean();
  }
});

test("CDP and actionable-node failures release all object handles and session leases", async () => {
  for (const method of ["Page.enable", "Accessibility.getFullAXTree", "DOM.resolveNode", "Runtime.callFunctionOn"]) {
    const h = harness();
    h.state.commandHook = (called) => {
      if (called === method) throw new Error(`failed ${method}`);
    };
    await assert.rejects(h.read(), /failed/);
    h.assertClean();
  }
  for (const options of [{ connected: false }, { foreignDocument: true }, { childDocument: true }]) {
    const h = harness(options);
    await assert.rejects(h.read(), /detached or document changed/);
    h.assertClean();
  }
  const h = harness({ commandHook: (method) =>
    method === "DOM.resolveNode" ? { object: {} } : undefined });
  await assert.rejects(h.read(), /no longer available/);
  h.assertClean();
});

test("malformed AX trees and mismatched roots fail closed", async () => {
  for (const nodes of [null, [], [axNode("1", "RootWebArea", "Wrong", 999)]]) {
    const h = harness({ nodes });
    await assert.rejects(h.read(), /Invalid Accessibility|document changed|root/);
    h.assertClean();
  }
});

test("acquisition errors propagate without releasing someone else's lease", async () => {
  const h = harness({ acquireError: "DevTools is already attached" });
  await assert.rejects(h.read(), /DevTools/);
  assert.equal(h.state.released, 0);
  assert.equal(h.state.calls.length, 0);
});

test("unexpected remote-object cleanup failure cannot silently succeed", async () => {
  const h = harness({
    commandHook: (method) => {
      if (method === "Runtime.releaseObjectGroup") throw new Error("cleanup transport failure");
    },
  });
  await assert.rejects(h.read(), /AX cleanup failed: cleanup transport failure/);
  assert.equal(h.state.released, 1);
  assert.equal(h.updated.listeners.size, 0);
  assert.equal(h.navigated.listeners.size, 0);
});

test("expected destroyed-session cleanup failures are explicitly logged", async () => {
  for (const message of [
    "CDP requires an active lease from acquire(tabId)",
    "CDP session ended: target_closed",
    "Execution context was destroyed",
    "Debugger is not attached to the tab with id: 7",
  ]) {
    const h = harness({
      commandHook: (method, _params, state) => {
        if (method === "Runtime.releaseObjectGroup") {
          state.objectIds.clear();
          throw new Error(message);
        }
      },
    });
    assert.equal((await h.read()).returned, 9);
    assert.equal(h.state.warnings.length, 1);
    assert.match(h.state.warnings[0], /cleanup after context\/session destruction/);
    assert.ok(h.state.warnings[0].includes(message));
    h.assertClean();
  }
});

test("operation errors are preserved when remote cleanup or lease release also fail", async () => {
  const h = harness({
    releaseError: "lease release failure",
    commandHook: (method) => {
      if (method === "Accessibility.getFullAXTree") throw new Error("original AX failure");
      if (method === "Runtime.releaseObjectGroup") throw new Error("object cleanup failure");
    },
  });
  await assert.rejects(h.read(), /^Error: original AX failure$/);
  assert.equal(h.state.warnings.length, 2);
  assert.match(h.state.warnings[0], /object cleanup failure/);
  assert.match(h.state.warnings[1], /lease release failure/);
  h.assertClean();
  const releaseOnly = harness({ releaseError: "lease release failure" });
  await assert.rejects(releaseOnly.read(), /AX cleanup failed: lease release failure/);
  releaseOnly.assertClean();
});
