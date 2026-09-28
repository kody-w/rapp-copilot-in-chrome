#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "extension/forms.js"), "utf8");
const background = fs.readFileSync(path.join(__dirname, "extension/background.js"), "utf8");
const inPageSource = background.match(/async function inPage\(tabId, func, args\) \{[\s\S]*?\n\}/)[0];
const plain = (value) => JSON.parse(JSON.stringify(value));

function harness() {
  const h = { elements: [], scripts: [], inputs: [] };
  class Element {
    constructor(tagName = "DIV", properties = {}) {
      Object.assign(this, {
        tagName, parentElement: null, isConnected: true, disabled: false,
        readOnly: false, inert: false, isContentEditable: false,
        attributes: {}, events: [], writes: [], focusCount: 0, textContent: "",
      }, properties);
    }
    getAttribute(key) { return this.attributes[key] ?? null; }
    hasAttribute(key) { return Object.hasOwn(this.attributes, key); }
    getRootNode() { return this.root || {}; }
    matches(selector) {
      assert.equal(selector, ":disabled");
      return this.disabled || this.nativeDisabled
        || (this.tagName === "OPTION" && this.parentElement?.tagName === "OPTGROUP"
          && this.parentElement.disabled);
    }
    closest(selector) {
      assert.equal(selector, "select");
      for (let node = this; node; node = node.parentElement) {
        if (node.tagName === "SELECT") return node;
      }
      return null;
    }
    focus() { this.focusCount++; this.onFocus?.(); }
    dispatchEvent(event) {
      this.events.push(event);
      this.onEvent?.(event);
      return true;
    }
  }
  class Input extends Element {
    constructor(properties) {
      super("INPUT", {
        type: "text", name: "", form: null, _value: "", _checked: false, _indeterminate: false,
        ...properties,
      });
      h.inputs.push(this);
    }
    get value() { return this._value; }
    set value(value) {
      this.writes.push(["value", value]);
      if (this.setterError) throw new Error(this.setterError);
      value = String(value);
      if (this.type === "number") {
        value = value !== "" && !/^-?(?:\d+(?:\.\d+)?|\.\d+)(?:e[+-]?\d+)?$/i.test(value) ? "" : value;
      } else if (this.type === "range") {
        const min = this.min ?? 0, max = this.max ?? 100, step = this.step ?? 1;
        const number = value.trim() !== "" && Number.isFinite(Number(value)) ? Number(value) : (min + max) / 2;
        value = String(Math.max(min, Math.min(max, min + Math.round((number - min) / step) * step)));
      } else if (this.type === "date") {
        value = /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : "";
      } else if (this.type === "time") {
        value = /^(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$/.test(value) ? value : "";
      } else if (this.type === "month") {
        value = /^\d{4}-(?:0[1-9]|1[0-2])$/.test(value) ? value : "";
      } else if (this.type === "week") {
        value = /^\d{4}-W(?:0[1-9]|[1-4]\d|5[0-3])$/.test(value) ? value : "";
      } else if (this.type === "datetime-local") {
        value = value.replace(" ", "T").replace(/:00$/, "");
      } else if (this.type === "color") {
        value = /^#[\da-f]{6}$/i.test(value) ? value.toLowerCase() : "#000000";
      } else if (this.type === "email" || this.type === "url") {
        value = value.replace(/[\r\n]/g, "").trim();
      } else {
        value = value.replace(/[\r\n]/g, "");
      }
      this._value = this.rejectSet ? this._value : value;
    }
    get checked() { return this._checked; }
    set checked(value) {
      this.writes.push(["checked", value]);
      if (this.rejectSet) return;
      this._checked = Boolean(value);
      if (this.type === "radio" && this._checked && this.name) {
        for (const other of h.inputs) {
          if (other !== this && other.type === "radio" && other.name === this.name
              && other.form === this.form && other.root === this.root) {
            other._checked = false;
          }
        }
      }
    }
    get indeterminate() { return this._indeterminate; }
    set indeterminate(value) {
      this.writes.push(["indeterminate", value]);
      if (!this.rejectIndeterminate) this._indeterminate = Boolean(value);
    }
  }
  class Textarea extends Element {
    constructor(properties) { super("TEXTAREA", { _value: "", ...properties }); }
    get value() { return this._value; }
    set value(value) {
      this.writes.push(["value", value]);
      this._value = String(value).replace(/\r\n?/g, "\n");
    }
  }
  class Select extends Element {
    constructor(properties) { super("SELECT", { options: [], multiple: false, ...properties }); }
    get value() { return this.options.find((option) => option.selected)?.value || ""; }
    set value(value) {
      this.writes.push(["value", value]);
      let found = false;
      for (const option of this.options) {
        option._selected = !found && option.value === String(value);
        found ||= option._selected;
      }
    }
    add(value, label = value, properties = {}) {
      const option = new Option({ value, textContent: label, parentElement: this, ...properties });
      this.options.push(option);
      return option;
    }
  }
  class Option extends Element {
    constructor(properties) { super("OPTION", { _selected: false, value: "", ...properties }); }
    get selected() { return this._selected; }
    set selected(value) {
      this.writes.push(["selected", value]);
      if (this.rejectSet) return;
      const control = this.closest("select");
      if (value && control && !control.multiple) {
        for (const option of control.options) option._selected = false;
      }
      this._selected = Boolean(value);
    }
  }
  class DOMEvent {
    constructor(type, properties) { Object.assign(this, { type, isTrusted: false }, properties); }
  }
  const page = vm.createContext({
    HTMLInputElement: Input, HTMLTextAreaElement: Textarea,
    HTMLSelectElement: Select, HTMLOptionElement: Option,
    Event: DOMEvent, KeyboardEvent: DOMEvent,
    document: {
      querySelectorAll(selector) {
        if (selector === "[") throw new SyntaxError("invalid CSS selector");
        return h.elements;
      },
    },
  });
  const context = vm.createContext({
    chrome: { scripting: {
      async executeScript(args) {
        h.scripts.push(args);
        assert.equal(args.world, "MAIN");
        assert.deepEqual(plain(args.target), { tabId: 7 });
        assert.equal(args.func.name, "_pageFormInput");
        assert.deepEqual(plain(args.args), Array.from(args.args), "all arguments survive serialization");
        if (h.inject) return h.inject(args);
        const injected = vm.runInContext(`(${args.func.toString()})`, page);
        const result = injected(...plain(args.args));
        h.lastEnvelope = plain(result);
        return [{ result: plain(result) }];
      },
    } },
  });
  vm.runInContext(`${inPageSource}\n${source}`, context, { filename: "forms.js" });
  Object.assign(h, {
    Element, Input, Textarea, Select, Option, page, context,
    run: (text, extra = {}) => context.formInput({ tabId: 7, selector: "#target", text, ...extra }),
    target: (element) => { h.elements = [element]; return element; },
  });
  return h;
}

function eventTypes(element) { return element.events.map((event) => event.type); }
function untouched(element) {
  assert.deepEqual(element.writes, []);
  assert.deepEqual(element.events, []);
  assert.equal(element.focusCount, 0);
}
function instanceTrap(element, property) {
  const native = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(element), property);
  Object.defineProperty(element, property, {
    configurable: true,
    get() { return native.get.call(this); },
    set() { throw new Error(`React instance ${property} setter must be bypassed`); },
  });
}

