// Memory-only, explicit observers. Every observer owns a CDP lease; domains are
// never disabled because the attachment can also serve eval, AX, and input.
const nativeObservers = (() => {
  const sessions = new Map();
  const MAX_BODY_COMMANDS = 4;
  const FIELD_BYTES = 4096;
  const ID_BYTES = 1024;
  let bodyCommands = 0;

  function bytes(text) {
    let size = 0;
    for (let i = 0; i < text.length; i++) {
      const code = text.charCodeAt(i);
      if (code < 0x80) size++;
      else if (code < 0x800) size += 2;
      else if (code >= 0xd800 && code <= 0xdbff
          && i + 1 < text.length && text.charCodeAt(i + 1) >= 0xdc00
          && text.charCodeAt(i + 1) <= 0xdfff) { size += 4; i++; }
      else size += 3;
    }
    return size;
  }

  function clipped(text, limit, mark) {
    if (typeof text !== "string") return "";
    let size = 0, end = 0;
    while (end < text.length) {
      const code = text.codePointAt(end);
      const width = code > 0xffff ? 2 : 1;
      const count = code < 0x80 ? 1 : code < 0x800 ? 2 : code < 0x10000 ? 3 : 4;
      if (size + count > limit) break;
      size += count;
      end += width;
    }
    if (end === text.length) return text;
    if (mark) mark.truncated = true;
    // Copy the prefix: a sliced string can otherwise retain its enormous input.
    return JSON.parse(JSON.stringify(text.slice(0, end)));
  }

  function argumentsFor(a, operation) {
    if (!a || typeof a !== "object" || Array.isArray(a)) {
      throw new Error("observe arguments must be an object");
    }
    if (!Number.isSafeInteger(a.tabId) || a.tabId < 0) {
      throw new Error("observe tabId must be a nonnegative safe integer");
    }
    if (!["console", "network"].includes(a.kind)) {
      throw new Error("observe kind must be console or network");
    }
    const allowed = new Set(["tabId", "kind"]);
    if (operation === "start") {
      allowed.add("maxEvents"); allowed.add("maxBytes");
      if (a.kind === "network") {
        allowed.add("includeHeaders"); allowed.add("includeBodies"); allowed.add("maxBodyBytes");
      }
    } else if (operation === "read") allowed.add("clear");
    for (const key of Object.keys(a)) {
      if (!allowed.has(key)) throw new Error(`observe ${operation}: unsupported argument ${key}`);
    }
    const integer = (name, fallback, min, max) => {
      const value = a[name] === undefined ? fallback : a[name];
      if (!Number.isInteger(value) || value < min || value > max) {
        throw new Error(`${name} must be an integer from ${min} to ${max}`);
      }
      return value;
    };
    const boolean = (name) => {
      if (a[name] !== undefined && typeof a[name] !== "boolean") {
        throw new Error(`${name} must be a boolean`);
      }
      return a[name] === true;
    };
    const options = {};
    if (operation === "start") {
      options.maxEvents = integer("maxEvents", 500, 1, 5000);
      options.maxBytes = integer("maxBytes", 1048576, 1024, 8388608);
      if (a.kind === "network") {
        options.includeHeaders = boolean("includeHeaders");
        options.includeBodies = boolean("includeBodies");
        if (Object.hasOwn(a, "maxBodyBytes") && !options.includeBodies) {
          throw new Error("maxBodyBytes requires includeBodies:true");
        }
        if (options.includeBodies) options.maxBodyBytes = integer("maxBodyBytes", 65536, 1, 1048576);
      }
    }
    return {
      tabId: a.tabId, kind: a.kind, options,
      clear: operation === "read" ? boolean("clear") : false,
    };
  }

  const keyFor = (tabId, kind) => `${tabId}:${kind}`;
  function check(state) {
    if (state.failure) throw new Error(state.failure);
    if (sessions.get(state.key) !== state) throw new Error("observe session is no longer active");
  }
  function lookup(a) {
    const state = sessions.get(keyFor(a.tabId, a.kind));
    if (!state) throw new Error(`No active ${a.kind} observer for tab ${a.tabId}`);
    check(state);
    if (state.status !== "active") throw new Error(`observe session is ${state.status}; retry after it completes`);
    return state;
  }

  function clearRing(state, counter) {
    state.counters[counter] += state.count;
    state.ring.fill(undefined);
    state.head = 0;
    state.count = 0;
    state.eventBytes = 0;
  }
  function append(state, event) {
    if (state.status !== "active") return;
    state.counters.receivedEvents++;
    if (event.truncated) state.counters.truncatedEvents++;
    const json = JSON.stringify(event);
    const size = bytes(json);
    if (size > state.options.maxBytes) {
      state.counters.droppedEvents++;
      state.counters.oversizedEvents++;
      return;
    }
    while (state.count && (state.count >= state.options.maxEvents
        || state.eventBytes + size > state.options.maxBytes)) {
      state.eventBytes -= state.ring[state.head].size;
      state.ring[state.head] = undefined;
      state.head = (state.head + 1) % state.options.maxEvents;
      state.count--;
      state.counters.droppedEvents++;
      state.counters.evictedEvents++;
    }
    state.ring[(state.head + state.count) % state.options.maxEvents] = { json, size };
    state.count++;
    state.eventBytes += size;
  }
  function base(state, type, timestamp, receipt = Date.now()) {
    return {
      type, generation: state.generation, receivedAt: receipt,
      ...(Number.isFinite(timestamp) ? {
        cdpTimestamp: timestamp,
        cdpTimestampUnit: state.kind === "console" ? "epochMilliseconds" : "monotonicSeconds",
      } : {}),
    };
  }
  function text(value, event, limit = FIELD_BYTES) { return clipped(value, limit, event); }
  function frameIdentity(value, name, allowEmpty = false) {
    if (typeof value !== "string" || (!value && !allowEmpty)
        || value.length > ID_BYTES || bytes(value) > ID_BYTES) {
      throw new Error(`observe main-frame ${name} is unavailable or exceeds ${ID_BYTES} bytes`);
    }
    return value;
  }
  function scalar(value, event) {
    if (typeof value === "string") return text(value, event);
    return typeof value === "boolean" || (typeof value === "number" && Number.isFinite(value))
      || value === null ? value : undefined;
  }
  function remote(value, event) {
    return {
      type: text(value?.type, event, 64),
      ...(typeof value?.subtype === "string" ? { subtype: text(value.subtype, event, 64) } : {}),
      ...(scalar(value?.value, event) !== undefined ? { value: scalar(value.value, event) } : {}),
      ...(typeof value?.description === "string" ? { description: text(value.description, event) } : {}),
      ...(typeof value?.unserializableValue === "string"
        ? { unserializableValue: text(value.unserializableValue, event) } : {}),
    };
  }
  function stack(value, event) {
    const frames = value?.callFrames;
    if (!Array.isArray(frames)) return undefined;
    if (frames.length > 8 || value.parent || value.parentId) event.truncated = true;
    return frames.slice(0, 8).map((frame) => ({
      functionName: text(frame.functionName, event),
      url: text(frame.url, event),
      lineNumber: scalar(frame.lineNumber, event),
      columnNumber: scalar(frame.columnNumber, event),
    }));
  }
  function consoleEvent(state, method, params) {
    const timestamp = method === "Log.entryAdded" ? params.entry?.timestamp : params.timestamp;
    if (Number.isFinite(timestamp) && timestamp < state.generationStartedAt) {
      state.counters.ignoredReplayEvents++;
      return;
    }
    const event = base(state, method, timestamp);
    if (method === "Runtime.consoleAPICalled") {
      event.level = text(params.type, event, 64);
      const args = Array.isArray(params.args) ? params.args : [];
      if (args.length > 32) event.truncated = true;
      event.args = args.slice(0, 32).map((value) => remote(value, event));
      event.stack = stack(params.stackTrace, event);
      event.executionContextId = scalar(params.executionContextId, event);
    } else if (method === "Runtime.exceptionThrown") {
      const details = params.exceptionDetails || {};
      event.text = text(details.text, event);
      event.exception = remote(details.exception, event);
      event.url = text(details.url, event);
      event.lineNumber = scalar(details.lineNumber, event);
      event.columnNumber = scalar(details.columnNumber, event);
      event.stack = stack(details.stackTrace, event);
    } else {
      const entry = params.entry || {};
      event.level = text(entry.level, event, 64);
      event.source = text(entry.source, event, 64);
      event.text = text(entry.text, event);
      event.url = text(entry.url, event);
      event.lineNumber = scalar(entry.lineNumber, event);
      event.stack = stack(entry.stackTrace, event);
    }
    append(state, event);
  }

  function headers(value, event) {
    const result = Object.create(null);
    let size = 0, count = 0;
    if (!value || typeof value !== "object") return result;
    for (const name in value) {
      if (!Object.hasOwn(value, name)) continue;
      if (count === 64 || size >= 16384) { event.truncated = true; break; }
      const key = text(name, event, 256);
      const item = text(value[name], event, Math.min(FIELD_BYTES, 16384 - size));
      const nextSize = bytes(JSON.stringify({ [key]: item }));
      if (size + nextSize > 16384) { event.truncated = true; break; }
      result[key] = item;
      size += nextSize;
      count++;
    }
    return result;
  }
  function response(state, raw, event) {
    const value = raw || {};
    return {
      url: text(value.url, event),
      status: scalar(value.status, event),
      statusText: text(value.statusText, event, 256),
      mimeType: text(value.mimeType, event, 256),
      protocol: text(value.protocol, event, 64),
      fromDiskCache: value.fromDiskCache === true,
      fromServiceWorker: value.fromServiceWorker === true,
      ...(state.options.includeHeaders ? { headers: headers(value.headers, event) } : {}),
    };
  }
  function removeRequest(state, id) {
    const old = state.requests.get(id);
    if (old) {
      state.requestBytes -= old.size;
      state.requests.delete(id);
    }
    return old;
  }
  function track(state, record) {
    record.size = bytes(JSON.stringify(record));
    if (record.size > state.limits.maxTrackedRequestBytes) {
      state.counters.droppedRequests++;
      return;
    }
    while (state.requests.size && (state.requests.size >= state.limits.maxTrackedRequests
        || state.requestBytes + record.size > state.limits.maxTrackedRequestBytes)) {
      removeRequest(state, state.requests.keys().next().value);
      state.counters.droppedRequests++;
    }
    state.requests.set(record.requestId, record);
    state.requestBytes += record.size;
  }
  function cancelBodies(state, requestId) {
    for (const job of state.jobs) {
      if (requestId !== undefined && job.requestId !== requestId) continue;
      job.valid = false;
      state.jobs.delete(job);
      state.counters.discardedBodies++;
    }
  }
  function retainedBody(raw, maxBytes) {
    if (typeof raw?.body !== "string" || typeof raw.base64Encoded !== "boolean") {
      throw new Error("Network.getResponseBody returned an invalid body");
    }
    if (!raw.base64Encoded) {
      const originalBytes = bytes(raw.body);
      const data = clipped(raw.body, maxBytes);
      return {
        data, base64Encoded: false, originalBytes, retainedBytes: bytes(data),
        truncated: originalBytes > maxBytes,
      };
    }
    const value = raw.body;
    if (value.length % 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(value)) {
      throw new Error("Network.getResponseBody returned invalid base64");
    }
    const originalBytes = value.length / 4 * 3
      - (value.endsWith("==") ? 2 : value.endsWith("=") ? 1 : 0);
    const retainedBytes = Math.min(originalBytes, maxBytes);
    const data = btoa(atob(value.slice(0, Math.ceil(retainedBytes / 3) * 4)).slice(0, retainedBytes));
    return {
      data, base64Encoded: true, originalBytes, retainedBytes,
      truncated: retainedBytes < originalBytes,
    };
  }
  function body(state, event) {
    if (bodyCommands >= MAX_BODY_COMMANDS) {
      state.counters.skippedBodies++;
      event.bodySkipped = "worker body-command limit reached (no waiting queue)";
      return;
    }
    const job = {
      valid: true, generation: state.generation, requestId: event.requestId,
      requestSequence: event.requestSequence, hop: event.hop,
      receivedAt: event.receivedAt, timestamp: event.cdpTimestamp,
    };
    state.jobs.add(job);
    bodyCommands++;
    // Only this small job and the bounded state survive the async boundary;
    // raw network events and full response bodies are never saved in side maps.
    (async () => {
      let captured, error;
      try {
        const raw = await cdpSessions.send(state.tabId, "Network.getResponseBody", {
          requestId: job.requestId,
        }, state.lease);
        if (job.valid) captured = retainedBody(raw, state.options.maxBodyBytes);
      } catch (failure) {
        error = clipped(String(failure?.message || failure), FIELD_BYTES);
      }
      if (job.valid && state.status === "active" && sessions.get(state.key) === state
          && state.generation === job.generation) {
        const result = {
          ...base(state, "Network.responseBody", job.timestamp, job.receivedAt),
          completedAt: Date.now(), requestId: job.requestId,
          requestSequence: job.requestSequence, hop: job.hop,
          ...(error ? { error } : { body: captured }),
          ...(captured?.truncated ? { truncated: true } : {}),
        };
        if (error) {
          state.counters.bodyErrors++;
          state.lastError = { receivedAt: result.completedAt, message: error };
        }
        append(state, result);
      }
    })().catch((error) => end(state, `body processing failed: ${error?.message || error}`))
      .finally(() => {
        bodyCommands--;
        state.jobs.delete(job);
      });
  }
  function networkEvent(state, method, params) {
    const id = params.requestId;
    if (typeof id !== "string" || !id || id.length > ID_BYTES || bytes(id) > ID_BYTES) {
      state.counters.receivedEvents++;
      state.counters.droppedEvents++;
      state.counters.invalidRequestIds++;
      return;
    }
    const event = { ...base(state, method, params.timestamp), requestId: id };
    let record = state.requests.get(id);
    if (method === "Network.requestWillBeSent") {
      cancelBodies(state, id);
      removeRequest(state, id);
      const redirect = params.redirectResponse;
      if (redirect) {
        const redirected = {
          ...base(state, "Network.redirectResponse", params.timestamp),
          requestId: id, requestSequence: record?.sequence ?? null,
          hop: record?.hop ?? null, matched: !!record,
        };
        redirected.response = response(state, redirect, redirected);
        append(state, redirected);
      }
      const request = params.request || {};
      event.request = {
        url: text(request.url, event), method: text(request.method, event, 64),
        ...(state.options.includeHeaders ? { headers: headers(request.headers, event) } : {}),
      };
      event.resourceType = text(params.type, event, 64);
      event.frameId = text(params.frameId, event, ID_BYTES);
      event.loaderId = text(params.loaderId, event, ID_BYTES);
      event.redirectFromSequence = redirect ? record?.sequence ?? null : null;
      record = {
        requestId: id, sequence: ++state.requestSequence,
        hop: redirect ? (record && record.hop !== null ? record.hop + 1 : null) : 0,
        responseReceived: false,
      };
      track(state, record);
    } else if (method === "Network.responseReceived") {
      event.response = response(state, params.response, event);
      event.resourceType = text(params.type, event, 64);
      if (record) record.responseReceived = true;
    } else if (method === "Network.loadingFinished") {
      event.encodedDataLength = scalar(params.encodedDataLength, event);
    } else {
      event.errorText = text(params.errorText, event);
      event.canceled = params.canceled === true;
      event.blockedReason = text(params.blockedReason, event, 256);
    }
    event.requestSequence = record?.sequence ?? null;
    event.hop = record?.hop ?? null;
    event.matched = !!record;
    if (method === "Network.loadingFinished" || method === "Network.loadingFailed") {
      removeRequest(state, id);
      if (method === "Network.loadingFinished" && state.options.includeBodies) {
        if (record?.responseReceived) body(state, event);
        else {
          state.counters.skippedBodies++;
          event.bodySkipped = "request or response not tracked in this generation";
        }
      }
    }
    append(state, event);
  }

  function navigation(state, frame) {
    if (!frame || frame.parentId || typeof frame.id !== "string") return;
    const frameId = frameIdentity(frame.id, "id");
    const loaderId = frameIdentity(frame.loaderId ?? "", "loaderId", true);
    if (loaderId && state.frame?.frameId === frameId && state.frame.loaderId === loaderId) return;
    state.frame = { frameId, loaderId };
    state.generation++;
    state.generationStartedAt = Date.now();
    state.counters.navigations++;
    clearRing(state, "navigationClearedEvents");
    state.requests.clear();
    state.requestBytes = 0;
    cancelBodies(state);
    state.lastError = null;
  }
  function scrub(state) {
    clearRing(state, "clearedEvents");
    state.requests.clear();
    state.requestBytes = 0;
    cancelBodies(state);
    state.lastError = null;
  }
  async function release(state) {
    if (!state.lease) return null;
    const lease = state.lease;
    state.lease = null;
    return await cdpSessions.release(state.tabId, lease);
  }
  function end(state, reason) {
    state.failure ||= clipped(`observe session ended: ${reason}`, FIELD_BYTES);
    scrub(state);
    // An in-progress acquire/enable owns its own finally cleanup.
    if (state.status !== "active") return;
    state.status = "ended";
    console.warn(state.failure);
    release(state).catch((error) => console.warn(`observe cleanup failed: ${
      clipped(String(error?.message || error), FIELD_BYTES)}`)).finally(() => {
      if (sessions.get(state.key) === state) sessions.delete(state.key);
    });
  }

  function snapshot(state, status = state.status) {
    const events = [];
    for (let i = 0; i < state.count; i++) {
      events.push(JSON.parse(state.ring[(state.head + i) % state.options.maxEvents].json));
    }
    return {
      tabId: state.tabId, kind: state.kind, status,
      startedAt: state.startedAt, captureStartedAt: state.captureStartedAt,
      generation: state.generation, generationStartedAt: state.generationStartedAt,
      frame: { ...state.frame }, options: { ...state.options }, limits: { ...state.limits },
      counters: { ...state.counters }, lastError: state.lastError && { ...state.lastError },
      buffer: { eventCount: state.count, bytes: state.eventBytes },
      trackedRequests: state.requests.size, trackedRequestBytes: state.requestBytes,
      pendingBodies: state.jobs.size, workerPendingBodyCommands: bodyCommands,
      events,
      semantics: {
        storage: "worker memory only; nothing is persisted",
        byteAccounting: "sum of UTF-8 JSON event bytes, excluding array separators and result metadata",
        overflow: "evict oldest events; an individually oversized event is dropped without evicting others",
        counters: "session lifetime; clear/navigation do not reset counters; droppedEvents counts overflow/invalid IDs",
        timestamps: "receivedAt is worker epoch milliseconds; cdpTimestampUnit identifies CDP units; responseBody uses the triggering loadingFinished timestamps and completedAt for retrieval completion",
        clear: "read(clear:true) returns the pre-clear snapshot then clears only buffered events",
        navigation: "new top-frame Page.frameNavigated document/loader, including same-URL reload, clears events, request matching and pending bodies; subframes and same-document history do not reset",
        scope: "root debugger target only; same-process subframes may contribute events, child debugger sessions are ignored",
        replay: "events during startup and console/Log timestamps older than the capture/generation boundary are excluded; timestamp-less events cannot be classified as replay",
        matching: "requestSequence identifies each redirect hop; unmatched events have null sequence/hop when a request predates start/navigation or was evicted",
        tracking: "request map bounds count and serialized identity metadata bytes at insertion; headers, URLs and bodies are not retained in this map",
        bodies: "request bodies never retained; response retrieval requires includeBodies:true and a tracked request/response; at most 4 unsettled commands worker-wide, no waiting queue; excess work is reported as skipped",
        bodyBytes: "maxBodyBytes measures decoded bytes for base64 or UTF-8 bytes for text; CDP may transiently deliver the whole body; stored base64 is valid and may exceed decoded size",
        networkBuffers: "Network.enable requests content buffers of maxBytes total and min(maxBytes,maxBodyBytes) per resource when bodies are enabled, otherwise 1024 bytes each, with maxPostDataSize:0; browser eviction can make oversized bodies unavailable rather than truncated",
        privacy: "flags govern worker retention/output, not browser caches or transient CDP payloads; no remote object handles, nested values or previews are retained; shared domains and their bounded browser buffers can remain enabled until the shared attachment ends",
        stop: "snapshot then erase retained data and release the lease without waiting for bodies; late results discarded",
        cleanup: "domains are not disabled; shared CDP manager detaches after its idle grace and outstanding commands, not necessarily before stop returns",
        lifetime: "ends on stop, debugger detach, tab removal or worker restart; navigation resets data but keeps the lease",
      },
    };
  }

  async function start(raw) {
    const a = argumentsFor(raw, "start");
    const key = keyFor(a.tabId, a.kind);
    if (sessions.has(key)) throw new Error(`An ${a.kind} observer already exists for tab ${a.tabId}`);
    const now = Date.now();
    const state = {
      key, tabId: a.tabId, kind: a.kind, options: a.options,
      status: "starting", lease: null, failure: null,
      startedAt: now, captureStartedAt: null, generation: 0, generationStartedAt: now,
      frame: null, ring: new Array(a.options.maxEvents), head: 0, count: 0, eventBytes: 0,
      requests: new Map(), requestBytes: 0, requestSequence: 0, jobs: new Set(), lastError: null,
      limits: {
        maxFieldBytes: FIELD_BYTES, maxRequestIdBytes: ID_BYTES,
        maxConsoleArguments: 32, maxStackFrames: 8, maxHeaderEntries: 64, maxHeaderBytes: 16384,
        maxTrackedRequests: Math.min(a.options.maxEvents, 1000),
        maxTrackedRequestBytes: Math.min(a.options.maxBytes, 1048576),
        maxPendingBodyCommandsWorker: MAX_BODY_COMMANDS, maxQueuedBodies: 0,
        ...(a.kind === "network" ? {
          maxNetworkBufferBytes: a.options.includeBodies ? a.options.maxBytes : 1024,
          maxNetworkResourceBufferBytes: a.options.includeBodies
            ? Math.min(a.options.maxBytes, a.options.maxBodyBytes) : 1024,
        } : {}),
      },
      counters: {
        receivedEvents: 0, droppedEvents: 0, evictedEvents: 0, oversizedEvents: 0,
        truncatedEvents: 0, ignoredReplayEvents: 0, ignoredStartupEvents: 0,
        clearedEvents: 0, navigationClearedEvents: 0, navigations: 0,
        droppedRequests: 0, invalidRequestIds: 0,
        skippedBodies: 0, discardedBodies: 0, bodyErrors: 0,
      },
    };
    sessions.set(key, state);
    try {
      state.lease = await cdpSessions.acquire(a.tabId);
      check(state);
      const send = async (method, params = {}) => {
        const result = await cdpSessions.send(a.tabId, method, params, state.lease);
        check(state);
        return result;
      };
      await send("Page.enable");
      const generation = state.generation;
      const frame = (await send("Page.getFrameTree"))?.frameTree?.frame;
      if (!frame || typeof frame.id !== "string" || !frame.id
          || typeof frame.loaderId !== "string") throw new Error("observe main-frame identity is unavailable");
      if (state.generation === generation) {
        state.frame = {
          frameId: frameIdentity(frame.id, "id"),
          loaderId: frameIdentity(frame.loaderId, "loaderId", true),
        };
      }
      if (a.kind === "console") {
        await send("Runtime.enable");
        await send("Log.enable");
      } else {
        // Positive caps avoid interpreting zero as a browser's default buffer.
        await send("Network.enable", {
          maxTotalBufferSize: state.limits.maxNetworkBufferBytes,
          maxResourceBufferSize: state.limits.maxNetworkResourceBufferBytes,
          maxPostDataSize: 0,
        });
      }
      state.captureStartedAt = Date.now();
      state.generationStartedAt = state.captureStartedAt;
      state.status = "active";
      return snapshot(state);
    } catch (error) {
      state.status = "ended";
      scrub(state);
      let cleanupError;
      try { await release(state); } catch (failure) { cleanupError = failure; }
      if (sessions.get(key) === state) sessions.delete(key);
      throw new Error(`${clipped(String(error?.message || error), FIELD_BYTES)}${
        cleanupError ? `; observe cleanup failed: ${clipped(String(cleanupError?.message || cleanupError), FIELD_BYTES)}` : ""}`);
    }
  }
  async function read(raw) {
    const a = argumentsFor(raw, "read");
    const state = lookup(a);
    const result = snapshot(state);
    result.cleared = a.clear;
    if (a.clear) clearRing(state, "clearedEvents");
    return result;
  }
  async function stop(raw) {
    const a = argumentsFor(raw, "stop");
    const state = lookup(a);
    state.status = "stopping";
    cancelBodies(state);
    const result = snapshot(state, "stopped");
    scrub(state);
    try {
      result.leaseReleased = await release(state);
      if (state.failure) throw new Error(state.failure);
      return result;
    } catch (error) {
      throw new Error(`observe cleanup failed: ${clipped(String(error?.message || error), FIELD_BYTES)}`);
    } finally {
      if (sessions.get(state.key) === state) sessions.delete(state.key);
    }
  }

  chrome.debugger.onEvent.addListener((source, method, params = {}) => {
    if (source.sessionId) return;
    for (const kind of ["console", "network"]) {
      const state = sessions.get(keyFor(source.tabId, kind));
      if (!state || state.failure || !["active", "starting"].includes(state.status)) continue;
      try {
        if (method === "Page.frameNavigated") navigation(state, params.frame);
        else if ((kind === "console" && [
          "Runtime.consoleAPICalled", "Runtime.exceptionThrown", "Log.entryAdded",
        ].includes(method)) || (kind === "network" && [
          "Network.requestWillBeSent", "Network.responseReceived",
          "Network.loadingFinished", "Network.loadingFailed",
        ].includes(method))) {
          if (state.status === "starting") state.counters.ignoredStartupEvents++;
          else if (kind === "console") consoleEvent(state, method, params);
          else networkEvent(state, method, params);
        }
      } catch (error) { end(state, `event processing failed: ${error?.message || error}`); }
    }
  });
  chrome.debugger.onDetach.addListener((source, reason) => {
    if (source.sessionId) return;
    for (const kind of ["console", "network"]) {
      const state = sessions.get(keyFor(source.tabId, kind));
      if (state) end(state, reason || "debugger detached");
    }
  });
  chrome.tabs.onRemoved.addListener((tabId) => {
    for (const kind of ["console", "network"]) {
      const state = sessions.get(keyFor(tabId, kind));
      if (state) end(state, "tab removed");
    }
  });
  return Object.freeze({ start, read, stop });
})();

async function observeStart(a) { return nativeObservers.start(a); }
async function observeRead(a) { return nativeObservers.read(a); }
async function observeStop(a) { return nativeObservers.stop(a); }
