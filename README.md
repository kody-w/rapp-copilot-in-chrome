# rapp-copilot-in-chrome

**Drive your real, logged-in Edge/Chrome from GitHub Copilot CLI.**

Not a headless throwaway browser — *your* browser, with your profile, your cookies, and your
authenticated sessions. Navigate, click, type, screenshot, read the accessibility tree, run
JavaScript, and inspect console and network traffic, all from Copilot CLI.

Recommended — local extension, local stdio MCP, no vendor account:

```bash
curl -fsSL https://raw.githubusercontent.com/kody-w/rapp-copilot-in-chrome/main/install-local.sh | sh
```

The reverse-engineered Claude bridge remains available as a compatibility backend:

```bash
curl -fsSL https://raw.githubusercontent.com/kody-w/rapp-copilot-in-chrome/main/install.sh | sh
```

---

## Vendorless local bridge (recommended)

The original reverse engineering proved the useful boundary was a stdio MCP server. The next step
was removing the vendor from both sides of it:

```
Copilot CLI
  -> ~/.rappter-chrome/runtime/rappter_chrome_mcp.py
  -> localhost WebSocket (127.0.0.1 only)
  -> unpacked MV3 extension
  -> your real Edge/Chrome tabs
```

The extension **dials out**. That inversion removes the native-messaging manifest, browser restart,
Claude binary, and claude.ai login in one move: nothing needs to spawn a native host.

| | Local bridge | Claude compatibility bridge |
| --- | --- | --- |
| Vendor binary | none | Claude Code |
| Vendor account | none | matching claude.ai login |
| Native messaging manifest | none | required |
| Python packages | none (stdlib) | none |
| Browser | Edge or Chrome | Claude extension's supported browsers |

### Install

The installer copies a portable runtime and extension to `~/.rappter-chrome`, generates a
machine-local token, registers `rappter-chrome-local` in Copilot's MCP config, and opens the
extensions page.

1. Open `edge://extensions/` or `chrome://extensions/`, then enable
   **Developer mode**.
2. Choose **Load unpacked** and select `~/.rappter-chrome/extension`.
3. Open the extension popup, paste the token printed by the installer, and
   click **Save & connect**.
4. Restart Copilot CLI.

The popup says **waiting for a local server** while idle. That is healthy: the MCP server is
one-shot and exists only while Copilot is using browser tools.

Upgrades are transactional: runtime, extension, skill, and MCP config are staged and journaled,
then swapped atomically. An interrupted process or power loss restores the coherent previous
generation on the next installer run. An already configured extension reloads itself, and an
active Voice service is stopped before the swap and restarted afterwards.
The installed runtime also carries the installer entry points, extension/skill
source, and regression tests, so diagnostics and future self-upgrades do not
depend on the original clone or curl temporary directory still existing.

Each browser profile receives a persistent instance ID, visible in the popup and with
`python3 ~/.rappter-chrome/runtime/bridge.py identity`. Set `browser_instance` in
`~/.rappter-chrome/config.json` on
multi-profile machines; every other profile is rejected instead of racing whichever connects
first.

### The 11 local tools

`tabs_context_mcp`, `tabs_create_mcp`, `tabs_close_mcp`, `navigate`, `get_page_text`,
`read_page`, `form_input`, `computer`, `javascript_tool`, `browser_batch`, and
`list_connected_browsers`.

They preserve the core names from the compatibility bridge, so existing browser prompts need
little or no adaptation. Ordinary click/type/read operations use declared functions through
`chrome.scripting`; arbitrary JavaScript attaches `chrome.debugger` only for that call and detaches
afterwards.

### Security boundary

This drives a real authenticated profile. Three independent guards protect the socket:

- binds `127.0.0.1`, never `0.0.0.0`;
- a random 192-bit token stored mode `0600`;
- the WebSocket `Origin` must begin `chrome-extension://`.

A web page that guesses the port receives `403 Forbidden` before the WebSocket upgrade. The token
is never placed in the URL or sent on the wire: client and server prove possession with fresh
nonces and directional HMAC-SHA256 proofs. A hostile local process can occupy the port and cause
denial of service, but cannot authenticate as the server or issue browser commands.

### Google Voice transport for a hatched twin

`gvoice.py` operates the signed-in Google Voice web app and confirms sent text by reading it back
as an outgoing message. `voice_assistant.py` locks both the Google account and peer number through
`~/.rappter-chrome/config.json`, watermarks existing history on first run, and uses:

```
observe inbound -> reserve stable twin turn -> Brainstem agents -> persist result
                -> prepare outbound -> attempt once -> readback -> mark handled
```

The browser is transport only—there is no handwritten conversational command
grammar. Every inbound turn goes to one durable curated Brainstem twin. Asking
for Hacker News follows the normal
`brainstem.py -> HackerNewsAgent.perform() -> result` loop; the transport never
returns a promise that another process will act later.

