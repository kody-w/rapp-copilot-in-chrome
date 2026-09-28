---
name: rappter-chrome-local
description: Drive the user's real, logged-in Edge or Chrome through the vendorless local Rappter bridge.
---

# Rappter Chrome Local

Use this skill for browser work in the user's real authenticated Chromium
profile. It is not a headless browser.

## Start here

Always call `tabs_context_mcp` first. It returns the tab IDs required by the
other tools. Set `createIfEmpty: true` when an empty profile is acceptable.

Prefer `browser_batch` for ordered multi-step work.

## Tools

- `tabs_context_mcp`, `tabs_create_mcp`, `tabs_close_mcp`
- `navigate`
- `get_page_text`, `read_page` (existing text/CSS readers)
- `read_page_ax`, `find_elements` (native accessibility snapshot and role/name/text matching)
- `wait_for` (CSS presence), `assert_page` (visible, enabled, text-contains, url-matches)
- `form_input` (text/typed values, checkbox/radio booleans, select values or labels)
- `computer` (`click`, `type`, `activate`, `screenshot`)
- `input` (browser-level `click`, `hover`, `scroll`, `type`, `key`)
- `observe_start`, `observe_read`, `observe_stop` (`kind: console | network`)
- `javascript_tool`
- `browser_batch`
- `list_connected_browsers`

`computer` screenshots accept `fullPage: true` or a `region` with CSS-page
`x`, `y`, `width`, and `height`; optional `scale` controls CDP output size.
Screenshots are MCP image blocks, including inside `browser_batch`.
The default remains a visible-tab screenshot.

Accessibility references are scoped to a document and invalidate on navigation
or reload. `find_elements` is deterministic matching, not natural-language
understanding. Re-read after navigation; existing click/type tools still use CSS
selectors, not accessibility references.

`input` accepts viewport `{x,y}`, `{selector,index}`, or a DOM `{ref}` from AX.
Mouse actions require a target; keyboard actions optionally click a target to focus it.
Clicks accept `button: left | right | middle` and `clickCount: 1 | 2 | 3`.
Use `deltaX`/`deltaY` for scroll, `text` for type, and `key` plus optional
`modifiers: ["ctrl", "cmd", "shift", "alt"]` for keys (choose only needed modifiers).
Hidden/obscured elements and stale/virtual refs fail rather than clicking blindly.
`computer` remains the back-compatible DOM-click/synthetic-type path.

`form_input` accepts boolean `value` for checked state, strings/numbers for
typed fields, and strings (or arrays for multi-select) for options.
Set `by: "label"` to select by label instead of value; `index` chooses a CSS match.
Booleans require checkbox/radio; checkbox writes also clear mixed state.
`by: "label"` requires a select/option and uses case-sensitive normalized labels.
Its `submit` remains synthetic Enter; use `input` key Enter for browser-level input.

Observation is explicit and memory-only. Start before the action, read afterward,
and stop in cleanup to release the debugger lease. Console/network buffers have
count/byte limits and dropped counters; navigation resets events, and stop, tab
closure, debugger detach, or worker eviction discards state. Read preserves events
unless `clear: true`; stop returns a final snapshot before discarding.
Network headers and response bodies are absent by default. Set `includeHeaders`
or `includeBodies` deliberately on network start; `maxBodyBytes` is allowed only
with bodies enabled. Request bodies are never captured. URLs/logs may contain
secrets even without either flag; returned content goes to the MCP caller and its
retention policy. Do not enable sensitive capture speculatively.

Assertions return `passed: false` on unmet conditions and are marked as MCP
errors, including in batches. Inspect their structured results before acting.
`list_connected_browsers` reports the actual extension instance ID and profile
label; it does not discover every browser installed on the machine.

## Safety

This is the user's real browser and real authenticated identity.

- Confirm before sending, purchasing, publishing, deleting, or submitting.
- Read the page after an irreversible action and verify the result.
- Never treat a click without readback as proof of completion.
- Use `javascript_tool` only when the ordinary read/click/type tools cannot
  express the action.

## Google Voice

The local runtime also includes `gvoice.py` and `voice_assistant.py`.
Google Voice sends are account-locked and are only successful after the sent
text appears as an outgoing message in the thread.
