// Classic-worker AX perception. Child-frame documents (including same-origin
// frames) are deliberately excluded; the iframe element itself can be present.
// DOM refs use renderer backend IDs, not session-local AX IDs. Virtual AX nodes
// use paths anchored to DOM refs: stable for unchanged AX structure, not handles
// to independently addressable DOM elements. No remote object survives a call.

const AX_TEXT_SEMANTICS = "Whitespace-normalized AX value plus exposed descendant "
  + "StaticText names (InlineTextBox names only when not already represented by "
  + "StaticText), in tree order. Accessible labels alone are not text. "
  + "Matching is literal, case-insensitive, and not natural-language interpretation.";
const AX_ACTION_ROLES = new Set([
  "button", "checkbox", "combobox", "link", "listbox", "menuitem",
  "menuitemcheckbox", "menuitemradio", "option", "radio", "scrollbar",
  "searchbox", "slider", "spinbutton", "switch", "tab", "textbox", "treeitem",
]);

function axArguments(a, defaultLimit) {
  if (!a || typeof a !== "object" || Array.isArray(a)) {
    throw new Error("AX arguments must be an object");
  }
  if (!Number.isInteger(a.tabId) || a.tabId < 0) {
    throw new Error("tabId must be a nonnegative integer");
  }
  const limit = a.limit === undefined ? defaultLimit : a.limit;
  if (!Number.isInteger(limit) || limit < 1 || limit > 5000) {
    throw new Error("limit must be an integer from 1 to 5000");
  }
  return { tabId: a.tabId, limit };
}

function axBoolean(a, name) {
  if (a[name] !== undefined && typeof a[name] !== "boolean") {
    throw new Error(`${name} must be a boolean`);
  }
  return a[name] === true;
}

function axScalar(value) {
  const v = value?.value;
  return ["string", "number", "boolean"].includes(typeof v) ? v : null;
}

function axNormalize(value) {
  return String(value ?? "").replace(/\s+/g, " ").trim();
}

async function axDocument(tabId) {
  const results = await chrome.scripting.executeScript({
    target: { tabId, frameIds: [0] },
    world: "ISOLATED",
    func: () => location.href,
  });
  const result = results?.[0];
  if (results?.length !== 1 || result?.frameId !== 0
      || typeof result.documentId !== "string" || !result.documentId
      || typeof result.result !== "string") {
    throw new Error("AX requires a main-frame scripting documentId (modern Chrome)");
  }
  return { id: result.documentId, url: result.result };
}

function axBuildTree(rawNodes, rootBackendId, frameId, prefix, includeIgnored) {
  const byId = new Map(rawNodes.map((node) => [node.nodeId, node]));
  const root = rawNodes.find((node) =>
    node.backendDOMNodeId === rootBackendId
    && (!node.frameId || node.frameId === frameId)
    && node.role?.value === "RootWebArea");
  if (!root) throw new Error("AX document changed or main-frame root is unavailable; retry");

  const entries = [];
  const visited = new Set();
  const stack = [{ raw: root, parent: null, index: 0 }];
  while (stack.length) {
    const { raw, parent, index } = stack.pop();
    if (visited.has(raw.nodeId)) continue;
    visited.add(raw.nodeId);
    if (raw.frameId && raw.frameId !== frameId) continue;
    if (raw !== root && raw.role?.value === "RootWebArea") continue;
    const backend = raw.backendDOMNodeId;
    const role = String(axScalar(raw.role) ?? "");
    const ref = backend
      ? `${prefix}:d:${backend}`
      : `${parent.ref}:v:${encodeURIComponent(role)}:${index}`;
    const states = Object.fromEntries((raw.properties || [])
      .map((property) => [property.name, axScalar(property.value)])
      .filter(([, value]) => value !== null));
    const documentNode = raw === root
      || ["rootwebarea", "webarea", "document"].includes(role.toLowerCase());
    const entry = {
      raw, parent, ref, children: [],
      result: {
        ref,
        refKind: backend ? "dom" : "virtual",
        role,
        name: String(axScalar(raw.name) ?? ""),
        value: axScalar(raw.value),
        states,
        ignored: raw.ignored === true,
        actionable: !documentNode && raw.ignored !== true && !!backend
          && (AX_ACTION_ROLES.has(role.toLowerCase()) || states.focusable === true
            || (states.editable !== undefined && states.editable !== false)),
        ...(backend ? { backendDOMNodeId: backend } : {}),
      },
    };
    if (parent) parent.children.push(entry);
    entries.push(entry);
    const childIds = raw.childIds || [];
    for (let i = childIds.length - 1; i >= 0; i--) {
      const child = byId.get(childIds[i]);
      if (child) stack.push({ raw: child, parent: entry, index: i });
    }
  }
  // Bottom-up text avoids recursion on deeply nested documents and does not
  // count an InlineTextBox again after its owning StaticText supplied the text.
  for (let i = entries.length - 1; i >= 0; i--) {
    const entry = entries[i];
    const node = entry.result;
    const isText = ["StaticText", "InlineTextBox"].includes(node.role);
    const ownText = !node.ignored && isText ? axNormalize(node.name) : "";
    const value = node.ignored ? "" : axNormalize(node.value);
    node.text = axNormalize([
      value,
      ownText || entry.children.map((child) => child.result.text).filter(Boolean).join(" "),
    ].filter(Boolean).join(" "));
  }
  const included = entries.filter((entry) => includeIgnored || !entry.result.ignored);
  for (const entry of entries) {
    entry.visibleParent = entry.parent
      ? ((includeIgnored || !entry.parent.result.ignored)
        ? entry.parent : entry.parent.visibleParent)
      : null;
    entry.result.parentRef = entry.visibleParent?.ref || null;
    entry.result.childRefs = [];
  }
  for (const entry of included) {
    entry.visibleParent?.result.childRefs.push(entry.ref);
  }
  return included.map((entry) => entry.result);
}

