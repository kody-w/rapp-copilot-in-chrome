---
name: "rapp-copilot-in-chrome"
description: |
  Drive the user's real, logged-in Chrome browser from GitHub Copilot CLI — navigate, click, type,
  screenshot, read the accessibility tree or page text, run JavaScript, and inspect console and
  network traffic. Use when the user asks to open or browse a site, click through a web UI, fill a
  form, scrape or read a page, debug a web app in the browser, take a screenshot of a page, or
  automate anything in Chrome. Also use for "rapp copilot in chrome", "browser automation", "drive
  chrome", "open a tab", "check the console", or to install, verify, or diagnose the bridge itself.
---

# rapp-copilot-in-chrome

Browser automation against the browser you **actually use** — real profile, live cookies,
authenticated sessions. Not a throwaway headless instance.

## Architecture

```
Copilot CLI (MCP client)
  -> ~/.copilot/bin/rapp-copilot-in-chrome        (launcher shim)
  -> claude --claude-in-chrome-mcp                (self-contained stdio MCP server)
  -> native host com.anthropic.claude_code_browser_extension
  -> Chrome extension fcoeoabgfenejglbffodgkkbkcdhcgfn
  -> live tabs
```

The MCP server is self-contained: it does **not** require a Claude Code session to be running. It
needs only the Claude binary (which hosts the bridge) plus the Chrome extension installed and
connected. That is the whole trick — the bridge was always a plain stdio MCP server, so any MCP
client can drive it.

## Run this — do not improvise

Setup and health checks have a **deterministic implementation** shipped next to this file as
`rapp_copilot_in_chrome_agent.py`. It is stdlib-only Python with no install step. When asked to
install, verify, or diagnose the bridge, execute it and use its output verbatim — do not guess at
the state of the system:

```bash
python3 rapp_copilot_in_chrome_agent.py '{"action": "<action>"}'   # doctor | status | install | uninstall
python3 rapp_copilot_in_chrome_agent.py '{"action": "doctor"}'     # full check + live Chrome round trip
python3 rapp_copilot_in_chrome_agent.py '{"action": "status"}'     # fast check, no browser traffic
python3 rapp_copilot_in_chrome_agent.py '{"action": "install"}'    # launcher + MCP registration + skill
python3 rapp_copilot_in_chrome_agent.py --tool                     # emit the JSON tool contract
```

`doctor` verifies all seven links in the chain — Claude binary, native-host manifest, Chrome
extension, launcher, MCP registration, MCP handshake, and a live round trip that reaches a real tab.

## Loading the tools (do this first)

The 22 browser tools arrive **deferred** in Copilot CLI. Load them with a single tool-search call,
batching every tool you expect to need — each extra search costs a round trip:

```
tool_search_tool  pattern: "rapp-copilot-in-chrome"
```

Or target a subset: `"tabs_context_mcp|browser_batch|navigate|read_page|computer"`.

## The two rules that matter

1. **Always call `tabs_context_mcp` with `createIfEmpty: true` first.** Nearly every other tool
   requires a `tabId`, and tabs only exist inside this session's tab group. Skip it and you get
   `No tab available`.
2. **Prefer `browser_batch` over single calls.** It runs a whole sequence in ONE round trip,
   executing sequentially and stopping on the first error. Batch whenever you can predict two or
   more steps ahead.

```jsonc
// 1. get a tabId
tabs_context_mcp { "createIfEmpty": true }
// -> {"availableTabs":[{"tabId":1363872857,...}],"tabGroupId":249531617}

// 2. batch the rest
browser_batch {
  "actions": [
    { "name": "navigate",      "input": { "url": "https://example.com", "tabId": 1363872857 } },
    { "name": "get_page_text", "input": { "tabId": 1363872857 } }
  ]
}
```

## Tools (22)

**Tabs** — `tabs_context_mcp` (list/create group), `tabs_create_mcp`, `tabs_close_mcp`

**Navigation** — `navigate` (url, or back/forward), `resize_window`

**Reading** — `get_page_text` (article text; best for reading), `read_page` (accessibility tree with
`ref_id`s; supports `filter: interactive`, `depth`, `max_chars`), `find` (natural-language element
lookup returning refs)

