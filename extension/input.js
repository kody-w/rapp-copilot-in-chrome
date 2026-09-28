// Native input uses viewport CSS pixels (coordinates/deltas bounded by 1e6)
// and at most 1 MiB of UTF-8 text. No DOM events, automatic scrolling, or
// synthetic fallback. Success means CDP dispatched input, not application success.
const INPUT_LIMITS = Object.freeze({ MAX_COORDINATE: 1000000, MAX_TEXT_BYTES: 1024 * 1024 });
const inputQueues = new Map();
const INPUT_MODIFIERS = Object.freeze({
  ctrl: { key: "Control", code: "ControlLeft", windowsVirtualKeyCode: 17, bit: 2 },
  alt: { key: "Alt", code: "AltLeft", windowsVirtualKeyCode: 18, bit: 1 },
  shift: { key: "Shift", code: "ShiftLeft", windowsVirtualKeyCode: 16, bit: 8 },
  cmd: { key: "Meta", code: "MetaLeft", windowsVirtualKeyCode: 91, bit: 4 },
});

function inputKey(key) {
  if (typeof key !== "string" || !key) throw new Error("input key must be a single character or named key");
  const named = {
    Enter: ["Enter", 13, "\r"], Tab: ["Tab", 9], Escape: ["Escape", 27],
    Backspace: ["Backspace", 8], Delete: ["Delete", 46], Insert: ["Insert", 45],
    ArrowLeft: ["ArrowLeft", 37], ArrowUp: ["ArrowUp", 38],
    ArrowRight: ["ArrowRight", 39], ArrowDown: ["ArrowDown", 40],
    Home: ["Home", 36], End: ["End", 35], PageUp: ["PageUp", 33], PageDown: ["PageDown", 34],
    Space: ["Space", 32, " "],
  };
  const modifier = Object.values(INPUT_MODIFIERS).find((item) => item.key === key);
  if (modifier) return { ...modifier };
  if (Object.hasOwn(named, key)) {
    const [code, windowsVirtualKeyCode, text] = named[key];
    return { key: key === "Space" ? " " : key, code, windowsVirtualKeyCode, text };
  }
  if (/^F(?:[1-9]|1\d|2[0-4])$/.test(key)) {
    return { key, code: key, windowsVirtualKeyCode: 111 + Number(key.slice(1)) };
  }
  if (Array.from(key).length !== 1 || /[\u0000-\u001f\u007f-\u009f]/u.test(key)
      || (key.length === 1 && /[\ud800-\udfff]/u.test(key))) {
    throw new Error("input key must be a printable Unicode character or supported named key");
  }
  const punctuation = {
    " ": ["Space", 32], ";": ["Semicolon", 186], ":": ["Semicolon", 186],
    "=": ["Equal", 187], "+": ["Equal", 187], ",": ["Comma", 188], "<": ["Comma", 188],
    "-": ["Minus", 189], "_": ["Minus", 189], ".": ["Period", 190], ">": ["Period", 190],
    "/": ["Slash", 191], "?": ["Slash", 191], "`": ["Backquote", 192], "~": ["Backquote", 192],
    "[": ["BracketLeft", 219], "{": ["BracketLeft", 219],
    "\\": ["Backslash", 220], "|": ["Backslash", 220],
    "]": ["BracketRight", 221], "}": ["BracketRight", 221],
    "'": ["Quote", 222], "\"": ["Quote", 222],
  };
  let code = "", windowsVirtualKeyCode = 0;
  if (/^[a-z]$/i.test(key)) {
    code = `Key${key.toUpperCase()}`;
    windowsVirtualKeyCode = key.toUpperCase().charCodeAt(0);
  } else if (/^[0-9]$/.test(key)) {
    code = `Digit${key}`;
    windowsVirtualKeyCode = key.charCodeAt(0);
  } else if ("!@#$%^&*()".includes(key)) {
    const digit = ("!@#$%^&*()".indexOf(key) + 1) % 10;
    code = `Digit${digit}`;
    windowsVirtualKeyCode = 48 + digit;
  } else if (Object.hasOwn(punctuation, key)) {
    [code, windowsVirtualKeyCode] = punctuation[key];
  }
  return { key, code, windowsVirtualKeyCode, text: key };
}

