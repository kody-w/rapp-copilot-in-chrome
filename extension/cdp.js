// Shared classic-worker CDP sessions. The debugger banner lasts only for active
// work plus a 250ms idle grace; sessions are never deliberately left attached.
// Each acquire returns an opaque lease. Pass it to send/release so a detached
// caller cannot accidentally use or release a replacement session on the tab.
// The tokenless compatibility API addresses only an unambiguous tab session;
// after detach, unresolved older acquisitions require explicit lease tokens.
const cdpSessions = (() => {
  const IDLE_GRACE_MS = 250;
  const DETACH_ATTEMPTS = 3;
  const sessions = new Map();
  const leases = new WeakMap();
  const ambiguous = new Map();

  function validTab(tabId) {
    if (!Number.isSafeInteger(tabId) || tabId < 0) {
      throw new Error("CDP tabId must be a nonnegative safe integer");
    }
  }

  function clearIdle(state) {
    if (state.timer !== null) clearTimeout(state.timer);
    state.timer = null;
  }

  function invalidate(state, reason) {
    clearIdle(state);
    if (!state.error) {
      state.invalidEpoch = state.epoch;
      if (state.refs) {
        state.staleRefs = state.refs;
        if (!ambiguous.has(state.tabId)) ambiguous.set(state.tabId, new Set());
        ambiguous.get(state.tabId).add(state);
      }
    }
    state.error ||= new Error(`CDP session ended: ${reason}`);
    state.refs = 0;
    state.epoch++;
  }

  function settleStale(record) {
    const state = record.state;
    if (record.epoch !== state.invalidEpoch) return;
    if (!state.staleRefs || --state.staleRefs) return;
    const generations = ambiguous.get(state.tabId);
    generations?.delete(state);
    if (!generations?.size) ambiguous.delete(state.tabId);
  }

  function unambiguous(tabId) {
    if (ambiguous.has(tabId)) {
      throw new Error("CDP lease required after detach while older acquisitions remain unresolved");
    }
  }

  function current(state) {
    if (sessions.get(state.tabId) !== state || state.error || state.retiring) {
      throw state.error || new Error("CDP session is no longer active");
    }
  }

  function retire(state) {
    if (state.retiring) return state.retiring;
    clearIdle(state);
    // Keep the old generation in the map until attach, queued commands and
    // detach have settled. A replacement must never race an old detach.
    state.retiring = Promise.resolve().then(async () => {
      await state.ready.catch(() => {});
      await state.queue;
      for (let attempt = 0; state.attached; attempt++) {
        try {
          await chrome.debugger.detach({ tabId: state.tabId });
          state.attached = false;
        } catch (error) {
          if (!state.attached || /not attached|no tab|no target|target closed/i.test(String(error))) {
            state.attached = false;
          } else if (attempt + 1 >= DETACH_ATTEMPTS) {
            // Fail closed rather than treating an uncertain attachment as a
            // fresh session. A later onDetach can still clear this state.
            state.cleanupFailed = true;
            throw new Error(`CDP debugger cleanup failed: ${error.message || error}`);
          }
        }
      }
      if (sessions.get(state.tabId) === state) sessions.delete(state.tabId);
    });
    return state.retiring;
  }

  function cleanup(state) {
    retire(state).catch((error) => console.warn(error.message));
  }

  function idle(state) {
    if (state.refs || state.pending || state.error || state.retiring) return;
    clearIdle(state);
    state.timer = setTimeout(() => {
      state.timer = null;
      if (!state.refs && !state.pending) cleanup(state);
    }, IDLE_GRACE_MS);
  }

  async function acquire(tabId) {
    validTab(tabId);
    let state;
    for (;;) {
      state = sessions.get(tabId);
      if (!state) {
        state = {
          tabId, refs: 0, epoch: 0, queue: Promise.resolve(), pending: 0,
          timer: null, attached: false, error: null, retiring: null,
          cleanupFailed: false, staleRefs: 0,
        };
        sessions.set(tabId, state);
        state.ready = Promise.resolve().then(async () => {
          try {
            await chrome.debugger.attach({ tabId }, "1.3");
            state.attached = true;
            current(state);
          } catch (error) {
            invalidate(state, error.message || error);
            cleanup(state);
            throw error;
          }
        });
      }
      if (!state.error && !state.retiring) break;
      await retire(state);
    }
    clearIdle(state);
    const lease = Object.freeze({});
    const record = { state, epoch: state.epoch, released: false };
    leases.set(lease, record);
    state.refs++;
    try {
      await state.ready;
      current(state);
      return lease;
    } catch (error) {
      record.released = true;
      settleStale(record);
      throw error;
    }
  }

  function owner(tabId, lease) {
    validTab(tabId);
    if (lease === undefined) {
      unambiguous(tabId);
      const state = sessions.get(tabId);
      if (!state || !state.refs || !state.attached) {
        throw new Error("CDP requires an active acquisition from acquire(tabId)");
      }
      current(state);
      return state;
    }
    const record = lease && typeof lease === "object" ? leases.get(lease) : undefined;
    const state = record?.state;
    if (!state || state.tabId !== tabId || record.released
        || record.epoch !== state.epoch || !state.refs) {
      throw new Error("CDP requires an active lease from acquire(tabId)");
    }
    current(state);
    return state;
  }

  async function send(tabId, method, params = {}, lease) {
    const state = owner(tabId, lease);
    if (typeof method !== "string" || !method) throw new Error("CDP method must be a string");
    state.pending++;
    const result = state.queue.then(async () => {
      current(state);
      const response = await chrome.debugger.sendCommand({ tabId }, method, params);
      current(state);
      return response;
    }).finally(() => {
      state.pending--;
      idle(state);
    });
    // Errors belong to the individual caller, not to the next queued command.
    state.queue = result.catch(() => {});
    return result;
  }

  function release(tabId, lease) {
    validTab(tabId);
    let state;
    if (lease === undefined) {
      unambiguous(tabId);
      state = sessions.get(tabId);
      if (!state || !state.refs || state.error || state.retiring) {
        throw new Error("CDP release called without an active acquisition");
      }
    } else {
      const record = lease && typeof lease === "object" ? leases.get(lease) : undefined;
      state = record?.state;
      if (!state || state.tabId !== tabId) throw new Error("CDP release received an invalid lease");
      if (record.released) throw new Error("CDP lease has already been released");
      // A finally block from an externally detached operation must not mask its
      // original failure, nor decrement a replacement session's reference count.
      if (state.error) {
        if (record.epoch !== state.invalidEpoch) {
          throw new Error("CDP release called without an active lease");
        }
        record.released = true;
        settleStale(record);
        return false;
      }
      if (record.epoch !== state.epoch || !state.refs || sessions.get(tabId) !== state) {
        throw new Error("CDP release called without an active lease");
      }
      record.released = true;
    }
    state.refs--;
    // Invalidate tokens discarded by tokenless callers once all users release.
    // Keeping only a counter also lets legacy and leased callers coexist.
    if (!state.refs) state.epoch++;
    idle(state);
    return true;
  }

  chrome.debugger.onDetach.addListener((source, reason) => {
    // A detached child target is not the root tab's debugger session.
    if (source.sessionId) return;
    const state = sessions.get(source.tabId);
    if (!state) return;
    state.attached = false;
    invalidate(state, reason || "debugger detached");
    if (state.cleanupFailed) state.retiring = null;
    cleanup(state);
  });
  chrome.tabs.onRemoved.addListener((tabId) => {
    const state = sessions.get(tabId);
    if (state) {
      invalidate(state, "tab closed");
      cleanup(state);
    }
    ambiguous.delete(tabId);
  });

  return Object.freeze({ acquire, send, release, IDLE_GRACE_MS, DETACH_ATTEMPTS });
})();
