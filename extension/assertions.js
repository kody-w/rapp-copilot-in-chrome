// Loaded by background.js as a classic script; inPage injects declared functions
// into the MAIN world without eval or a debugger attachment.
function _pageAssertionState(condition, selector, text) {
  try {
    const element = document.querySelector(selector);
    if (!element) {
      return {
        passed: false,
        found: false,
        actual: condition === "text-contains" ? null : false,
        reason: "no element matches selector",
      };
    }

    if (condition === "text-contains") {
      const actual = /^(INPUT|TEXTAREA|SELECT)$/.test(element.tagName)
        ? String(element.value)
        : (typeof element.innerText === "string"
          ? element.innerText : element.textContent || "");
      const passed = actual.includes(text);
      return {
        passed, found: true, actual,
        reason: passed ? "element text contains expected text"
          : "element text does not contain expected text",
      };
    }

    let reason = "";
    if (condition === "enabled") {
      // :disabled includes disabled fieldsets (and their first-legend exception)
      // and disabled optgroups, unlike checking the element's property alone.
      if (element.matches(":disabled") || element.disabled === true) {
        reason = "element is disabled";
      }
      for (let node = element; !reason && node; node = node.parentElement) {
        if (node.inert || node.hasAttribute("inert")) {
          reason = "element or ancestor is inert";
        } else if ((node.getAttribute("aria-disabled") || "").trim().toLowerCase() === "true") {
          reason = "element or ancestor is aria-disabled";
        }
      }
    } else {
      const style = getComputedStyle(element);
      if (!element.isConnected) {
        reason = "element is detached";
      } else if (style.visibility === "hidden" || style.visibility === "collapse") {
        reason = "element visibility is hidden or collapsed";
      }
      for (let node = element; !reason && node; node = node.parentElement) {
        const currentStyle = node === element ? style : getComputedStyle(node);
        if (currentStyle.display === "none") {
          reason = "element or ancestor has display:none";
        } else if (Number(currentStyle.opacity) === 0) {
          reason = "element or ancestor has zero opacity";
        } else if (currentStyle.contentVisibility === "hidden") {
          reason = "element or ancestor has hidden content-visibility";
        }
      }
      if (!reason && !Array.from(element.getClientRects()).some(
        (rect) => rect.width > 0 && rect.height > 0,
      )) {
        reason = "element has no positive-area layout box";
      }
    }
    return {
      passed: !reason, found: true, actual: !reason,
      reason: reason || `element is ${condition}`,
    };
  } catch (error) {
    // executeScript does not consistently reject for exceptions in the page.
    // Send an explicit error envelope rather than treating missing data as false.
    return { error: String(error && error.message || error) };
  }
}

// timeout is a retry budget in milliseconds (0 = one asynchronous check).
// Element selectors use the first match; text is a literal, case-sensitive
// substring, and URL patterns are case-sensitive JavaScript regex sources.
// Unmet assertions resolve with passed:false; operational failures reject.
async function assertPage(a) {
  if (!a || typeof a !== "object" || Array.isArray(a)) {
    throw new TypeError("assert args must be an object");
  }
  if (!Number.isSafeInteger(a.tabId) || a.tabId < 0) {
    throw new TypeError("tabId must be a non-negative integer");
  }
  const condition = a.condition;
  if (!["visible", "enabled", "text-contains", "url-matches"].includes(condition)) {
    throw new TypeError("condition must be visible, enabled, text-contains, or url-matches");
  }
  const timeout = a.timeout === undefined ? 0 : a.timeout;
  if (typeof timeout !== "number" || !Number.isFinite(timeout)
      || timeout < 0 || timeout > 30000) {
    throw new TypeError("timeout must be a number between 0 and 30000 milliseconds");
  }
  if (a.flags !== undefined) {
    throw new TypeError("regex flags are not supported; provide a JavaScript regex source");
  }
  if (condition !== "url-matches"
      && (typeof a.selector !== "string" || !a.selector.trim())) {
    throw new TypeError("selector must be a non-empty CSS selector");
  }
  if (condition === "text-contains" && typeof a.text !== "string") {
    throw new TypeError("text must be a string for text-contains");
  }
  if (condition === "url-matches" && typeof a.pattern !== "string") {
    throw new TypeError("pattern must be a JavaScript regex source for url-matches");
  }
  const regex = condition === "url-matches" ? new RegExp(a.pattern) : null;
  const expected = regex ? a.pattern : condition === "text-contains" ? a.text : true;
  const started = Date.now();

  return new Promise((resolve, reject) => {
    let settled = false;
    let deadlineTimer;
    let pollTimer;
    let urlRevision = 0;
    let last = {
      passed: false, actual: null,
      reason: "condition could not be observed before timeout",
    };
    const finish = (error = null, timedOut = false) => {
      if (settled) return;
      settled = true;
      clearTimeout(deadlineTimer);
      clearTimeout(pollTimer);
      chrome.tabs.onUpdated.removeListener(onUpdated);
      chrome.tabs.onRemoved.removeListener(onRemoved);
      if (error) {
        reject(error);
      } else {
        resolve({
          ...last,
          condition,
          tabId: a.tabId,
          ...(regex ? {} : { selector: a.selector }),
          expected,
          reason: timedOut ? `timeout after ${timeout}ms: ${last.reason}` : last.reason,
          timedOut,
          waitedMs: Math.max(0, Date.now() - started),
        });
      }
    };
    const observeURL = (url) => {
      if (typeof url !== "string") {
        throw new Error(`URL is unavailable for tab ${a.tabId}`);
      }
      const passed = regex.test(url);
      last = {
        passed, actual: url,
        reason: passed ? "URL matches pattern" : "URL does not match pattern",
      };
      if (passed) finish();
    };
    const onRemoved = (tabId) => {
      if (tabId === a.tabId) finish(new Error(`tab ${a.tabId} was closed during assertion`));
    };
    const onUpdated = (tabId, changeInfo, tab) => {
      if (settled || tabId !== a.tabId) return;
      const url = typeof changeInfo.url === "string" ? changeInfo.url : tab?.url;
      if (typeof url !== "string") return;
      urlRevision++;
      try { observeURL(url); } catch (error) { finish(error); }
    };
    const sampleElement = async () => {
      try {
        const state = await inPage(a.tabId, _pageAssertionState, [
          condition, a.selector, condition === "text-contains" ? a.text : "",
        ]);
        if (settled) return;
        if (state && typeof state.error === "string") throw new Error(state.error);
        if (!state || typeof state.passed !== "boolean") {
          throw new Error("page assertion returned no valid result; the page may have navigated");
        }
        last = state;
        if (state.passed || timeout === 0) {
          finish();
        } else {
          const remaining = timeout - (Date.now() - started);
          if (remaining <= 0) finish(null, true);
          else pollTimer = setTimeout(sampleElement, Math.min(50, remaining));
        }
      } catch (error) {
        finish(error);
      }
    };

    try {
      chrome.tabs.onRemoved.addListener(onRemoved);
      if (regex) chrome.tabs.onUpdated.addListener(onUpdated);
      if (timeout > 0) deadlineTimer = setTimeout(() => finish(null, true), timeout);
      if (regex) {
        // Subscribe before reading. A newer event must not be overwritten by a
        // stale tabs.get response captured while navigation was in progress.
        const revision = urlRevision;
        chrome.tabs.get(a.tabId).then((tab) => {
          if (settled) return;
          if (revision === urlRevision) observeURL(tab.url);
          if (!settled && timeout === 0) finish();
        }).catch((error) => finish(error));
      } else {
        sampleElement();
      }
    } catch (error) {
      finish(error);
    }
  });
}