function inputArguments(a) {
  if (!a || typeof a !== "object" || Array.isArray(a)) throw new Error("input args must be an object");
  if (!Number.isSafeInteger(a.tabId) || a.tabId < 0) throw new Error("input tabId must be a nonnegative safe integer");
  if (!["click", "hover", "scroll", "type", "key"].includes(a.action)) {
    throw new Error("input action must be click, hover, scroll, type, or key");
  }
  const targetKeys = ["x", "y", "selector", "index", "ref"];
  const allowed = ["tabId", "action", ...targetKeys];
  if (a.action !== "type") allowed.push("modifiers");
  if (a.action === "click") allowed.push("button", "clickCount");
  if (a.action === "scroll") allowed.push("deltaX", "deltaY");
  if (a.action === "type") allowed.push("text");
  if (a.action === "key") allowed.push("key");
  for (const key of Object.keys(a)) {
    if (!allowed.includes(key)) throw new Error(`input ${key} is not supported for ${a.action}`);
  }
  const bounded = (value, name, signed = false) => {
    if (typeof value !== "number" || !Number.isFinite(value)
        || Math.abs(value) > INPUT_LIMITS.MAX_COORDINATE || (!signed && value < 0)) {
      throw new Error(`input ${name} must be finite CSS pixels ${signed ? "within +/-" : "from 0 to "}1000000`);
    }
    return value;
  };
  let target;
  const keys = targetKeys.filter((key) => Object.hasOwn(a, key));
  if (keys.length) {
    const t = a;
    if (keys.length === 2 && keys.includes("x") && keys.includes("y")) {
      target = { x: bounded(t.x, "x"), y: bounded(t.y, "y") };
    } else if (keys.includes("selector") && keys.every((key) => ["selector", "index"].includes(key))) {
      if (typeof t.selector !== "string" || !t.selector.trim()) throw new Error("input selector must be nonempty");
      const index = t.index === undefined ? 0 : t.index;
      if (!Number.isSafeInteger(index) || index < 0) throw new Error("input index must be a nonnegative safe integer");
      target = { selector: t.selector, index };
    } else if (keys.length === 1 && keys[0] === "ref") {
      const match = typeof t.ref === "string" && /^ax:(0|[1-9]\d*):([^:]+):d:([1-9]\d*)$/.exec(t.ref);
      if (!match || !Number.isSafeInteger(Number(match[1])) || !Number.isSafeInteger(Number(match[3]))) {
        throw new Error("input ref must be a document-scoped DOM ref, not a virtual AX ref");
      }
      if (Number(match[1]) !== a.tabId) throw new Error("input ref belongs to another tab");
      target = { ref: t.ref, documentId: match[2], backendNodeId: Number(match[3]) };
    } else {
      throw new Error("input requires exactly one flat target: x/y, selector/index, or ref");
    }
  }
  if (!target && ["click", "hover", "scroll"].includes(a.action)) {
    throw new Error(`input ${a.action} requires a target`);
  }
  const modifiers = a.modifiers === undefined ? [] : a.modifiers;
  if (!Array.isArray(modifiers) || Array.from(modifiers).some((name) =>
    typeof name !== "string" || !Object.hasOwn(INPUT_MODIFIERS, name))
      || new Set(modifiers).size !== modifiers.length) {
    throw new Error("input modifiers must be unique ctrl, cmd, shift, or alt strings");
  }
  // Copy validated data before queuing; callers cannot mutate a waiting action.
  const result = {
    tabId: a.tabId, action: a.action, target,
    modifiers: Object.keys(INPUT_MODIFIERS).filter((name) => modifiers.includes(name)),
  };
  if (a.action === "click") {
    result.button = a.button === undefined ? "left" : a.button;
    result.clickCount = a.clickCount === undefined ? 1 : a.clickCount;
    if (!["left", "right", "middle"].includes(result.button)) throw new Error("input button must be left, right, or middle");
    if (!Number.isInteger(result.clickCount) || result.clickCount < 1 || result.clickCount > 3) {
      throw new Error("input clickCount must be an integer from 1 to 3");
    }
  }
  if (a.action === "scroll") {
    result.deltaX = bounded(a.deltaX === undefined ? 0 : a.deltaX, "deltaX", true);
    result.deltaY = bounded(a.deltaY === undefined ? 0 : a.deltaY, "deltaY", true);
    if (!result.deltaX && !result.deltaY) throw new Error("input scroll requires a nonzero delta");
  }
  if (a.action === "type") {
    if (typeof a.text !== "string" || a.text.length > INPUT_LIMITS.MAX_TEXT_BYTES) {
      throw new Error("input text must be a string of at most 1 MiB UTF-8");
    }
    result.textBytes = new TextEncoder().encode(a.text).byteLength;
    if (result.textBytes > INPUT_LIMITS.MAX_TEXT_BYTES) throw new Error("input text exceeds 1 MiB UTF-8");
    result.text = a.text;
  }
  if (a.action === "key") {
    result.key = inputKey(a.key);
    if (result.modifiers.some((name) => INPUT_MODIFIERS[name].key === result.key.key)) {
      throw new Error("input key must not duplicate a held modifier");
    }
    if (result.modifiers.includes("shift") && Array.from(result.key.key).length === 1
        && result.key.text) {
      const plain = "`1234567890-=[]\\;',./";
      const shifted = "~!@#$%^&*()_+{}|:\"<>?";
      const original = result.key.text;
      const offset = plain.indexOf(original);
      const text = /^[a-z]$/.test(original) ? original.toUpperCase()
        : offset >= 0 ? shifted[offset] : original;
      result.key = { ...result.key, key: text, text };
    }
  }
  return result;
}