test("legacy string text uses serialized MAIN function, native setter and synthetic Enter", async () => {
  const h = harness();
  const input = h.target(new h.Input());
  instanceTrap(input, "value");
  const result = plain(await h.run("hello 🐈", { submit: true }));
  assert.deepEqual(result, { typed: 8, selector: "#target", value: "hello 🐈" });
  assert.deepEqual(input.writes, [["value", "hello 🐈"]]);
  assert.equal(input.focusCount, 1);
  assert.deepEqual(eventTypes(input), ["input", "change", "keydown", "keyup"]);
  for (const event of input.events) {
    assert.equal(event.bubbles, true);
    assert.equal(event.isTrusted, false);
  }
  assert.equal(input.events[2].key, "Enter");
  assert.equal(input.events[2].code, "Enter");
  assert.equal(input.events[2].keyCode, 13);
  assert.deepEqual(plain(h.scripts[0].args), ["#target", "hello 🐈", true, 0, "value"]);
});

test("legacy empty and unchanged strings still dispatch input/change without Enter by default", async () => {
  const h = harness(), input = h.target(new h.Input());
  for (let i = 0; i < 2; i++) {
    assert.deepEqual(plain(await h.run("")), { typed: 0, selector: "#target", value: "" });
  }
  assert.deepEqual(eventTypes(input), ["input", "change", "input", "change"]);
});

test("selector index chooses the requested match; default by value works for text controls", async () => {
  const h = harness(), first = new h.Input(), second = new h.Input();
  h.elements = [first, second];
  assert.equal((await h.run("second", { index: 1, by: "value" })).value, "second");
  untouched(first);
  assert.equal(second.value, "second");
  await assert.rejects(h.run("missing", { index: 2 }), /no element.*\[2\].*2 matched/);
});

