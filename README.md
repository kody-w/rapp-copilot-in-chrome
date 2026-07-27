# rapp-copilot-in-chrome

**Drive your real, logged-in Chrome from GitHub Copilot CLI.**

Not a headless throwaway browser — *your* browser, with your profile, your cookies, and your
authenticated sessions. Navigate, click, type, screenshot, read the accessibility tree, run
JavaScript, and inspect console and network traffic, all from Copilot CLI.

```bash
curl -fsSL https://raw.githubusercontent.com/kody-w/rapp-copilot-in-chrome/main/install.sh | sh
```

Then restart Copilot CLI and ask for browser work.

---

## What this is

Claude Code ships a browser bridge it calls `claude-in-chrome`. It is not a published MCP package
and it is not documented — it is wired into the Claude binary. This repo is the result of reverse
engineering it, plus the glue to use it from **any** MCP client.

The finding that makes it portable: the bridge is a **plain stdio MCP server** exposed by a hidden
flag, and it does **not** require a Claude Code session to be running.

```
claude --claude-in-chrome-mcp
```

Full chain:

```
Copilot CLI (MCP client)
  -> ~/.copilot/bin/rapp-copilot-in-chrome        (launcher shim)
  -> claude --claude-in-chrome-mcp                (self-contained stdio MCP server)
  -> native host com.anthropic.claude_code_browser_extension
  -> Chrome extension fcoeoabgfenejglbffodgkkbkcdhcgfn
  -> live tabs
```

Chrome's native-messaging manifest lives at
`~/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.anthropic.claude_code_browser_extension.json`
and points at a shim that execs the Claude binary with `--chrome-native-host`. That is the extension
side. The `--claude-in-chrome-mcp` side is the client side, and it is the one worth borrowing.

## How it was found

1. `~/.claude/chrome/chrome-native-host` is a 3-line shim: `exec "<claude binary>" --chrome-native-host`.
2. The native-messaging manifest names the extension ID, confirming the transport.
3. `strings` on the Claude binary surfaces `mcp__claude-in-chrome__*` tool names and, crucially, a
   second flag — `--claude-in-chrome-mcp` — sitting next to the literal `stdio`.
4. Speaking JSON-RPC to that flag returns `serverInfo: {"name": "Claude in Chrome"}` and 22 tools.
5. Calling `tabs_context_mcp` drives real Chrome with no Claude Code session anywhere.

## Requirements

- **Claude Code** installed (it hosts the bridge binary). The bridge does not need to be *running*.
- The **Claude in Chrome** extension installed and connected.
- **Copilot CLI**, and **Python 3.9+** for the installer.
- macOS or Linux.

If your Claude binary is somewhere unusual, set `RAPP_CHROME_CLAUDE_BIN` to its absolute path.

## Verify it

```bash
python3 rapp_copilot_in_chrome_agent.py '{"action": "doctor"}'
```

`doctor` checks all seven links in the chain and finishes with a live round trip into a real tab:

```
[ok] claude binary (hosts the bridge) -- /Users/you/.local/bin/claude
[ok] native messaging host manifest -- Chrome, Chromium, Brave
[ok] Chrome extension fcoeoabgfenejglbffodgkkbkcdhcgfn -- installed
[ok] launcher /Users/you/.copilot/bin/rapp-copilot-in-chrome -- executable
[ok] MCP server registered -- registered in /Users/you/.copilot/mcp-config.json
[ok] bridge answers MCP -- 22 tools
[ok] Chrome reachable (live round trip) -- tab group reachable
```

Other actions: `status` (fast, no browser traffic), `install`, `uninstall`.

## The 22 tools