async function inputEditingCommands(a) {
  if (a.action !== "key" || a.modifiers.includes("ctrl")) return [];
  const primary = a.modifiers.filter((name) => name !== "shift");
  if (primary.length !== 1 || !["cmd", "alt"].includes(primary[0])) return [];
  const modifier = primary[0];
  const shifted = a.modifiers.includes("shift");
  const editing = shifted
    ? { KeyZ: "redo", KeyV: "pasteAndMatchStyle" }
    : { KeyA: "selectAll", KeyC: "copy", KeyX: "cut", KeyV: "paste", KeyZ: "undo" };
  const movement = modifier === "cmd" ? {
    ArrowLeft: "moveToLeftEndOfLine", ArrowRight: "moveToRightEndOfLine",
    ArrowUp: "moveToBeginningOfDocument", ArrowDown: "moveToEndOfDocument",
  } : { ArrowLeft: "moveWordLeft", ArrowRight: "moveWordRight" };
  const deletion = modifier === "cmd"
    ? { Backspace: "deleteToBeginningOfLine", Delete: "deleteToEndOfLine" }
    : { Backspace: "deleteWordBackward", Delete: "deleteWordForward" };
  let command = modifier === "cmd" ? editing[a.key.code] : undefined;
  if (movement[a.key.code]) {
    command = movement[a.key.code] + (shifted ? "AndModifySelection" : "");
  } else if (!shifted && deletion[a.key.code]) {
    command = deletion[a.key.code];
  }
  if (!command) return [];
  const platform = await chrome.runtime.getPlatformInfo();
  // On macOS CDP does not infer Cocoa editing selectors from Meta key events.
  // Only the explicit bindings above add commands; other shortcuts dispatch
  // key events only. Commands run on key-down, never via a DOM fallback.
  return platform.os === "mac" ? [command] : [];
}