test("textarea and contenteditable retain string behavior and support literal markup", async () => {
  const h = harness();
  const textarea = h.target(new h.Textarea());
  instanceTrap(textarea, "value");
  assert.equal((await h.run("one\ntwo")).typed, 7);
  assert.equal(textarea.value, "one\ntwo");
  assert.deepEqual(eventTypes(textarea), ["input", "change"]);
  const editable = h.target(new h.Element("DIV", { isContentEditable: true }));
  assert.equal((await h.run("<b>literal</b>", { submit: true })).value, "<b>literal</b>");
  assert.equal(editable.textContent, "<b>literal</b>");
  assert.deepEqual(eventTypes(editable), ["input", "change", "keydown", "keyup"]);
});

test("finite numbers explicitly become values for text-like controls", async () => {
  const h = harness();
  for (const control of [
    new h.Input(), new h.Textarea(), new h.Element("DIV", { isContentEditable: true }),
  ]) {
    h.target(control);
    for (const text of [0, -1.25, 1e20]) {
      assert.deepEqual(plain(await h.run(text)), {
        typed: String(text).length, selector: "#target", value: String(text),
      });
    }
  }
});

test("checkbox requires a boolean, uses native checked, and dispatches only for changes", async () => {
  const h = harness(), checkbox = h.target(new h.Input({ type: "checkbox", _value: "on" }));
  instanceTrap(checkbox, "checked");
  for (const text of ["true", "false", "", 0, 1, ["on"]]) {
    await assert.rejects(h.run(text), /must be a boolean/);
    untouched(checkbox);
  }
  assert.deepEqual(plain(await h.run(false)), {
    selector: "#target", checked: false, changed: false, indeterminate: false,
  });
  assert.deepEqual(checkbox.events, []);
  assert.deepEqual(checkbox.writes, []);
  assert.deepEqual(plain(await h.run(true)), {
    selector: "#target", checked: true, changed: true, indeterminate: false,
  });
  assert.equal(checkbox.value, "on", "checked state is not a coerced value");
  assert.deepEqual(checkbox.writes, [["checked", true]]);
  assert.deepEqual(eventTypes(checkbox), ["input", "change"]);
  assert.equal((await h.run(true)).changed, false);
  assert.equal(checkbox.events.length, 2);
  assert.equal((await h.run(false)).changed, true);
  assert.deepEqual(checkbox.writes, [["checked", true], ["checked", false]]);
  assert.deepEqual(eventTypes(checkbox), ["input", "change", "input", "change"]);
});

test("checkbox boolean state clears indeterminate natively without inventing checked-change events", async () => {
  for (const checked of [false, true]) {
    for (const requested of [false, true]) {
      const h = harness();
      const checkbox = h.target(new h.Input({
        type: "checkbox", _checked: checked, _indeterminate: true,
      }));
      instanceTrap(checkbox, "checked");
      instanceTrap(checkbox, "indeterminate");
      assert.deepEqual(plain(await h.run(requested)), {
        selector: "#target", checked: requested, changed: true, indeterminate: false,
      });
      assert.equal(checkbox.indeterminate, false);
      assert.deepEqual(checkbox.writes, [
        ...(checked !== requested ? [["checked", requested]] : []), ["indeterminate", false],
      ]);
      assert.deepEqual(eventTypes(checkbox), checked !== requested ? ["input", "change"] : []);
      assert.equal((await h.run(requested)).changed, false);
    }
  }
});

test("rejected indeterminate setter or handlers restoring mixed state cannot report success", async () => {
  for (const stage of ["setter", "change", "keyup"]) {
    const h = harness();
    const checkbox = h.target(new h.Input({ type: "checkbox", _indeterminate: true }));
    if (stage === "setter") checkbox.rejectIndeterminate = true;
    else checkbox.onEvent = (event) => {
      if (event.type === stage) checkbox._indeterminate = true;
    };
    await assert.rejects(h.run(true, { submit: true }), /did not retain/);
    if (stage === "setter") assert.deepEqual(checkbox.events, []);
  }
});