async function axResolveActions(nodes, send, objectGroup) {
  let next = 0;
  let failure;
  const actions = nodes.filter((node) => node.actionable);
  const worker = async () => {
    while (!failure && next < actions.length) {
      const node = actions[next++];
      try {
        const resolved = await send("DOM.resolveNode", {
          backendNodeId: node.backendDOMNodeId, objectGroup,
        });
        const objectId = resolved.object?.objectId;
        if (!objectId) throw new Error("AX actionable DOM node is no longer available; retry");
        const details = await send("Runtime.callFunctionOn", {
          objectId,
          returnByValue: true,
          functionDeclaration: `function () {
            return {
              connected: this.isConnected === true,
              mainDocument: this.ownerDocument === document && window === window.top,
              tagName: (this.localName || "").toLowerCase(),
              inputType: this.localName === "input" ? this.type : null
            };
          }`,
        });
        if (details.exceptionDetails || !details.result?.value?.connected
            || details.result.value.mainDocument !== true) {
          throw new Error("AX actionable node detached or document changed; retry");
        }
        const { tagName, inputType } = details.result.value;
        node.dom = { tagName, inputType };
      } catch (error) {
        failure ||= error;
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(actions.length, 8) }, worker));
  if (failure) throw failure;
}

async function axSnapshot(tabId, includeIgnored, select) {
  const lease = await cdpSessions.acquire(tabId);
  const objectGroup = `rappter-ax-${crypto.randomUUID()}`;
  let operationError;
  let navigation = false;
  let frameId;
  const updated = (id, info) => {
    if (id === tabId && info.status === "loading") navigation = true;
  };
  const navigated = (source, method, params) => {
    if (source.tabId !== tabId) return;
    if (method === "Page.frameNavigated" && !params.frame?.parentId) navigation = true;
    if (method === "Page.frameDetached" && params.frameId === frameId) navigation = true;
  };
  chrome.tabs.onUpdated.addListener(updated);
  chrome.debugger.onEvent.addListener(navigated);
  const send = (method, params = {}) => {
    if (navigation) throw new Error("Navigation during AX snapshot; retry");
    return cdpSessions.send(tabId, method, params, lease);
  };
  try {
    await send("Page.enable");
    const document = await axDocument(tabId);
    const frame = (await send("Page.getFrameTree")).frameTree?.frame;
    frameId = frame?.id;
    if (!frameId || !frame.loaderId) throw new Error("AX main-frame identity is unavailable");
    const domRoot = (await send("DOM.getDocument", { depth: 0 })).root;
    if (!domRoot?.backendNodeId) throw new Error("AX DOM document identity is unavailable");
    await send("Accessibility.enable");
    const tree = await send("Accessibility.getFullAXTree", { frameId });
    if (!Array.isArray(tree.nodes)) throw new Error("Invalid Accessibility.getFullAXTree response");
    const nodes = axBuildTree(
      tree.nodes, domRoot.backendNodeId, frameId,
      `ax:${tabId}:${document.id}`, includeIgnored,
    );
    const selection = select(nodes);
    await axResolveActions(selection.nodes, send, objectGroup);
    const finalRoot = (await send("DOM.getDocument", { depth: 0 })).root;
    const finalFrame = (await send("Page.getFrameTree")).frameTree?.frame;
    const finalDocument = await axDocument(tabId);
    if (navigation || finalDocument.id !== document.id
        || finalRoot?.backendNodeId !== domRoot.backendNodeId
        || finalFrame?.id !== frameId || finalFrame?.loaderId !== frame.loaderId) {
      throw new Error("Navigation or document change during AX snapshot; retry");
    }
    return {
      tabId,
      documentId: document.id,
      url: document.url,
      scope: {
        frameId, mainFrameOnly: true, childFramesIncluded: false,
        description: "Main document only; iframe elements may appear, their documents are excluded.",
      },
      referencePolicy: {
        documentScoped: true,
        stableAcrossDebuggerSessions: true,
        invalidatedBy: ["navigation to a different document", "reload, including same-URL reload"],
        dom: "Backend DOM node identity; valid only while that node belongs to this document.",
        virtual: "DOM-anchored AX structural paths, stable for unchanged structure; "
          + "text/layout changes can replace these non-actionable virtual references.",
        hierarchy: "parentRef/childRefs can refer to nodes outside a limited or filtered result.",
      },
      textSemantics: AX_TEXT_SEMANTICS,
      total: selection.total,
      returned: selection.nodes.length,
      truncated: selection.nodes.length < selection.total,
      nodes: selection.nodes,
    };
  } catch (error) {
    operationError = error;
    throw error;
  } finally {
    chrome.tabs.onUpdated.removeListener(updated);
    chrome.debugger.onEvent.removeListener(navigated);
    let cleanupError;
    try {
      await cdpSessions.send(tabId, "Runtime.releaseObjectGroup", { objectGroup }, lease);
    } catch (error) {
      const message = String(error?.message || error);
      const destroyed = /CDP requires an active lease|CDP session (?:ended|is no longer active)|execution context was destroyed|cannot find context with specified id|not attached|(?:target|session|tab) (?:was )?closed|no tab with id|no target with given id/i.test(message);
      if (destroyed || operationError) {
        console.warn(`AX remote-object cleanup ${destroyed ? "after context/session destruction" : "failed"}: ${message}`);
      }
      if (!destroyed) cleanupError = error;
    }
    try {
      await cdpSessions.release(tabId, lease);
    } catch (error) {
      if (operationError) console.warn(`AX session release failed: ${error?.message || error}`);
      cleanupError ||= error;
    }
    if (cleanupError && !operationError) {
      throw new Error(`AX cleanup failed: ${cleanupError?.message || cleanupError}`);
    }
  }
}