// Serialized into the target's execution context; depends only on page globals.
function _inputGeometry(point, requireFocus) {
  if (this.nodeType !== 1 || !this.isConnected) throw new Error("input target is not a connected element");
  if (this.ownerDocument !== document || window !== window.top) throw new Error("input target is outside the main document");
  const parent = (node) => node.parentElement || node.getRootNode?.().host || null;
  const within = (ancestor, node) => {
    for (; node; node = parent(node)) if (node === ancestor) return true;
    return false;
  };
  const check = (element) => {
    const visibility = getComputedStyle(element).visibility;
    if (visibility === "hidden" || visibility === "collapse") throw new Error("input target is hidden");
    for (let node = element; node; node = parent(node)) {
      const style = getComputedStyle(node);
      if (style.display === "none" || Number(style.opacity) === 0 || style.contentVisibility === "hidden") {
        throw new Error("input target or ancestor is hidden");
      }
      if (node.inert || node.hasAttribute("inert")) throw new Error("input target or ancestor is inert");
      if ((node.getAttribute("aria-disabled") || "").trim().toLowerCase() === "true"
          || (node.matches(":disabled") && (node === element || node.localName !== "fieldset"))) {
        throw new Error("input target or ancestor is disabled");
      }
    }
  };
  check(this);
  if (!Array.from(this.getClientRects()).some((rect) => rect.width > 0 && rect.height > 0)) {
    throw new Error("input target has no visible layout box");
  }
  const rect = this.getBoundingClientRect();
  const width = Math.min(window.innerWidth, document.documentElement.clientWidth);
  const height = Math.min(window.innerHeight, document.documentElement.clientHeight);
  const left = Math.max(0, rect.left), right = Math.min(width, rect.right);
  const top = Math.max(0, rect.top), bottom = Math.min(height, rect.bottom);
  if (!(right > left && bottom > top)) throw new Error("input target is outside the viewport");
  const x = point ? point.x : (left + right) / 2;
  const y = point ? point.y : (top + bottom) / 2;
  if (!Number.isFinite(x) || !Number.isFinite(y) || x < 0 || y < 0
      || x >= width || y >= height || x > 1000000 || y > 1000000) {
    throw new Error("input point is outside the bounded viewport");
  }
  let hit = document.elementFromPoint(x, y);
  while (hit?.shadowRoot?.elementFromPoint) {
    const inner = hit.shadowRoot.elementFromPoint(x, y);
    if (!inner || inner === hit) break;
    hit = inner;
  }
  if (!hit || !within(this, hit)) throw new Error("input target is obscured or not hit-testable");
  check(hit);
  if (requireFocus) {
    let active = document.activeElement;
    while (active?.shadowRoot?.activeElement) active = active.shadowRoot.activeElement;
    if (!active || active === document.body || active === document.documentElement
        || (!within(this, active) && !within(active, this))) {
      throw new Error("input target did not receive focus from the native click");
    }
  }
  return { x, y };
}