test("radio setter preserves group exclusivity and supports honest false without clicking", async () => {
  const h = harness(), form = {};
  const old = new h.Input({ type: "radio", name: "group", form, _checked: true });
  const radio = h.target(new h.Input({ type: "radio", name: "group", form }));
  const differentForm = new h.Input({ type: "radio", name: "group", form: {}, _checked: true });
  const differentName = new h.Input({ type: "radio", name: "other", form, _checked: true });
  const differentRoot = new h.Input({ type: "radio", name: "group", form, root: {}, _checked: true });
  instanceTrap(radio, "checked");
  assert.equal((await h.run(true)).checked, true);
  assert.equal(old.checked, false);
  assert.equal(differentForm.checked, true);
  assert.equal(differentName.checked, true);
  assert.equal(differentRoot.checked, true);
  assert.deepEqual(old.events, [], "native deselection does not invent events on the old radio");
  assert.deepEqual(eventTypes(radio), ["input", "change"]);
  assert.equal((await h.run(false)).checked, false);
  assert.equal(old.checked, false, "false does not silently reselect a peer");
  await assert.rejects(h.run("false"), /must be a boolean/);
});

test("single select matches exact value, changes option identity natively, and emits on owner", async () => {
  const h = harness(), select = h.target(new h.Select());
  const first = select.add("1", "One", { _selected: true });
  const second = select.add("2", "Two");
  instanceTrap(second, "selected");
  instanceTrap(select, "value");
  const result = plain(await h.run(2));
  assert.deepEqual(result, { selector: "#target", value: "2", values: ["2"], changed: true });
  assert.equal(first.selected, false);
  assert.equal(second.selected, true);
  assert.deepEqual(second.writes, [["selected", true]]);
  assert.deepEqual(eventTypes(select), ["input", "change"]);
  assert.deepEqual(second.events, []);
  assert.equal((await h.run("2")).changed, false);
  assert.equal(select.events.length, 2);
  await assert.rejects(h.run(" 2 "), /no option matches value/);
});

test("labels are exact case-sensitive normalized visible labels, preferring nonempty label attribute", async () => {
  const h = harness(), select = h.target(new h.Select());
  const first = select.add("a", "  Hello \n world  ");
  const second = select.add("b", "Hidden text", { attributes: { label: "  Visible\t label  " } });
  select.add("c", "Fallback text", { attributes: { label: "" } });
  await h.run("Hello\t world", { by: "label" });
  assert.equal(first.selected, true);
  await h.run(" Visible label ", { by: "label" });
  assert.equal(second.selected, true);
  assert.equal((await h.run("Fallback text", { by: "label" })).value, "c");
  await assert.rejects(h.run("visible label", { by: "label" }), /no option matches label/);
  await assert.rejects(h.run("Hidden text", { by: "label" }), /no option matches label/);
});

test("duplicate values may be disambiguated by distinct labels but ambiguous criteria fail before mutation", async () => {
  const h = harness(), select = h.target(new h.Select());
  const first = select.add("duplicate", "One", { _selected: true });
  const second = select.add("duplicate", "Two");
  await assert.rejects(h.run("duplicate"), /ambiguous option value/);
  untouched(select);
  untouched(first);
  untouched(second);
  assert.equal((await h.run("Two", { by: "label" })).value, "duplicate");
  assert.equal(second.selected, true);
  assert.equal(first.selected, false);

  const other = h.target(new h.Select());
  other.add("a", " Same label ");
  other.add("b", "Same\nlabel");
  await assert.rejects(h.run("Same label", { by: "label" }), /ambiguous option label/);
  untouched(other);
  other.options.forEach(untouched);
});

test("multiple select replaces full set, supports labels/scalars/empty arrays, and returns DOM-order values", async () => {
  const h = harness(), select = h.target(new h.Select({ multiple: true }));
  const one = select.add("a", "One");
  const two = select.add("b", "Two", { _selected: true });
  const three = select.add("c", "Three");
  for (const option of select.options) instanceTrap(option, "selected");
  assert.deepEqual(plain(await h.run(["c", "a"])), {
    selector: "#target", value: "a", values: ["a", "c"], changed: true,
  });
  assert.equal(two.selected, false);
  assert.deepEqual(one.writes, [["selected", true]]);
  assert.deepEqual(two.writes, [["selected", false]]);
  assert.deepEqual(three.writes, [["selected", true]]);
  assert.equal((await h.run(["One", "Three"], { by: "label" })).changed, false);
  assert.equal(select.events.length, 2);
  assert.deepEqual(plain((await h.run("b")).values), ["b"]);
  assert.deepEqual(plain(await h.run([])), {
    selector: "#target", value: "", values: [], changed: true,
  });
  assert.equal((await h.run([])).changed, false);
});