| Group | Tools |
| --- | --- |
| Tabs | `tabs_context_mcp`, `tabs_create_mcp`, `tabs_close_mcp` |
| Navigation | `navigate`, `resize_window` |
| Reading | `get_page_text`, `read_page`, `find` |
| Interaction | `computer`, `form_input`, `file_upload`, `upload_image` |
| Scripting | `javascript_tool` |
| Debugging | `read_console_messages`, `read_network_requests` |
| Orchestration | `browser_batch` |
| Recording | `gif_creator` |
| Shortcuts | `shortcuts_list`, `shortcuts_execute` |
| Browser selection | `list_connected_browsers`, `select_browser`, `switch_browser` |

Full JSON Schemas: [`docs/tools.json`](docs/tools.json).

## Two rules that will save you

1. **Call `tabs_context_mcp { createIfEmpty: true }` first.** Almost everything else needs a `tabId`,
   and tabs only exist inside the session's tab group. Skip it and you get `No tab available`.
2. **Prefer `browser_batch`.** One round trip for a whole sequence, executed in order, stopping on
   the first error.

```jsonc
tabs_context_mcp { "createIfEmpty": true }
// -> {"availableTabs":[{"tabId":1363872857,...}],"tabGroupId":249531617}

browser_batch {
  "actions": [
    { "name": "navigate",      "input": { "url": "https://example.com", "tabId": 1363872857 } },
    { "name": "get_page_text", "input": { "tabId": 1363872857 } }
  ]
}
```

One more, learned the hard way: **`read_network_requests` starts recording on first call.** A page
that already loaded shows nothing. Call it, *then* navigate, then read.

In Copilot CLI the tools arrive **deferred** — load them with one tool search for
`rapp-copilot-in-chrome` rather than one call per tool.

## The skill is toasted

[`SKILL.md`](SKILL.md) is [toasted](https://kody-w.github.io/rapp-toaster/): it carries an RCI
capsule as an HTML comment, so it round-trips byte-exact between `SKILL.md`, `agent.py`, openclaw,
and openrappter without drift. No frontmatter field is added or required, and hosts that ignore the
capsule lose nothing — it is a valid `SKILL.md` everywhere.

```
$ toaster.py soak SKILL.md --depth 3 --cycles 25
  ok   SKILL.md   40 routes x depth<=3 + 25 cycles  -> CLEAN
198 conversions across 1 artifact(s)
NO DRIFT — path-independent, idempotent, and fixed-point stable in every direction.
```

`rapp_copilot_in_chrome_agent.py` is toasted too, and soaks clean over 188 conversions.

## Layout

```
SKILL.md                          toasted skill — the browser-usage guide
rapp_copilot_in_chrome_agent.py   toasted agent — install / status / doctor / uninstall
bin/rapp-copilot-in-chrome        launcher shim installed into ~/.copilot/bin
docs/tools.json                   JSON Schemas for all 22 tools
install.sh                        one-liner installer
```

## Manual install

```jsonc
// ~/.copilot/mcp-config.json
{
  "mcpServers": {
    "rapp-copilot-in-chrome": {
      "type": "local",
      "command": "/Users/you/.copilot/bin/rapp-copilot-in-chrome",
      "args": [],
      "tools": ["*"]
    }
  }
}
```

Copy `bin/rapp-copilot-in-chrome` there and `chmod +x` it, and copy `SKILL.md` plus the agent to
`~/.copilot/skills/rapp-copilot-in-chrome/`. Or just run `install.sh`, which does exactly this.

Nothing here is Copilot-specific except the config path — the same launcher works from any MCP
client.

## Uninstall

```bash
python3 rapp_copilot_in_chrome_agent.py '{"action": "uninstall"}'
```

## ⚠️ Read this part

This drives your **actual** browser with your **real** authenticated sessions. Anything it clicks,
submits, purchases, sends, or deletes happens **as you**. The skill instructs the model to confirm
before destructive or irreversible actions, but that is a guardrail, not a sandbox. Treat it with
the same care as handing someone your unlocked laptop.

## Related

- [rapp-toaster](https://github.com/kody-w/rapp-toaster) — the zero-fidelity-loss format shim

## License

MIT
