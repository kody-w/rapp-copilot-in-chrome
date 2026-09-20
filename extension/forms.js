// Classic-worker form controls: declared functions injected by inPage into MAIN.
function _pageFormInput(selector, text, submit, index, by) {
  try {
    const elements = document.querySelectorAll(selector);
    const element = elements[index];
    if (!element) {
      throw new Error(`no element for ${selector} [${index}] (${elements.length} matched)`);
    }
    const normalize = (value) => value.replace(/\s+/g, " ").trim();
    const writable = (control) => {
      if (!control.isConnected) throw new Error("form control is detached");
      if (control.matches(":disabled") || control.disabled === true) {
        throw new Error("form control is disabled");
      }
      if (control.readOnly === true || control.hasAttribute("readonly")) {
        throw new Error("form control is read-only");
      }
      for (let node = control; node; node = node.parentElement || node.getRootNode?.().host) {
        if (node.inert || node.hasAttribute("inert")) {
          throw new Error("form control or ancestor is inert");
        }
        if (normalize(node.getAttribute("aria-disabled") || "").toLowerCase() === "true") {
          throw new Error("form control or ancestor is aria-disabled");
        }
        if (normalize(node.getAttribute("aria-readonly") || "").toLowerCase() === "true") {
          throw new Error("form control or ancestor is aria-readonly");
        }
      }
    };
    const accessor = (prototype, property) => {
      const descriptor = Object.getOwnPropertyDescriptor(prototype, property);
      if (!descriptor?.set || !descriptor.get) {
        throw new Error(`native ${property} accessor is unavailable`);
      }
      return {
        get: (control) => descriptor.get.call(control),
        set: (control, value) => descriptor.set.call(control, value),
      };
    };
    const events = (control) => {
      control.dispatchEvent(new Event("input", { bubbles: true }));
      control.dispatchEvent(new Event("change", { bubbles: true }));
    };
    const enter = (control) => {
      // Compatibility only: these synthetic events are NOT trusted keystrokes
      // or guaranteed form submission. Use the native input tool for trusted Enter.
      if (submit) {
        for (const type of ["keydown", "keyup"]) {
          control.dispatchEvent(new KeyboardEvent(type, {
            key: "Enter", code: "Enter", keyCode: 13, bubbles: true,
          }));
        }
      }
    };
    const mismatch = () => {
      throw new Error("form control did not retain the requested state (normalized, rejected, or changed by the page)");
    };
    writable(element);
    const selection = element instanceof HTMLSelectElement || element instanceof HTMLOptionElement;
    if (by === "label" && !selection) {
      throw new TypeError("by label requires a select or option target");
    }

    if (element instanceof HTMLInputElement && ["checkbox", "radio"].includes(element.type)) {
      if (typeof text !== "boolean") throw new TypeError("checkbox/radio text must be a boolean");
      const type = element.type;
      const checked = accessor(HTMLInputElement.prototype, "checked");
      const indeterminate = type === "checkbox"
        ? accessor(HTMLInputElement.prototype, "indeterminate") : null;
      element.focus();
      writable(element);
      if (element.type !== type) mismatch();
      const checkedChanged = checked.get(element) !== text;
      const mixed = indeterminate ? indeterminate.get(element) : false;
      const changed = checkedChanged || mixed;
      // Native checked handles radio exclusivity; click would toggle or submit.
      if (checkedChanged) checked.set(element, text);
      if (mixed) indeterminate.set(element, false);
      const verify = () => {
        if (!element.isConnected || element.type !== type || checked.get(element) !== text
            || (indeterminate && indeterminate.get(element) !== false)) mismatch();
      };
      verify();
      // Resolving mixed presentation alone does not change checked state.
      if (checkedChanged) events(element);
      enter(element);
      verify();
      return {
        ok: true,
        result: { selector, checked: text, changed, ...(indeterminate ? { indeterminate: false } : {}) },
      };
    }
    if (typeof text === "boolean") {
      throw new TypeError("boolean text requires a checkbox or radio target");
    }

    if (selection) {
      const optionTarget = element instanceof HTMLOptionElement;
      const control = optionTarget ? element.closest("select") : element;
      if (!(control instanceof HTMLSelectElement)) {
        throw new Error("option has no owning select");
      }
      writable(control);
      if (Array.isArray(text) && (!control.multiple || optionTarget)) {
        throw new TypeError("an array requires a multiple select target");
      }
      const selected = accessor(HTMLOptionElement.prototype, "selected");
      const value = accessor(HTMLSelectElement.prototype, "value");
      const options = Array.from(control.options);
      const multiple = control.multiple;
      if (optionTarget && !options.includes(element)) throw new Error("option has no owning select");
      // label is the visible label attribute when nonempty, otherwise textContent.
      // Both it and the requested label are trimmed and whitespace-normalized,
      // case-sensitively. Values, including empty values, are matched literally.
      const key = (option) => by === "label"
        ? normalize(option.getAttribute("label") || option.textContent || "")
        : option.value;
      const wanted = (Array.isArray(text) ? text : [String(text)]).map(
        (entry) => by === "label" ? normalize(entry) : entry,
      );
      if (new Set(wanted).size !== wanted.length) {
        throw new Error("duplicate requested options");
      }
      const targets = wanted.map((entry) => {
        const matches = options.filter((option) => key(option) === entry);
        if (!matches.length) throw new Error(`no option matches ${by} ${JSON.stringify(entry)}`);
        if (matches.length > 1) throw new Error(`ambiguous option ${by} ${JSON.stringify(entry)}`);
        if (optionTarget && matches[0] !== element) {
          throw new Error("requested option does not match the targeted option");
        }
        writable(matches[0]);
        return matches[0];
      });
      // An option target selects only that option, preserving other selections in
      // a multiple select. A select target replaces the complete selection set.
      const expected = new Set(optionTarget && control.multiple
        ? options.filter((option) => selected.get(option)).concat(targets) : targets);
      const sameOptions = () => {
        const current = Array.from(control.options);
        return control.multiple === multiple && current.length === options.length
          && current.every((option, i) => option === options[i])
          && wanted.every((entry, i) => {
            const matches = options.filter((option) => key(option) === entry);
            return matches.length === 1 && matches[0] === targets[i];
          });
      };
      const checkOptions = () => {
        writable(control);
        if (!sameOptions()) {
          throw new Error("select options changed during form input");
        }
        targets.forEach(writable);
        if (control.multiple) {
          for (const option of options) {
            if (selected.get(option) && !expected.has(option)) writable(option);
          }
        }
      };
      checkOptions();
      control.focus();
      checkOptions();
      const changed = options.some((option) => selected.get(option) !== expected.has(option));
      if (changed) {
        if (control.multiple) {
          for (const option of options) {
            if (selected.get(option) !== expected.has(option)) {
              selected.set(option, expected.has(option));
            }
          }
        } else {
          // Selecting by option identity also supports distinct labels sharing a value.
          selected.set(targets[0], true);
        }
      }
      const verify = () => {
        if (!control.isConnected || !sameOptions()
            || options.some((option) => selected.get(option) !== expected.has(option))) mismatch();
      };
      verify();
      if (changed) events(control);
      enter(control);
      verify();
      return {
        ok: true,
        result: {
          selector, value: value.get(control),
          values: options.filter((option) => selected.get(option)).map((option) => option.value),
          changed,
        },
      };
    }

    const input = element instanceof HTMLInputElement;
    const textarea = element instanceof HTMLTextAreaElement;
    if (input && ![
      "text", "search", "tel", "url", "email", "password",
      "number", "range", "date", "datetime-local", "month", "week", "time", "color",
    ].includes(element.type)) {
      throw new Error(`unsupported input type: ${element.type}`);
    }
    if (!input && !textarea && (!element.isContentEditable
        || ["BUTTON", "FIELDSET", "OUTPUT", "METER", "PROGRESS"].includes(element.tagName))) {
      throw new Error("unsupported form control; expected an input, textarea, select, option, or contenteditable");
    }
    if (Array.isArray(text)) throw new TypeError("an array requires a multiple select target");
    const requested = String(text);
    const type = input ? element.type : null;
    const sameControl = () => input ? element.type === type : textarea || element.isContentEditable;
    const value = input || textarea
      ? accessor(input ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype, "value")
      : { get: (control) => control.textContent, set: (control, next) => { control.textContent = next; } };
    element.focus();
    writable(element);
    if (!sameControl()) mismatch();
    // Bypass React's instance setter, preserving its native-setter/input-event path.
    value.set(element, requested);
    const verify = () => {
      if (!element.isConnected || !sameControl() || value.get(element) !== requested) mismatch();
    };
    verify();
    events(element);
    enter(element);
    verify();
    return { ok: true, result: { typed: requested.length, selector, value: value.get(element) } };
  } catch (error) {
    // Page exceptions do not reliably reject executeScript; always return an envelope.
    return { error: String(error && error.message || error) };
  }
}