test("select invalid, missing, duplicate, or disabled options fail atomically before focus/events/writes", async () => {
  const cases = [
    { text: ["a", "missing"], error: /no option/ },
    { text: ["a", "a"], error: /duplicate/ },
    { text: ["a", "b"], properties: { disabled: true }, error: /disabled/ },
    { text: ["a", "b"], properties: { attributes: { inert: "" } }, error: /inert/ },
    { text: ["Same", " Same "], by: "label", labels: ["Same", "Other"], error: /duplicate/ },
  ];
  for (const item of cases) {
    const h = harness(), select = h.target(new h.Select({ multiple: true }));
    select.add("a", item.labels?.[0] || "A");
    select.add("b", item.labels?.[1] || "B", item.properties);
    await assert.rejects(h.run(item.text, item.by ? { by: item.by } : {}), item.error);
    untouched(select);
    select.options.forEach(untouched);
  }
  const h = harness(), select = h.target(new h.Select());
  select.add("a");
  await assert.rejects(h.run(["a"]), /multiple select target/);
  untouched(select);
  select.options.forEach(untouched);
});

test("disabled optgroups and owning selects are respected for select and option targets", async () => {
  for (const targetOption of [false, true]) {
    for (const blocked of ["optgroup", "select"]) {
      const h = harness(), select = new h.Select();
      const group = new h.Element("OPTGROUP", { parentElement: select, disabled: blocked === "optgroup" });
      const option = select.add("a", "A", { parentElement: group });
      select.disabled = blocked === "select";
      h.target(targetOption ? option : select);
      await assert.rejects(h.run("a"), /disabled/);
      untouched(select);
      untouched(option);
    }
  }
});

test("disabled selected placeholders may be left in single selects but not mutated in multiple selects", async () => {
  const h = harness(), select = h.target(new h.Select());
  const placeholder = select.add("", "Choose", { disabled: true, _selected: true });
  select.add("a", "A");
  assert.equal((await h.run("a")).value, "a");
  assert.equal(placeholder.selected, false);
  await assert.rejects(h.run(""), /disabled/);

  const multiple = h.target(new h.Select({ multiple: true }));
  multiple.add("blocked", "Blocked", { disabled: true, _selected: true });
  multiple.add("a", "A");
  await assert.rejects(h.run(["a"]), /disabled/);
  untouched(multiple);
  multiple.options.forEach(untouched);
});

test("option target selects its owning select by value/label, preserving other multiple selections", async () => {
  for (const multiple of [false, true]) {
    const h = harness(), select = new h.Select({ multiple });
    const first = select.add("a", "A", { _selected: true });
    const group = new h.Element("OPTGROUP", { parentElement: select });
    const option = h.target(select.add("b", " B label ", { parentElement: group }));
    const result = plain(await h.run("B label", { by: "label", submit: true }));
    assert.deepEqual(result.values, multiple ? ["a", "b"] : ["b"]);
    assert.equal(first.selected, multiple);
    assert.equal(option.selected, true);
    assert.equal(select.focusCount, 1);
    assert.equal(option.focusCount, 0);
    assert.deepEqual(eventTypes(select), ["input", "change", "keydown", "keyup"]);
    assert.deepEqual(option.events, []);
    assert.equal((await h.run("b")).changed, false);
  }
});

test("option requests cannot silently select a different option or detached owner", async () => {
  const h = harness(), select = new h.Select();
  const option = h.target(select.add("a"));
  select.add("b");
  await assert.rejects(h.run("b"), /does not match the targeted option/);
  await assert.rejects(h.run(["a"]), /multiple select target/);
  untouched(select);
  untouched(option);
  h.target(new h.Option({ value: "a" }));
  await assert.rejects(h.run("a"), /no owning select/);
  h.target(new h.Option({ value: "a", parentElement: select }));
  await assert.rejects(h.run("a"), /no owning select/);
});

test("select empty values and literal boolean-looking strings are matched exactly", async () => {
  const h = harness(), select = h.target(new h.Select());
  select.add("true");
  select.add("false");
  select.add("");
  assert.equal((await h.run("true")).value, "true");
  assert.equal((await h.run("false")).value, "false");
  assert.deepEqual(plain((await h.run("")).values), [""]);
});

test("booleans outside checkbox/radio fail instead of silently becoming text or option values", async () => {
  const h = harness(), select = new h.Select();
  const option = select.add("true");
  select.add("false");
  for (const element of [
    new h.Input(), new h.Input({ type: "number" }), new h.Textarea(), select, option,
    new h.Element("DIV", { isContentEditable: true }),
  ]) {
    h.target(element);
    for (const text of [true, false]) {
      await assert.rejects(h.run(text), /boolean text requires a checkbox or radio/);
    }
    untouched(element);
  }
});