**Interaction** — `computer` (screenshot, click, type, key, scroll, drag by coordinate),
`form_input` (set a value by `ref`), `file_upload` (by `ref` — never click a file input, it opens a
native dialog you cannot see), `upload_image`

**Scripting** — `javascript_tool` (REPL semantics in page context: top-level `await` works and the
last expression is returned — do not write `return`)

**Debugging** — `read_console_messages` (`pattern` filter), `read_network_requests` (`urlPattern`
filter)

**Orchestration** — `browser_batch`

**Recording** — `gif_creator` (start/stop/export an animated GIF of the session)

**Shortcuts** — `shortcuts_list`, `shortcuts_execute`

**Browser selection** — `list_connected_browsers`, `select_browser` (by deviceId), `switch_browser`
(broadcast a pairing request, wait for the user to click Connect)

> **Network tracking is lazy.** `read_network_requests` only begins recording the first time it is
> called on a tab, so a page that already loaded shows nothing. Call it once *before* navigating or
> triggering the requests you want, then call it again to read them. Requests also clear when the
> tab navigates to a different domain.

## Choosing a reading tool

- Reading an article, docs page, or any prose -> `get_page_text`
- Need to click or fill something -> `find` (natural language) or `read_page` with
  `filter: "interactive"` to get `ref`s
- Need pixel coordinates, or the page is canvas/visual -> `computer` screenshot first
- `read_page` truncates at 50k chars; narrow with `ref_id` or `depth` rather than raising
  `max_chars` blindly

## Safety

This drives the user's **actual** browser with their **real** authenticated sessions. Anything it
clicks, submits, purchases, sends, or deletes happens as the user. Confirm before any destructive or
irreversible action — sending messages, submitting payments, deleting data, changing account
settings. Prefer opening a fresh tab per task rather than reusing tabs the user is actively working
in.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `No tab available` | Call `tabs_context_mcp { createIfEmpty: true }` first, then pass `tabId`. |
| `Cannot access a chrome:// URL` | New tabs start on `chrome://newtab`; navigate somewhere real first. |
| `read_network_requests` empty | Tracking starts on first call — call it, then navigate/act, then read. |
| Tools not listed | They are deferred — run the tool search above. |
| Server missing | `copilot mcp get rapp-copilot-in-chrome` should report `Status: Enabled`. |
| Cannot locate `claude` | Set `RAPP_CHROME_CLAUDE_BIN` to its absolute path. |
| Browser not responding | Extension may be disconnected — `list_connected_browsers`, then `select_browser` or `switch_browser`. |

Run `python3 rapp_copilot_in_chrome_agent.py '{"action": "doctor"}'` before debugging by hand — it
names the exact broken link in the chain.

<!-- toaster:generated:begin -->

## Parameters

The typed contract this capability answers to (JSON Schema — the deterministic layer):

```json
{
  "type": "object",
  "properties": {
    "action": {
      "type": "string",
      "description": "Derived from `<action>` used in the documented command at line 30."
    }
  },
  "required": []
}
```

<!-- toaster:generated:end -->

<!-- toaster:generated:begin -->

## Deterministic steps

Lifted verbatim from the procedure above by `toaster.py toast`. Run them in order, substituting the typed parameters; do not paraphrase:

```bash
python3 rapp_copilot_in_chrome_agent.py '{"action": "doctor"}'
python3 rapp_copilot_in_chrome_agent.py '{"action": "<action>"}'   # doctor | status | install | uninstall
python3 rapp_copilot_in_chrome_agent.py '{"action": "doctor"}'     # full check + live Chrome round trip
python3 rapp_copilot_in_chrome_agent.py '{"action": "status"}'     # fast check, no browser traffic
python3 rapp_copilot_in_chrome_agent.py '{"action": "install"}'    # launcher + MCP registration + skill
python3 rapp_copilot_in_chrome_agent.py --tool                     # emit the JSON tool contract
```

<!-- toaster:generated:end -->

<!-- rci-capsule:v1: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 -->