async function readPageAX(a) {
  const { tabId, limit } = axArguments(a, 1000);
  const includeIgnored = axBoolean(a, "includeIgnored");
  const result = await axSnapshot(tabId, includeIgnored, (nodes) => ({
    total: nodes.length, nodes: nodes.slice(0, limit),
  }));
  return { ...result, includeIgnored };
}

async function findElements(a) {
  const { tabId, limit } = axArguments(a, 40);
  const exact = axBoolean(a, "exact");
  const criteria = {};
  for (const key of ["role", "name", "text"]) {
    if (a[key] === undefined) continue;
    if (typeof a[key] !== "string") throw new Error(`${key} must be a string`);
    const value = axNormalize(a[key]);
    if (!value) throw new Error(`${key} must be a nonempty string`);
    criteria[key] = value;
  }
  if (!Object.keys(criteria).length) {
    throw new Error("find requires at least one nonempty role, name, or text criterion");
  }
  const result = await axSnapshot(tabId, false, (nodes) => {
    const matches = nodes.filter((node) => Object.entries(criteria).every(([key, value]) => {
      const actual = axNormalize(node[key]).toLowerCase();
      const wanted = value.toLowerCase();
      return key === "role" || exact ? actual === wanted : actual.includes(wanted);
    }));
    return { total: matches.length, nodes: matches.slice(0, limit) };
  });
  const { nodes, ...metadata } = result;
  return { ...metadata, criteria, exact, matches: nodes };
}