test("by label fails loudly on every non-select/option target before focus or mutation", async () => {
  const h = harness();
  for (const element of [
    new h.Input(), new h.Input({ type: "checkbox" }), new h.Input({ type: "radio" }),
    new h.Textarea(), new h.Element("DIV", { isContentEditable: true }), new h.Element("BUTTON"),
  ]) {
    h.target(element);
    await assert.rejects(h.run("label", { by: "label" }), /by label requires a select or option/);
    untouched(element);
  }
});

test("all controls reject disabled/readonly/inert/ARIA/detached states before mutation", async () => {
  for (const kind of ["text", "checkbox", "radio", "textarea", "select", "option", "contenteditable"]) {
    for (const blocked of [
      { disabled: true }, { nativeDisabled: true }, { readOnly: true },
      { attributes: { readonly: "" } }, { inert: true }, { attributes: { inert: "" } },
      { attributes: { "aria-disabled": " TRUE " } }, { attributes: { "aria-readonly": "true" } },
      { isConnected: false }, "inert-parent", "aria-parent", "readonly-parent", "shadow-host",
    ]) {
      const h = harness();
      let control, text = "a";
      if (kind === "select" || kind === "option") {
        const select = new h.Select(), option = select.add("a");
        control = kind === "select" ? select : option;
      } else if (kind === "textarea") control = new h.Textarea();
      else if (kind === "contenteditable") control = new h.Element("DIV", { isContentEditable: true });
      else {
        control = new h.Input({ type: kind });
        if (kind !== "text") text = true;
      }
      if (typeof blocked === "string") {
        const ancestor = new h.Element("DIV", {
          inert: blocked === "inert-parent" || blocked === "shadow-host",
          attributes: blocked === "aria-parent" ? { "aria-disabled": "true" }
            : blocked === "readonly-parent" ? { "aria-readonly": "true" } : {},
          parentElement: control.parentElement,
        });
        if (blocked === "shadow-host") { control.parentElement = null; control.root = { host: ancestor }; }
        else control.parentElement = ancestor;
      } else Object.assign(control, blocked);
      h.target(control);
      await assert.rejects(h.run(text), /disabled|read-only|readonly|inert|detached/, `${kind} ${JSON.stringify(blocked)}`);
      untouched(control);
    }
  }
});

test("native :disabled preserves first-legend exception instead of blanket fieldset ancestor denial", async () => {
  const h = harness();
  const fieldset = new h.Element("FIELDSET", { disabled: true });
  const legend = new h.Element("LEGEND", { parentElement: fieldset });
  const input = h.target(new h.Input({ parentElement: legend }));
  assert.equal((await h.run("allowed")).value, "allowed");
  input.nativeDisabled = true;
  await assert.rejects(h.run("blocked"), /disabled/);
  assert.equal(input.value, "allowed");
});

test("supported input kinds succeed only with exactly retained native values", async () => {
  const valid = {
    text: "hello", search: "search", tel: "+1 555 0000", url: "https://example.test/",
    email: "a@example.test", password: "secret", number: 12.5, range: 20,
    date: "2026-09-20", "datetime-local": "2026-09-20T10:30",
    month: "2026-09", week: "2026-W38", time: "10:30", color: "#abcdef",
  };
  const h = harness();
  for (const [type, text] of Object.entries(valid)) {
    const input = h.target(new h.Input({ type }));
    assert.equal((await h.run(text)).value, String(text), type);
    assert.deepEqual(eventTypes(input), ["input", "change"]);
  }
});

test("browser value sanitization/clamping/normalization never reports false success or emits input/change", async () => {
  const h = harness();
  for (const [type, text, properties] of [
    ["number", "not numeric"], ["number", "12x"],
    ["range", 101], ["range", -1], ["range", 3, { step: 2 }],
    ["date", "yesterday"], ["time", "25:00"],
    ["month", "2026-99"], ["week", "2026-W99"],
    ["datetime-local", "2026-09-20 10:30"], ["datetime-local", "2026-09-20T10:30:00"],
    ["color", "#ABCDEF"], ["color", "red"], ["text", "line\nbreak"],
    ["email", " a@example.test "], ["url", " https://example.test/ "],
  ]) {
    const input = h.target(new h.Input({ type, ...properties }));
    await assert.rejects(h.run(text, { submit: true }), /did not retain/, `${type} ${text}`);
    assert.equal(input.writes.length, 1);
    assert.deepEqual(input.events, []);
  }
  const textarea = h.target(new h.Textarea());
  await assert.rejects(h.run("line\r\nbreak"), /did not retain/);
  assert.deepEqual(textarea.events, []);
});