async function inputOperation(a) {
  const { tabId } = a;
  const objectGroup = `rappter-input-${crypto.randomUUID()}`;
  let lease, operationError, navigation = false, frameId, document, frame, root, objectId, point;
  let modifierMask = 0, mouse;
  const held = [];
  const updated = (id, info) => { if (id === tabId && info.status === "loading") navigation = true; };
  const removed = (id) => { if (id === tabId) navigation = true; };
  const navigated = (source, method, params) => {
    if (source.tabId !== tabId || source.sessionId) return;
    if (method === "Page.frameNavigated" && !params.frame?.parentId) navigation = true;
    if (method === "Page.frameDetached" && params.frameId === frameId) navigation = true;
  };
  const raw = (method, params = {}) => cdpSessions.send(tabId, method, params, lease);
  const guard = () => {
    if (navigation) throw new Error("Navigation or tab closure during input; action may be partially applied");
  };
  const send = (method, params = {}) => { guard(); return raw(method, params); };
  const value = (response) => {
    if (response?.exceptionDetails) {
      throw new Error(response.exceptionDetails.exception?.description || response.exceptionDetails.text || "input page evaluation failed");
    }
    return response?.result;
  };
  const verifyDocument = async () => {
    const currentRoot = (await send("DOM.getDocument", { depth: 0 })).root;
    const currentFrame = (await send("Page.getFrameTree")).frameTree?.frame;
    const currentDocument = await axDocument(tabId);
    guard();
    if (currentDocument.id !== document.id || currentRoot?.backendNodeId !== root.backendNodeId
        || currentFrame?.id !== frame.id || currentFrame?.loaderId !== frame.loaderId) {
      navigation = true;
      guard();
    }
  };
  const geometry = async (requireFocus = false) => {
    const result = value(await send("Runtime.callFunctionOn", {
      objectId, functionDeclaration: _inputGeometry.toString(), returnByValue: true,
      arguments: [{ value: a.target && "x" in a.target ? a.target : null }, { value: requireFocus }],
    }))?.value;
    if (!result || !Number.isFinite(result.x) || !Number.isFinite(result.y)
        || result.x < 0 || result.y < 0 || result.x > INPUT_LIMITS.MAX_COORDINATE
        || result.y > INPUT_LIMITS.MAX_COORDINATE) throw new Error("input returned invalid target geometry");
    guard();
    point = { x: result.x, y: result.y };
    return point;
  };
  const keyParams = (key, type) => ({
    type, key: key.key, code: key.code, windowsVirtualKeyCode: key.windowsVirtualKeyCode,
    modifiers: modifierMask,
    ...(key.bit ? { location: 1 } : {}),
  });
  const pressKey = async (key, commands = []) => {
    held.push(key);
    modifierMask |= key.bit || 0;
    const text = !(modifierMask & 7) ? key.text : undefined;
    await send("Input.dispatchKeyEvent", {
      ...keyParams(key, text ? "keyDown" : "rawKeyDown"),
      ...(text ? { text } : {}),
      // CDP's "unmodified" text retains Shift but ignores Ctrl/Alt/Meta.
      ...(key.text ? { unmodifiedText: key.text } : {}),
      ...(commands.length ? { commands } : {}),
    });
  };
  const releaseKey = async (key) => {
    modifierMask &= ~(key.bit || 0);
    // Key-up must bypass the navigation guard, including after Enter navigates.
    await raw("Input.dispatchKeyEvent", keyParams(key, "keyUp"));
    held.splice(held.lastIndexOf(key), 1);
  };
  const click = async (button, count) => {
    await send("Input.dispatchMouseEvent", { type: "mouseMoved", ...point, modifiers: modifierMask });
    for (let i = 1; i <= count; i++) {
      await verifyDocument();
      await geometry();
      const buttons = { left: 1, right: 2, middle: 4 }[button];
      mouse = { ...point, button, clickCount: i };
      await send("Input.dispatchMouseEvent", {
        type: "mousePressed", ...mouse, buttons, modifiers: modifierMask,
      });
      // Always release a possibly delivered press, even when it navigates.
      await raw("Input.dispatchMouseEvent", {
        type: "mouseReleased", ...mouse, clickCount: navigation ? 0 : i,
        buttons: 0, modifiers: modifierMask,
      });
      mouse = null;
    }
  };
  try {
    chrome.tabs.onUpdated.addListener(updated);
    chrome.tabs.onRemoved.addListener(removed);
    chrome.debugger.onEvent.addListener(navigated);
    lease = await cdpSessions.acquire(tabId);
    const commands = await inputEditingCommands(a);
    await send("Page.enable");
    document = await axDocument(tabId);
    frame = (await send("Page.getFrameTree")).frameTree?.frame;
    frameId = frame?.id;
    root = (await send("DOM.getDocument", { depth: 0 })).root;
    if (!frameId || !frame.loaderId || !root?.backendNodeId) throw new Error("input main-document identity is unavailable");
    if (a.target) {
      if (a.target.ref) {
        if (a.target.documentId !== document.id) throw new Error("input ref is stale for the current document");
        objectId = (await send("DOM.resolveNode", {
          backendNodeId: a.target.backendNodeId, objectGroup,
        })).object?.objectId;
      } else {
        const expression = "selector" in a.target
          ? `document.querySelectorAll(${JSON.stringify(a.target.selector)})[${a.target.index}]`
          : `document.elementFromPoint(${a.target.x},${a.target.y})`;
        objectId = value(await send("Runtime.evaluate", {
          expression, objectGroup, returnByValue: false,
        }))?.objectId;
      }
      if (!objectId) throw new Error("input target was not found or is no longer available");
      await geometry();
    }
    await verifyDocument();
    if (a.target) await geometry();
    if (a.target && ["type", "key"].includes(a.action)) {
      await click("left", 1);
      await verifyDocument();
      await geometry(true);
    }
    for (const name of a.modifiers) await pressKey(INPUT_MODIFIERS[name]);
    if (a.action === "click") await click(a.button, a.clickCount);
    if (a.action === "hover" || a.action === "scroll") {
      await verifyDocument();
      await geometry();
      await send("Input.dispatchMouseEvent", {
        type: a.action === "hover" ? "mouseMoved" : "mouseWheel", ...point,
        modifiers: modifierMask,
        ...(a.action === "scroll" ? { deltaX: a.deltaX, deltaY: a.deltaY } : {}),
      });
    }
    if (a.action === "type" || a.action === "key") {
      await verifyDocument();
      if (a.target) await geometry(true);
      if (a.action === "type") await send("Input.insertText", { text: a.text });
      else {
        await pressKey(a.key, commands);
        await releaseKey(a.key);
      }
    }
    return {
      tabId, action: a.action, dispatched: true, documentId: document.id,
      ...(point ? { point } : {}),
      ...(a.action === "type" ? { textBytes: a.textBytes } : { modifiers: a.modifiers }),
      ...(a.action === "key" ? { key: a.key.key, editingCommands: commands } : {}),
      ...(a.action === "click" ? { button: a.button, clickCount: a.clickCount } : {}),
      ...(a.action === "scroll" ? { deltaX: a.deltaX, deltaY: a.deltaY } : {}),
    };
  } catch (error) {
    operationError = error;
    throw error;
  } finally {
    let cleanupError;
    const cleanup = async (label, fn, attempts = 1, allowDestroyed = false) => {
      let error;
      for (let attempt = 0; attempt < attempts; attempt++) {
        try { await fn(); return; } catch (failure) { error = failure; }
      }
      const message = String(error?.message || error);
      const destroyed = /CDP requires an active lease|CDP session (?:ended|is no longer active)|execution context was destroyed|cannot find context with specified id|not attached|(?:target|session|tab) (?:was )?closed|no tab with id|no target with given id/i.test(message);
      if (!allowDestroyed || !destroyed) cleanupError ||= error;
      console.warn(`input ${label} cleanup failed: ${message}`);
    };
    if (lease) {
      if (mouse) {
        await cleanup("mouse", () => raw("Input.dispatchMouseEvent", {
          type: "mouseReleased", ...mouse, clickCount: 0, buttons: 0, modifiers: modifierMask,
        }), 2);
      }
      for (const key of [...held].reverse()) await cleanup("key", () => releaseKey(key), 2);
      await cleanup("remote object", () => raw("Runtime.releaseObjectGroup", { objectGroup }), 1, true);
      await cleanup("session", () => cdpSessions.release(tabId, lease));
    }
    chrome.tabs.onUpdated.removeListener(updated);
    chrome.tabs.onRemoved.removeListener(removed);
    chrome.debugger.onEvent.removeListener(navigated);
    if (cleanupError && !operationError) throw new Error(`input cleanup failed: ${cleanupError.message || cleanupError}`);
  }
}

async function browserInput(args) {
  const a = inputArguments(args);
  const preceding = inputQueues.get(a.tabId) || Promise.resolve();
  const operation = preceding.then(() => inputOperation(a));
  const tail = operation.catch(() => {});
  inputQueues.set(a.tabId, tail);
  try {
    return await operation;
  } finally {
    if (inputQueues.get(a.tabId) === tail) inputQueues.delete(a.tabId);
  }
}