async function formInput(a) {
  if (!a || typeof a !== "object" || Array.isArray(a)) {
    throw new TypeError("form input args must be an object");
  }
  if (!Number.isSafeInteger(a.tabId) || a.tabId < 0) {
    throw new TypeError("tabId must be a non-negative integer");
  }
  if (typeof a.selector !== "string" || !a.selector.trim()) {
    throw new TypeError("selector must be a non-empty CSS selector");
  }
  const text = a.text;
  if (!(typeof text === "string" || typeof text === "boolean"
      || (typeof text === "number" && Number.isFinite(text))
      || (Array.isArray(text) && Array.from(text).every((entry) => typeof entry === "string")))) {
    throw new TypeError("text must be a string, boolean, finite number, or array of strings");
  }
  const index = a.index === undefined ? 0 : a.index;
  if (!Number.isSafeInteger(index) || index < 0) {
    throw new TypeError("index must be a non-negative integer");
  }
  const by = a.by === undefined ? "value" : a.by;
  if (!["value", "label"].includes(by)) throw new TypeError("by must be value or label");
  if (a.submit !== undefined && typeof a.submit !== "boolean") {
    throw new TypeError("submit must be a boolean");
  }
  const state = await inPage(a.tabId, _pageFormInput, [
    a.selector, text, a.submit === true, index, by,
  ]);
  if (state && typeof state.error === "string") throw new Error(state.error);
  const result = state?.result;
  const valid = result && typeof result === "object" && result.selector === a.selector && (
    (Number.isSafeInteger(result.typed) && result.typed >= 0 && typeof result.value === "string")
    || (typeof result.checked === "boolean" && typeof result.changed === "boolean")
    || (typeof result.value === "string" && Array.isArray(result.values)
      && result.values.every((entry) => typeof entry === "string") && typeof result.changed === "boolean")
  );
  if (state?.ok !== true || !valid) {
    throw new Error("form input returned no valid result; the page may have navigated");
  }
  return result;
}