test("unsupported/file/button/hidden controls and arrays on non-multiple controls fail before focus", async () => {
  const h = harness();
  for (const type of ["file", "button", "submit", "reset", "image", "hidden", "future-type"]) {
    const input = h.target(new h.Input({ type }));
    await assert.rejects(h.run("x"), /unsupported input type/);
    untouched(input);
  }
  for (const tagName of ["BUTTON", "DIV", "PROGRESS", "METER", "OUTPUT"]) {
    const element = h.target(new h.Element(tagName));
    await assert.rejects(h.run("x"), /unsupported form control/);
    untouched(element);
  }
  for (const tagName of ["BUTTON", "FIELDSET", "PROGRESS", "METER", "OUTPUT"]) {
    const element = h.target(new h.Element(tagName, { isContentEditable: true }));
    await assert.rejects(h.run("x"), /unsupported form control/);
    untouched(element);
  }
  for (const element of [
    new h.Input(), new h.Textarea(), new h.Element("DIV", { isContentEditable: true }),
  ]) {
    h.target(element);
    await assert.rejects(h.run(["a"]), /multiple select target/);
    untouched(element);
  }
});

test("focus handlers cannot disable targets or change select options before mutation", async () => {
  const h = harness();
  for (const type of ["text", "checkbox"]) {
    const input = h.target(new h.Input({ type }));
    input.onFocus = () => { input.disabled = true; };
    await assert.rejects(h.run(type === "checkbox" ? true : "x"), /disabled/);
    assert.deepEqual(input.writes, []);
    assert.deepEqual(input.events, []);
  }
  for (const mutation of ["options", "value", "multiple", "disabled-option", "disabled-owner"]) {
    const select = h.target(new h.Select()), option = select.add("a");
    select.onFocus = () => {
      if (mutation === "options") select.add("b");
      else if (mutation === "value") option.value = "changed";
      else if (mutation === "multiple") select.multiple = true;
      else if (mutation === "disabled-option") option.disabled = true;
      else select.disabled = true;
    };
    await assert.rejects(h.run("a"), /options changed|disabled/);
    assert.deepEqual(option.writes, []);
    assert.deepEqual(select.events, []);
  }
});

test("changed input types or removed contenteditability cannot report success", async () => {
  for (const stage of ["focus", "input"]) {
    const h = harness();
    for (const kind of ["text", "checkbox", "contenteditable"]) {
      const element = h.target(kind === "contenteditable"
        ? new h.Element("DIV", { isContentEditable: true }) : new h.Input({ type: kind }));
      const mutate = () => {
        if (kind === "contenteditable") element.isContentEditable = false;
        else element.type = "file";
      };
      if (stage === "focus") element.onFocus = mutate;
      else element.onEvent = (event) => { if (event.type === stage) mutate(); };
      await assert.rejects(h.run(kind === "checkbox" ? true : "x"), /did not retain/);
      if (stage === "focus") assert.deepEqual(element.writes, []);
    }
  }
});

test("native setter rejection and event-handler resets are errors for values, checked, and selection", async () => {
  for (const stage of ["setter", "input", "change", "keyup"]) {
    const h = harness();
    for (const kind of ["text", "checkbox", "select", "contenteditable"]) {
      let control, target, text;
      if (kind === "select") {
        control = new h.Select();
        target = control.add("a");
        text = "a";
      } else if (kind === "contenteditable") {
        control = target = new h.Element("DIV", { isContentEditable: true });
        text = "a";
      } else {
        control = target = new h.Input({ type: kind });
        text = kind === "text" ? "a" : true;
      }
      if (stage === "setter") {
        if (kind === "contenteditable") continue;
        target.rejectSet = true;
      } else {
        control.onEvent = (event) => {
          if (event.type !== stage) return;
          if (kind === "select") target._selected = false;
          else if (kind === "checkbox") target._checked = false;
          else if (kind === "contenteditable") target.textContent = "rejected";
          else target._value = "rejected";
        };
      }
      h.target(control);
      await assert.rejects(h.run(text, { submit: true }), /did not retain/, `${kind} ${stage}`);
      if (stage === "setter") assert.deepEqual(control.events, []);
    }
  }
});

