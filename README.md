# rappter-chrome

**Drive your real, logged-in Edge/Chrome from Python. No vendor in the chain.**

Your browser, your profile, your cookies, your authenticated sessions —
navigate, read, click, type, run JavaScript, screenshot. Zero dependencies,
zero accounts, zero native-messaging setup.

```bash
python3 bridge.py tabs
python3 gvoice.py send "Mom" "on my way"
```

---

## Why this exists

The existing route to browser control here was Anthropic's `claude-in-chrome`,
wrapped by [`rapp-copilot-in-chrome`](https://github.com/kody-w/rapp-copilot-in-chrome):

```
script -> claude binary -> native messaging host -> Anthropic extension -> tabs
```

Four dependencies to read one page, and on this machine it was broken at two of
them — no native-messaging manifest registered, and the extension answering:

> Browser extension is not connected. Please ensure the Claude browser
> extension is installed and running, **and that you are logged into claude.ai
> with the same account as Claude Code.**

So browser automation required a second vendor's account to be logged in and
healthy. Worse, the vendor's own `doctor` reported:

```
[ok] Chrome reachable (live round trip) -- tab group reachable
```

…while that was the state. The refusal comes back as an ordinary text response
with no error flag, so a check that only looks for `isError` reads a refusal as
a success. A false green in the one check that was supposed to prove the whole
chain worked.

This replaces the chain entirely:

```
script -> localhost WebSocket -> our extension -> tabs
```

| | claude-in-chrome | rappter-chrome |
|---|---|---|
| Vendor binary | Claude Code required | none |
| Vendor account | must be logged into claude.ai | none |
| Native messaging host | manifest must be registered | none |
| Chrome restart | on host changes | never |
| Python dependencies | — | none (stdlib) |
| Direction | Chrome spawns the host | extension dials out |

**The extension dials out.** That single inversion removes the manifest, the
restart, and the vendor binary at once: there is nothing to register, because
nothing needs to spawn us.

## Install

1. **Load the extension** — `edge://extensions` (or `chrome://extensions`) →
   enable **Developer mode** → **Load unpacked** → select `extension/`.
2. **Get your token:**
   ```bash
   python3 bridge.py token
   ```
3. **Paste it into the extension popup** (click the toolbar icon), keep port
   `8777`, hit **Save & connect**. It should read `waiting for a local server…`.
4. **Verify:**
   ```bash
   python3 bridge.py tabs
   ```

The popup shows `connected` only while a server is actually running — it is a
live indicator, not a saved setting.

## Security

This drives your **real** browser with your **real** sessions. Anything it
clicks, sends, or deletes happens **as you**. Three guards, each closing a
specific hole:

| Guard | Stops |
|---|---|
| Binds `127.0.0.1` only | anything off-box |
| Shared token, `compare_digest` | local processes that don't have the token file (`0600`) |
| `Origin` must be `chrome-extension://` | **a web page you visit guessing the port and driving your browser** |

That third one is the important one and is easy to miss. A page cannot forge
its `Origin` — Chrome sets it — so `https://evil.example.com` is refused at the
HTTP layer before the socket upgrades. All three are covered by tests:

```
3. A WEB PAGE CANNOT DRIVE YOUR BROWSER
  PASS  rejected at HTTP layer  — HTTP/1.1 403 Forbidden
  PASS  server refused the origin
4. WRONG TOKEN IS REFUSED
  PASS  rejected at HTTP layer  — HTTP/1.1 401 Unauthorized
```

An empty token means the bridge is **off**. It fails closed.

## Commands

```bash
python3 bridge.py token                       # print/create the shared token
python3 bridge.py tabs                        # list open tabs
python3 bridge.py open https://example.com    # returns a tabId (reuses same-host tab)
python3 bridge.py text  <tabId>               # readable page text
python3 bridge.py query <tabId> "a.link"      # matching elements + text
python3 bridge.py click <tabId> "button.send"
python3 bridge.py type  <tabId> "input#q" "hello" --submit
python3 bridge.py eval  <tabId> "document.title"
```

```python
from bridge import Chrome
with Chrome() as c:
    tab = c.open("https://news.ycombinator.com")
    for row in c.query(tab, "span.titleline a", limit=10):
        print(row["text"])
```

### Verbs

| Group | Verbs |
|---|---|
| Tabs | `tabs` `find_tab` `create` `close_tab` `activate` `open` |
| Navigation | `navigate` |
| Reading | `text` `html` `query` `screenshot` |
| Interaction | `click` `type` `waitfor` |
| Scripting | `eval` |
| Orchestration | `batch` |

### Two implementation notes worth knowing

**Ordinary verbs do not attach the debugger.** `click`, `type`, `text` and
`query` run as *declared* functions through `chrome.scripting`. MV3 forbids
`unsafe-eval` in the extension, and most hardened sites forbid it in the page —
so a design that turns every action into an eval'd string breaks on exactly the
logged-in sites worth automating. Declared functions sidestep both, and no
"being debugged" banner appears for normal work.

**`eval` uses `chrome.debugger`.** Arbitrary JS is the one thing declared
functions cannot express. `Runtime.evaluate` runs outside the page's CSP, so it
works anywhere; the debugger attaches for that one call and detaches after.

**`type` uses the native value setter.** React and similar frameworks listen
for it rather than for `.value =`. Without that, text appears in the box, the
app never sees it, and the form submits empty — a failure that looks like
success in a screenshot.

## Google Voice

`gvoice.py` is the reason this was built: Google Voice has no public API for
personal accounts, and the supported answer is "use the web app". So it uses
the web app, in your browser, already logged in. No credentials are stored and
no OAuth app is registered.

```bash
python3 gvoice.py probe             # what the page looks like RIGHT NOW
python3 gvoice.py threads
python3 gvoice.py read "Mom"
python3 gvoice.py send "Mom" "on my way"
python3 gvoice.py unread
```

Google ships obfuscated class names that change without notice, so `SELECTORS`
is a **list of candidates per target**, tried in order, preferring stable
attributes (`aria-label`, `role`) over class soup. When Google changes the DOM,
`probe` prints what each candidate matches right now — re-tuning is editing one
list, not archaeology.

`send` re-reads the thread afterwards and reports `verified: false` if the
message did not appear. A send that reports success because a click did not
throw is the same mistake as trusting an exit code.

### Persistent chat with Copilot

`voice_assistant.py` locks both ends of the conversation:

- Google account must match `google_voice_account`; mismatch is a hard failure.
- Inbound number must match `google_voice_peer`; every other sender is ignored.

The state transition is deliberately ordered:

```
read inbound -> ask Copilot -> send -> verify in thread -> mark handled
```

Generation or delivery failure leaves the message unhandled for a later retry.
First startup watermarks history and replies to nobody; `--reply-latest` is the
explicit one-time adoption path.

```bash
python3 voice_assistant.py --reply-latest
python3 voice_assistant.py --loop --interval 60
```

The LaunchAgent template uses a resident `KeepAlive` loop, not `StartInterval`;
interval jobs on this host remain silently pended and never spawn.

## Tests

```bash
python3 test_bridge.py          # 17 RFC6455/security/concurrency checks
python3 test_mcp.py             # JSON-RPC recovery, 11 tools, batch translations
python3 test_gvoice.py          # cold start and stale-thread refusal
python3 test_voice_assistant.py # 27 injection, watermark, identity assertions
python3 test_install_local.py   # config, concurrency, and rollback safety
```

Covers the parts that cannot be checked by loading the extension: handshake
maths, frame encode/decode at every length class (including the 64-bit path
with a 70KB payload), masking, ping/pong, both security guards, and error
propagation. No Chrome required — a fake client speaks real RFC6455.

```
17 bridge checks, 11 MCP tools plus malformed-byte recovery,
2 Google Voice regressions, 27 assistant assertions, and transactional
installer concurrency/rollback protection.
```

## Layout

```
extension/manifest.json     MV3 manifest
extension/background.js     service worker — WebSocket client + command dispatch
extension/popup.html/.js    token, port, live connection status
bridge.py                   stdlib WebSocket server + Python API + CLI
gvoice.py                   Google Voice, built on bridge.py
voice_assistant.py          persistent, verified Copilot SMS loop
rappter_chrome_mcp.py       vendorless stdio MCP server
install_local.py/.sh        portable installer
test_*.py                   protocol, MCP, Voice, and installer regressions
```

## License

MIT