The owner-private hatch loads VoiceTwin memory, HackerNews, and bounded
text/encoding utilities. Shell, package installation, dynamic agent creation,
filesystem editing, unrestricted browser/messaging, and autonomous
Copilot/Claude dispatch are absent. The broker selects this roster before model
execution. Transport, principal, conversation, audience, and binding IDs are
HMAC-derived; raw account/peer values never enter logical session keys or logs.

Stable turn IDs and a durable twin journal prevent agent replay. If a send is
ambiguous, its state becomes `unknown`: recovery performs readback only and
never resends automatically. State uses atomic temp files, directory fsync, a
known-good backup, and a process lock.

Each installation mints one canonical RAPP/1 rappid, appends exact eleven-key
`memory.chat-turn` frames, and can build a deterministic `rapp/1-egg`
`rapplication` with one root `agent.py`:

```bash
python3 ~/.rappter-chrome/runtime/build_voice_twin_egg.py
```

The pinned rev-5 reference is byte-identical to
`kody-w/rapp-1@6723c7a`. The implementation reports
`structural-pre-acceptance`, not full/authenticated RAPP/1 conformance:
registered genesis/trust, exact section-8 facade acceptance, and a Google Voice
transport profile remain open at
<https://github.com/kody-w/rapp-1/issues/4>. See
`VOICE_TWIN_CONFORMANCE.json`.

Google Voice web remains the single text leader. The optional Android adapter
is Wi-Fi/VoIP-only and owns no text delivery or automated calling. Future
WhatsApp/Discord adapters must implement the same observe/claim/process and
prepare/attempt/reconcile states; adapters never chat, grant authority, share
credentials, or retry ambiguous effects.

Example machine-local config (keep it mode `0600`):

```json
{
  "browser_instance": "copy from: python3 ~/.rappter-chrome/runtime/bridge.py identity",
  "google_voice_account": "account@example.com",
  "google_voice_url": "https://voice.google.com/u/1/messages",
  "google_voice_peer": "5558675309",
  "google_voice_owner": "Owner",
  "google_voice_model": "gpt-5.6-sol",
  "brainstem_root": "/absolute/path/to/.brainstem",
  "rapp_owner": "github-login",
  "voice_twin_slug": "voice-twin",
  "voice_twin_timeout_seconds": 240,
  "max_replies_per_hour": 6
}
```

macOS uses the included LaunchAgent template. Linux installs include a systemd user-service
template; active services are detected and restarted during upgrades.

### Verify

```bash
cd ~/.rappter-chrome/runtime
python3 test_bridge.py           # 19 protocol/security/profile checks
python3 test_mcp.py              # JSON-RPC recovery, 11 tools, batch translations
python3 test_gvoice.py           # cold start and stale-thread refusal
python3 test_voice_assistant.py  # crash, injection, identity assertions
python3 test_voice_twin.py       # RAPP/1 identity, frames, hatch, replay
python3 test_install_local.py    # config, concurrency, rollback, SIGKILL recovery
```

The protocol suite covers 64-bit WebSocket frames with a 70KB payload, ping/pong, masking,
pre-header disconnects, malformed JSON/UTF-8 recovery, hostile web origins, HMAC authentication,
and deterministic profile selection. Voice tests exercise inboxes larger than 500 messages,
same-time duplicate text, send/crash recovery, corrupt-state recovery, prompt/control injection,
and unsafe action-claim filtering. Installer tests exercise concurrent processes, rollback after
an injected failure, and journal recovery after SIGKILL.

## Claude compatibility bridge

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
install.sh                        Claude compatibility installer
install-local.sh                  vendorless installer
install_local.py                  portable local installer
extension/                        vendorless MV3 extension
bridge.py                         zero-dependency localhost WebSocket transport
rappter_chrome_mcp.py             11-tool stdio MCP server
gvoice.py                         account-locked Google Voice browser driver
voice_assistant.py                exactly-once Google Voice transport loop
voice_twin.py                     durable curated Brainstem twin runtime
voice_twin_agent.py               single-file rapplication agent of record
rapp1.py                          pinned rev-5 identity/frame/egg reference
build_voice_twin_egg.py           deterministic RAPP/1 rapplication hatcher
VOICE_TWIN_CONFORMANCE.json       explicit pre-acceptance boundary
com.rapp.voice-assistant.plist.template  macOS resident service
rappter-voice-assistant.service.template Linux user service
test_bridge.py                    protocol and security tests
test_mcp.py                       MCP protocol smoke test
test_gvoice.py                    browser cold-start and DOM-settle tests
test_voice_assistant.py           message-loop safety tests
test_voice_twin.py                twin, RAPP/1, and RAPP Messaging tests
test_install_local.py             config, concurrency, and rollback tests
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