test("detached controls and changed option identities after events do not report success", async () => {
  const h = harness();
  for (const kind of ["text", "checkbox", "select"]) {
    const control = kind === "select" ? new h.Select() : new h.Input({ type: kind });
    if (kind === "select") control.add("a");
    control.onEvent = () => { control.isConnected = false; };
    h.target(control);
    await assert.rejects(h.run(kind === "checkbox" ? true : "a"), /did not retain/);
  }
  const select = h.target(new h.Select());
  select.add("a");
  select.onEvent = () => { select.options = [new h.Option({ value: "a", _selected: true })]; };
  await assert.rejects(h.run("a"), /did not retain/);
});

test("option value/label changes and newly ambiguous matches after events cannot silently succeed", async () => {
  for (const by of ["value", "label"]) {
    for (const mutation of ["requested-key", "ambiguous-key", "multiple"]) {
      const h = harness(), select = h.target(new h.Select());
      const option = select.add("a", "a"), other = select.add("b", "b");
      select.onEvent = (event) => {
        if (event.type !== "input") return;
        if (mutation === "multiple") select.multiple = true;
        else {
          const target = mutation === "requested-key" ? option : other;
          const next = mutation === "requested-key" ? "changed" : "a";
          if (by === "value") target.value = next;
          else target.textContent = next;
        }
      };
      await assert.rejects(h.run("a", { by }), /did not retain/);
    }
  }
});

test("argument validation rejects malformed types and nonfinite numbers before injection", async () => {
  const h = harness();
  const base = { tabId: 7, selector: "#target", text: "x" };
  const sparse = new Array(1);
  const invalid = [
    undefined, null, [], "input", {},
    ...[-1, 1.5, "7", null, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1].map((tabId) => ({ ...base, tabId })),
    ...["", " ", null, undefined, 2].map((selector) => ({ ...base, selector })),
    ...[undefined, null, {}, NaN, Infinity, -Infinity, ["a", 1], [["a"]], sparse, 1n, () => {}]
      .map((text) => ({ ...base, text })),
    ...[-1, 1.5, "0", null, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1].map((index) => ({ ...base, index })),
    ...["text", "Value", true, null].map((by) => ({ ...base, by })),
    ...["false", 0, 1, null].map((submit) => ({ ...base, submit })),
  ];
  for (const args of invalid) await assert.rejects(h.context.formInput(args));
  assert.equal(h.scripts.length, 0);
});

test("missing elements, invalid CSS and page exceptions return error envelopes, not empty success", async () => {
  const h = harness();
  await assert.rejects(h.run("x"), /no element/);
  assert.match(h.lastEnvelope.error, /no element/);
  await assert.rejects(h.run("x", { selector: "[" }), /invalid CSS/);
  assert.match(h.lastEnvelope.error, /invalid CSS/);
  const input = h.target(new h.Input({ setterError: "native setter exception" }));
  await assert.rejects(h.run("x"), /native setter exception/);
  assert.deepEqual(input.events, []);
  delete input.setterError;
  input.focus = () => { throw new Error("focus failed"); };
  await assert.rejects(h.run("x"), /focus failed/);
  input.focus = () => {};
  input.onEvent = () => { throw new Error("event failed"); };
  await assert.rejects(h.run("x"), /event failed/);
});

test("missing native accessors are operational errors rather than assignment fallbacks", async () => {
  const h = harness(), input = h.target(new h.Input());
  delete h.Input.prototype.value;
  await assert.rejects(h.run("x"), /native value accessor is unavailable/);
  untouched(input);
});

test("scripting rejections, absent executeScript results, and malformed envelopes fail closed", async () => {
  const h = harness();
  h.inject = () => { throw new Error("Cannot access contents of this URL"); };
  await assert.rejects(h.run("x"), /Cannot access/);
  for (const response of [
    [], [{}], [{ result: undefined }], [{ result: null }], [{ result: {} }],
    [{ result: { ok: true } }], [{ result: { ok: true, result: {} } }],
    [{ result: { ok: true, result: { selector: "#target", typed: 1 } } }],
    [{ result: { ok: true, result: { selector: "#other", typed: 1, value: "x" } } }],
    [{ result: { ok: true, result: { selector: "#target", checked: true } } }],
    [{ result: { ok: true, result: { selector: "#target", values: [1], value: "1", changed: true } } }],
  ]) {
    h.inject = () => response;
    await assert.rejects(h.run("x"), /no valid result/);
  }
  h.inject = () => [{ result: { error: "document navigated" } }];
  await assert.rejects(h.run("x"), /document navigated/);
});
