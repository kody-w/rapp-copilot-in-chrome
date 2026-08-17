#!/usr/bin/env python3
"""gvoice.py — read and send Google Voice messages from a script.

    python3 gvoice.py probe            # what the page actually looks like NOW
    python3 gvoice.py threads          # conversation list
    python3 gvoice.py read "Mom"       # newest messages in one thread
    python3 gvoice.py send "Mom" "on my way"
    python3 gvoice.py unread           # threads with unread messages

Google Voice has no public API for personal accounts. The supported answer is
"use the web app", so this uses the web app — in your real browser, already
logged in, with no credentials stored anywhere and no OAuth app to register.

────────────────────────────────────────────────────────────────────────────
ABOUT THE SELECTORS

Google ships obfuscated class names that change without notice, so anything
here keyed on `.XyZ123` would be broken by a deploy nobody told us about. Two
defences:

  * SELECTORS below is a LIST of candidates per target, tried in order, and
    prefers stable attributes (aria-label, role, data-e2e) over class soup.
  * `probe` prints what each candidate actually matches right now. When Google
    changes the DOM, re-tuning is reading one command's output and editing one
    list — not archaeology through automation code.

A selector that silently matches nothing is the failure mode here, so every
function raises when a target is missing rather than returning empty and
letting a caller conclude you have no messages.
"""

import json
import os
import re
import secrets
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge import Chrome, BridgeError          # noqa: E402

MESSAGES_URL = "https://voice.google.com/u/0/messages"
CONFIG_FILE = Path.home() / ".rappter-chrome" / "config.json"

SELECTORS = {
    # The conversation list on the left.
    "thread_list": [
        "gv-message-thread-list-item",
        "gv-thread-list-item",
        "gv-thread-item",
        "[gv-id=thread-list] [role=listitem]",
        "[role=list] [role=listitem]",
    ],
    # The message bubbles inside an open conversation.
    "bubbles": [
        "gv-message-item",
        "[gv-test-id=message-item]",
        "[role=log] [role=listitem]",
        "div[data-e2e-message-bubble]",
    ],
    # The compose box.
    "compose": [
        "textarea[aria-label*='message' i]",
        "textarea[placeholder*='message' i]",
        "div[contenteditable=true][aria-label*='message' i]",
        "textarea",
    ],
    "send_button": [
        "button[aria-label*='send' i]",
        "[role=button][aria-label*='send' i]",
    ],
    "search": [
        "input[aria-label*='search' i]",
        "input[placeholder*='search' i]",
    ],
}


def trusted_voice_url(value):
    try:
        parsed = urllib.parse.urlsplit(str(value or ""))
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "voice.google.com"
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and re.fullmatch(r"/u/\d+/messages/?", parsed.path) is not None
    )


def require_voice_url(value, label="Google Voice URL"):
    if not trusted_voice_url(value):
        raise BridgeError(f"{label} is not the exact https://voice.google.com origin")
    return urllib.parse.urlsplit(value)


def canonical_peer_number(value):
    raw = str(value or "").strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+") and 8 <= len(digits) <= 15:
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    raise BridgeError(
        "Google Voice peer must be E.164 (+country...) or a US 10-digit number"
    )


def tab_url(c, tab):
    value = next(
        (item["url"] for item in c.tabs() if item["tabId"] == tab),
        "",
    )
    require_voice_url(value, "current Google Voice tab")
    return value


def config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def expected_account():
    return (
        os.environ.get("GOOGLE_VOICE_ACCOUNT")
        or config().get("google_voice_account")
        or ""
    ).strip().lower()


def account_from_label(label):
    emails = re.findall(
        r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
        str(label or ""),
    )
    return emails[0].lower() if len(emails) == 1 else ""


def account_for_tab(c, tab):
    label = c.eval(
        tab,
        """document.querySelector('[aria-label^="Google Account:"]')
             ?.getAttribute('aria-label') || ''""",
    )
    return account_from_label(label)


def first_match(c, tab, kind, limit=40):
    """Return (selector, matches) for the first candidate that matches anything."""
    for sel in SELECTORS[kind]:
        try:
            found = c.query(tab, sel, limit=limit)
        except BridgeError:
            continue
        if found:
            return sel, found
    return None, []


def open_voice(c):
    want = expected_account()
    target_url = config().get("google_voice_url") or MESSAGES_URL
    require_voice_url(target_url, "configured Google Voice URL")
    voice_tabs = [
        tab for tab in c.tabs()
        if trusted_voice_url(tab.get("url"))
    ]
    accounts = {}
    tab = None
    for candidate in voice_tabs:
        account = account_for_tab(c, candidate["tabId"])
        accounts[account or "(unknown)"] = candidate["url"]
        if want and account == want:
            tab = candidate["tabId"]
            break

    if tab is None:
        # Cold start is ordinary: the browser may have no Voice tab yet. Open
        # the configured /u/N surface, wait for the account chip, then apply
        # the same strict identity check used for pre-existing tabs.
        tab = c.open(target_url, reuse=False)
        try:
            c.waitfor(tab, '[aria-label^="Google Account:"]', timeout=15000)
        except BridgeError:
            pass

    account = account_for_tab(c, tab)
    if want and account != want:
        seen = ", ".join(sorted(accounts)) or "none"
        raise SystemExit(
            f"Google Voice account mismatch: expected {want}, got {account or 'unknown'}. "
            f"Previously open accounts: {seen}. Refusing to send from the wrong number."
        )

    # Preserve /u/N from the tab selected above. Navigating an account-matched
    # /u/1 tab back to the hardcoded /u/0 URL silently switches identities.
    current = tab_url(c, tab)
    parsed = require_voice_url(current, "selected Google Voice tab")
    match = re.search(r"/u/\d+/messages", parsed.path)
    messages_url = (
        f"https://voice.google.com{match.group(0)}"
        if match
        else MESSAGES_URL
    )
    c.navigate(tab, messages_url)
    tab_url(c, tab)
    # The app renders after the shell loads; waiting on the network is not
    # enough and a fixed sleep is either slow or flaky.
    for sel in SELECTORS["thread_list"]:
        try:
            c.waitfor(tab, sel, timeout=12000)
            post_account = account_for_tab(c, tab)
            if want and post_account != want:
                raise SystemExit(
                    "Google Voice account changed during navigation; refusing"
                )
            return tab
        except BridgeError:
            continue
    body = (c.text(tab) or "")[:400]
    if re.search(r"sign in|choose an account|couldn't sign you in", body, re.I):
        raise SystemExit(
            "Google Voice is showing a sign-in page in this browser.\n"
            "Log in once in Chrome, then re-run — this tool never handles your "
            "credentials.")
    raise SystemExit(
        "Loaded Google Voice but found no conversation list.\n"
        "Run `python3 gvoice.py probe` to see what the page looks like now; "
        "the selectors in SELECTORS may need one line updated.")


def threads(c, tab, limit=25):
    sel, found = first_match(c, tab, "thread_list", limit=limit)
    if not sel:
        raise SystemExit("no conversation list matched — run `probe`")
    out = []
    for t in found:
        txt = (t.get("text") or "").strip()
        if not txt:
            continue
        lines = [l.strip() for l in txt.split("\n") if l.strip()]
        out.append({
            "i": t["i"],
            "who": lines[0] if lines else "?",
            "preview": " ".join(lines[1:])[:120],
            "aria": t.get("aria"),
        })
    return sel, out


def pick(threads_list, who):
    """Match a thread by name or number, case-insensitively."""
    needle = who.lower().strip()
    digits = re.sub(r"\D", "", needle)
    for t in threads_list:
        hay = f"{t['who']} {t.get('aria') or ''}".lower()
        if needle in hay:
            return t
        if digits and len(digits) >= 7 and digits[-7:] in re.sub(r"\D", "", hay):
            return t
    return None


def messages_url_for_tab(c, tab):
    current = tab_url(c, tab)
    parsed = require_voice_url(current, "Google Voice thread tab")
    match = re.search(r"/u/\d+/messages", parsed.path)
    return (
        f"https://voice.google.com{match.group(0)}"
        if match
        else MESSAGES_URL
    )


def require_peer_thread(c, tab, peer):
    number = canonical_peer_number(peer)
    current = tab_url(c, tab)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(current).query)
    if query.get("itemId") != [f"t.{number}"]:
        raise BridgeError("Google Voice did not remain on the configured peer thread")
    return number


def wait_for_thread(
    c,
    tab,
    attempts=60,
    delay=0.25,
    previous_marker=None,
    peer=None,
    account=None,
):
    """Wait for either an existing message or a visible compose box."""
    marker_json = json.dumps(previous_marker or "")
    account_json = json.dumps(str(account or "").lower())
    peer_digits = re.sub(r"\D", "", canonical_peer_number(peer)) if peer else ""
    peer_digits_json = json.dumps(peer_digits)
    item_id_json = json.dumps(
        f"t.{canonical_peer_number(peer)}" if peer else ""
    )
    expression = f"""(() => {{
      if (
        {marker_json}
        && document.documentElement?.getAttribute(
          'data-rapp-navigation-marker'
        ) === {marker_json}
      ) return false;
      if ({item_id_json}) {{
        let url;
        try {{
          url = new URL(window.location.href);
        }} catch (_) {{
          return false;
        }}
        const label = document.querySelector(
          '[aria-label^="Google Account:"]'
        )?.getAttribute('aria-label') || '';
        const emails = label.match(
          /[\\w.+-]+@[\\w.-]+\\.[A-Za-z]{{2,}}/g
        ) || [];
        const header = document.querySelector(
          'gv-thread-details-header h2'
        );
        const headerDigits = String(
          header?.innerText || header?.textContent || ''
        ).replace(/\\D/g, '');
        const expectedDigits = {peer_digits_json};
        const peerOk = headerDigits === expectedDigits
          || (
            expectedDigits.length === 11
            && expectedDigits.startsWith('1')
            && headerDigits === expectedDigits.slice(1)
          );
        const itemIds = url.searchParams.getAll('itemId');
        if (
          url.origin !== 'https://voice.google.com'
          || !/^\\/u\\/\\d+\\/messages\\/?$/.test(url.pathname)
          || itemIds.length !== 1
          || itemIds[0] !== {item_id_json}
          || emails.length !== 1
          || emails[0].toLowerCase() !== {account_json}
          || !header || !header.offsetParent || !peerOk
        ) return false;
      }}
      if (document.querySelector('gv-message-item')) return true;
      return [...document.querySelectorAll(
        'textarea,div[contenteditable="true"][role="textbox"]'
      )].some(element => !!element.offsetParent);
    }})()"""
    for _ in range(attempts):
        try:
            if c.eval(tab, expression):
                return True
        except BridgeError:
            pass
        time.sleep(delay)
    return False


def open_thread(c, tab, who):
    digits = re.sub(r"\D", "", who)
    if len(digits) >= 7:
        number = canonical_peer_number(who)
        item_id = urllib.parse.quote(f"t.{number}", safe="")
        marker = secrets.token_hex(16)
        c.eval(
            tab,
            f"""(() => {{
              document.documentElement?.setAttribute(
                'data-rapp-navigation-marker',
                {json.dumps(marker)}
              );
              return true;
            }})()""",
        )
        c.navigate(tab, f"{messages_url_for_tab(c, tab)}?itemId={item_id}")
        # Navigation settles when the document is complete, but Voice renders
        # the thread asynchronously afterwards. Reading immediately produced
        # an empty list and the watcher reported "no new inbound messages"
        # while the reply was visibly present in the browser.
        if not wait_for_thread(
            c,
            tab,
            previous_marker=marker,
            peer=number,
            account=expected_account(),
        ):
            raise BridgeError(
                f"Google Voice thread for {number} did not render within 15 seconds"
            )
        require_peer_thread(c, tab, who)
        return {"who": number, "i": None}

    sel, tl = threads(c, tab)
    hit = pick(tl, who)
    if not hit:
        names = ", ".join(t["who"] for t in tl[:12])
        raise SystemExit(f"no conversation matching {who!r}. Open threads: {names}")
    c.click(tab, sel, index=hit["i"])
    if not wait_for_thread(c, tab, attempts=40):
        raise BridgeError(
            f"Google Voice thread for {hit['who']} did not render within 10 seconds"
        )
    return hit


def read(c, tab, who, limit=15):
    hit = open_thread(c, tab, who)
    items = messages(c, tab)
    if not items:
        raise SystemExit("opened the thread but found no messages — run `probe`")
    return hit, [item["body"] for item in items[-limit:]]


def messages(c, tab):
    """Structured messages in DOM order with an explicit direction.

    Google Voice does not expose a personal-account API. The accessibility text
    is still unusually helpful: outgoing rows say "Message from you", while
    inbound rows say "Message from <digits>". Direction is parsed from that
    declaration, never inferred from color or alignment.
    """
    return c.eval(
        tab,
        """(() => {
        const seen = new Map();
        return [...document.querySelectorAll(
          'gv-message-item,[data-e2e-is-outgoing]'
        )].map((node, index) => {
          const raw = (node.innerText || '').trim();
          const bodyNode = node.querySelector('.subject-content-container')
            || node.querySelector('[data-e2e-message-text]')
            || node.querySelector('.message-row');
          const body = (bodyNode?.innerText || bodyNode?.textContent || '')
            .replace(/\\s+/g, ' ').trim();
          const outbound = /(^|\\n)Message from you,/.test(raw);
          const match = raw.match(/Message from ([^,]+),/);
          const label = [...node.querySelectorAll('[aria-label]')]
            .map(el => el.getAttribute('aria-label') || '')
            .find(value => value.includes('Message from ')) || '';
          const declaration = raw.split('\\n')
            .map(line => line.trim())
            .find(line => line.startsWith('Message from ')) || '';
          const identity = label || declaration || raw;
          const signature = [outbound ? 'outbound' : 'inbound', identity, body]
            .join('|');
          const occurrence = (seen.get(signature) || 0) + 1;
          seen.set(signature, occurrence);
          return {
            index,
            direction: outbound ? 'outbound' : 'inbound',
            from: outbound ? 'you' : (match?.[1] || '').replace(/\\s+/g, ''),
            body,
            label,
            identity,
            occurrence,
            raw,
          };
        }).filter(item => item.body);
        })()""",
    )


def messages_locked(c, tab, peer, account):
    """Read one atomically account-, URL-, and rendered-peer-bound snapshot."""
    number = canonical_peer_number(peer)
    account = str(account or "").strip().lower()
    if not account:
        raise BridgeError("a configured Google Voice account is required to read")
    expected_item = json.dumps(f"t.{number}")
    expected_account_json = json.dumps(account)
    expected_digits = json.dumps(re.sub(r"\D", "", number))
    snapshot = c.eval(
        tab,
        f"""(() => {{
          let url;
          try {{
            url = new URL(window.location.href);
          }} catch (_) {{
            return {{ok:false, why:'invalid Voice URL'}};
          }}
          const label = document.querySelector(
            '[aria-label^="Google Account:"]'
          )?.getAttribute('aria-label') || '';
          const emails = label.match(
            /[\\w.+-]+@[\\w.-]+\\.[A-Za-z]{{2,}}/g
          ) || [];
          const header = document.querySelector(
            'gv-thread-details-header h2'
          );
          const headerDigits = String(
            header?.innerText || header?.textContent || ''
          ).replace(/\\D/g, '');
          const wantedDigits = {expected_digits};
          const peerOk = headerDigits === wantedDigits
            || (
              wantedDigits.length === 11
              && wantedDigits.startsWith('1')
              && headerDigits === wantedDigits.slice(1)
            );
          const itemIds = url.searchParams.getAll('itemId');
          const contextOk = url.origin === 'https://voice.google.com'
            && /^\\/u\\/\\d+\\/messages\\/?$/.test(url.pathname)
            && itemIds.length === 1
            && itemIds[0] === {expected_item}
            && emails.length === 1
            && emails[0].toLowerCase() === {expected_account_json}
            && !!header && !!header.offsetParent && peerOk;
          if (!contextOk) {{
            return {{
              ok:false,
              why:'account, URL, or rendered peer did not match'
            }};
          }}
          const seen = new Map();
          const items = [...document.querySelectorAll(
            'gv-message-item,[data-e2e-is-outgoing]'
          )].map((node, index) => {{
            const raw = (node.innerText || '').trim();
            const bodyNode = node.querySelector('.subject-content-container')
              || node.querySelector('[data-e2e-message-text]')
              || node.querySelector('.message-row');
            const body = (
              bodyNode?.innerText || bodyNode?.textContent || ''
            ).replace(/\\s+/g, ' ').trim();
            const outbound = /(^|\\n)Message from you,/.test(raw);
            const match = raw.match(/Message from ([^,]+),/);
            const messageLabel = [...node.querySelectorAll('[aria-label]')]
              .map(el => el.getAttribute('aria-label') || '')
              .find(value => value.includes('Message from ')) || '';
            const declaration = raw.split('\\n')
              .map(line => line.trim())
              .find(line => line.startsWith('Message from ')) || '';
            const identity = messageLabel || declaration || raw;
            const signature = [
              outbound ? 'outbound' : 'inbound',
              identity,
              body
            ].join('|');
            const occurrence = (seen.get(signature) || 0) + 1;
            seen.set(signature, occurrence);
            return {{
              index,
              direction: outbound ? 'outbound' : 'inbound',
              from: outbound
                ? 'you'
                : (match?.[1] || '').replace(/\\s+/g, ''),
              body,
              label: messageLabel,
              identity,
              occurrence,
              raw,
            }};
          }}).filter(item => item.body);
          return {{ok:true, items}};
        }})()""",
    )
    if not isinstance(snapshot, dict) or not snapshot.get("ok"):
        detail = snapshot.get("why") if isinstance(snapshot, dict) else "invalid snapshot"
        raise BridgeError(f"Google Voice locked read failed: {detail}")
    if not isinstance(snapshot.get("items"), list):
        raise BridgeError("Google Voice locked read returned invalid messages")
    return snapshot["items"]


def send(c, tab, who, text, confirm=True):
    """Type and send. `confirm` re-reads the thread to prove it landed.

    An automation that reports success because a click did not throw is the
    same mistake as trusting an exit code — the message has to appear.
    """
    account = expected_account()
    if not account:
        raise BridgeError("a configured Google Voice account is required to send")
    number = canonical_peer_number(who)
    open_thread(c, tab, number)
    require_peer_thread(c, tab, number)
    want = json.dumps(text)
    want_account = json.dumps(account)
    want_item_id = json.dumps(f"t.{number}")
    want_peer_digits = json.dumps(re.sub(r"\D", "", number))

    composed = c.eval(
        tab,
        f"""(async () => {{
          const sleep = ms => new Promise(r => setTimeout(r, ms));
          const validContext = () => {{
            let url;
            try {{
              url = new URL(window.location.href);
            }} catch (_) {{
              return false;
            }}
            const label = document.querySelector(
              '[aria-label^="Google Account:"]'
            )?.getAttribute('aria-label') || '';
            const emails = label.match(
              /[\\w.+-]+@[\\w.-]+\\.[A-Za-z]{{2,}}/g
            ) || [];
            const account = emails.length === 1 ? emails[0].toLowerCase() : '';
            const header = document.querySelector(
              'gv-thread-details-header h2'
            );
            const headerDigits = String(
              header?.innerText || header?.textContent || ''
            ).replace(/\\D/g, '');
            const expectedDigits = {want_peer_digits};
            const peerOk = headerDigits === expectedDigits
              || (
                expectedDigits.length === 11
                && expectedDigits.startsWith('1')
                && headerDigits === expectedDigits.slice(1)
              );
            const itemIds = url.searchParams.getAll('itemId');
            return url.protocol === 'https:'
              && url.hostname === 'voice.google.com'
              && (url.port === '' || url.port === '443')
              && !url.username && !url.password
              && /^\\/u\\/\\d+\\/messages\\/?$/.test(url.pathname)
              && itemIds.length === 1
              && itemIds[0] === {want_item_id}
              && account === {want_account}
              && !!header && !!header.offsetParent && peerOk;
          }};
          if (!validContext()) {{
            return {{ok:false, why:'account or thread changed before compose'}};
          }}
          const pick = () => {{
            const all = [...document.querySelectorAll(
              'textarea,div[contenteditable="true"][role="textbox"]'
            )];
            const visible = all.filter(el => !!el.offsetParent);
            return visible.find(el => /type a message/i.test(el.placeholder || ''))
              || visible.find(el => /message/i.test(el.getAttribute('aria-label') || ''))
              || visible.find(el => el.getAttribute('gv-test-id') === 'gv-message-input')
              || visible[0] || null;
          }};
          let box = null;
          for (let i = 0; i < 40 && !box; i++) {{
            box = pick();
            if (!box) await sleep(250);
          }}
          if (!box) return {{ok:false, why:'no message input appeared'}};
          if (!box.isConnected || !validContext()) {{
            return {{ok:false, why:'account or thread changed before compose'}};
          }}
          box.focus();
          const value = {want};
          if (box.tagName === 'TEXTAREA') {{
            const setter = Object.getOwnPropertyDescriptor(
              HTMLTextAreaElement.prototype, 'value'
            ).set;
            setter.call(box, value);
            box.dispatchEvent(new Event('input', {{bubbles:true}}));
          }} else {{
            box.textContent = value;
            box.dispatchEvent(new InputEvent('input', {{bubbles:true}}));
          }}
          await sleep(150);
          const read = box.tagName === 'TEXTAREA' ? box.value : box.textContent;
          return read && read.includes(value)
            ? {{ok:true}}
            : {{ok:false, why:'input did not take the text'}};
        }})()""",
    )
    if not composed.get("ok"):
        raise SystemExit(f"could not compose message: {composed.get('why')}")

    if not confirm:
        return {"sent": False, "verified": False, "composed": True}

    before = c.eval(
        tab,
        f"""(() => {{
          let url;
          try {{
            url = new URL(window.location.href);
          }} catch (_) {{
            return {{ok:false, count:0}};
          }}
          const label = document.querySelector(
            '[aria-label^="Google Account:"]'
          )?.getAttribute('aria-label') || '';
          const emails = label.match(
            /[\\w.+-]+@[\\w.-]+\\.[A-Za-z]{{2,}}/g
          ) || [];
          const account = emails.length === 1 ? emails[0].toLowerCase() : '';
          const header = document.querySelector(
            'gv-thread-details-header h2'
          );
          const headerDigits = String(
            header?.innerText || header?.textContent || ''
          ).replace(/\\D/g, '');
          const expectedDigits = {want_peer_digits};
          const peerOk = headerDigits === expectedDigits
            || (
              expectedDigits.length === 11
              && expectedDigits.startsWith('1')
              && headerDigits === expectedDigits.slice(1)
            );
          const itemIds = url.searchParams.getAll('itemId');
          const ok = url.protocol === 'https:'
            && url.hostname === 'voice.google.com'
            && (url.port === '' || url.port === '443')
            && !url.username && !url.password
            && /^\\/u\\/\\d+\\/messages\\/?$/.test(url.pathname)
            && itemIds.length === 1
            && itemIds[0] === {want_item_id}
            && account === {want_account}
            && !!header && !!header.offsetParent && peerOk;
          if (!ok) return {{ok:false, count:0}};
          const count = [...document.querySelectorAll(
            'gv-message-item,[data-e2e-is-outgoing]'
          )].filter(n => {{
          const normalize = value => String(value || '')
            .replace(/\\s+/g, ' ').trim();
          const mine = !!n.querySelector('.outgoing')
            || n.getAttribute('data-e2e-is-outgoing') === 'true'
            || String(n.className || '').includes('outgoing');
          return mine && normalize(n.innerText).includes(normalize({want}));
          }}).length;
          return {{ok:true, count}};
        }})()""",
    )
    if not before.get("ok"):
        raise BridgeError("Google Voice account or thread changed before send")

    clicked = c.eval(
        tab,
        f"""(async () => {{
          const sleep = ms => new Promise(r => setTimeout(r, ms));
          const validContext = () => {{
            let url;
            try {{
              url = new URL(window.location.href);
            }} catch (_) {{
              return false;
            }}
            const label = document.querySelector(
              '[aria-label^="Google Account:"]'
            )?.getAttribute('aria-label') || '';
            const emails = label.match(
              /[\\w.+-]+@[\\w.-]+\\.[A-Za-z]{{2,}}/g
            ) || [];
            const account = emails.length === 1 ? emails[0].toLowerCase() : '';
            const header = document.querySelector(
              'gv-thread-details-header h2'
            );
            const headerDigits = String(
              header?.innerText || header?.textContent || ''
            ).replace(/\\D/g, '');
            const expectedDigits = {want_peer_digits};
            const peerOk = headerDigits === expectedDigits
              || (
                expectedDigits.length === 11
                && expectedDigits.startsWith('1')
                && headerDigits === expectedDigits.slice(1)
              );
            const itemIds = url.searchParams.getAll('itemId');
            return url.protocol === 'https:'
              && url.hostname === 'voice.google.com'
              && (url.port === '' || url.port === '443')
              && !url.username && !url.password
              && /^\\/u\\/\\d+\\/messages\\/?$/.test(url.pathname)
              && itemIds.length === 1
              && itemIds[0] === {want_item_id}
              && account === {want_account}
              && !!header && !!header.offsetParent && peerOk;
          }};
          if (!validContext()) {{
            return {{ok:false, why:'account or thread changed before send'}};
          }}
          const find = () =>
            document.querySelector('button[gv-test-id="send-button"]:not([disabled])')
            || [...document.querySelectorAll('button')].find(
              b => /send/i.test(b.getAttribute('aria-label') || '')
                && !b.disabled && !!b.offsetParent
            );
          let button = null;
          for (let i = 0; i < 20 && !button; i++) {{
            button = find();
            if (!button) await sleep(250);
          }}
          if (!button) {{
            return {{ok:false, why:'send button never became enabled'}};
          }}
          const box = [...document.querySelectorAll(
            'textarea,div[contenteditable="true"][role="textbox"]'
          )].find(el => !!el.offsetParent);
          const value = box?.tagName === 'TEXTAREA'
            ? box.value
            : box?.textContent;
          if (!button.isConnected || !box?.isConnected || value !== {want}) {{
            return {{ok:false, why:'compose contents changed before send'}};
          }}
          if (!validContext()) {{
            return {{ok:false, why:'account or thread changed before send'}};
          }}
          button.click();
          return {{ok:true}};
        }})()""",
    )
    if not clicked.get("ok"):
        raise SystemExit(f"could not send message: {clicked.get('why')}")

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        observed = c.eval(
            tab,
            f"""(() => {{
              let url;
              try {{
                url = new URL(window.location.href);
              }} catch (_) {{
                return {{ok:false, count:0}};
              }}
              const label = document.querySelector(
                '[aria-label^="Google Account:"]'
              )?.getAttribute('aria-label') || '';
              const emails = label.match(
                /[\\w.+-]+@[\\w.-]+\\.[A-Za-z]{{2,}}/g
              ) || [];
              const account = emails.length === 1
                ? emails[0].toLowerCase()
                : '';
              const header = document.querySelector(
                'gv-thread-details-header h2'
              );
              const headerDigits = String(
                header?.innerText || header?.textContent || ''
              ).replace(/\\D/g, '');
              const expectedDigits = {want_peer_digits};
              const peerOk = headerDigits === expectedDigits
                || (
                  expectedDigits.length === 11
                  && expectedDigits.startsWith('1')
                  && headerDigits === expectedDigits.slice(1)
                );
              const itemIds = url.searchParams.getAll('itemId');
              const ok = url.protocol === 'https:'
                && url.hostname === 'voice.google.com'
                && (url.port === '' || url.port === '443')
                && !url.username && !url.password
                && /^\\/u\\/\\d+\\/messages\\/?$/.test(url.pathname)
                && itemIds.length === 1
                && itemIds[0] === {want_item_id}
                && account === {want_account}
                && !!header && !!header.offsetParent && peerOk;
              if (!ok) return {{ok:false, count:0}};
              const count = [...document.querySelectorAll(
                'gv-message-item,[data-e2e-is-outgoing]'
              )].filter(n => {{
              const normalize = value => String(value || '')
                .replace(/\\s+/g, ' ').trim();
              const mine = !!n.querySelector('.outgoing')
                || n.getAttribute('data-e2e-is-outgoing') === 'true'
                || String(n.className || '').includes('outgoing');
              return mine && normalize(n.innerText).includes(normalize({want}));
              }}).length;
              return {{ok:true, count}};
            }})()""",
        )
        if not observed.get("ok"):
            raise BridgeError(
                "Google Voice account or thread changed during send readback"
            )
        if observed["count"] > before["count"]:
            return {"sent": True, "verified": True}
        time.sleep(0.4)
    raise SystemExit(
        "send could not be confirmed: the message did not appear as outgoing "
        "within 15 seconds; treating it as NOT sent"
    )


def unread(c, tab):
    """Threads Google Voice is marking as unread."""
    code = """
    (() => {
      const out = [];
      document.querySelectorAll('[aria-label]').forEach(el => {
        const a = el.getAttribute('aria-label') || '';
        if (/unread/i.test(a)) out.push(a.slice(0, 160));
      });
      return out.slice(0, 20);
    })()
    """
    return c.eval(tab, code)


def probe(c, tab):
    print(f"url: {c.tabs() and [t for t in c.tabs() if t['tabId'] == tab][0]['url']}\n")
    for kind, cands in SELECTORS.items():
        print(f"{kind}:")
        for sel in cands:
            try:
                n = len(c.query(tab, sel, limit=60))
            except BridgeError as e:
                print(f"   {sel:56} ERROR {e}")
                continue
            mark = "<-- using" if n else ""
            print(f"   {sel:56} {n:>3} match{'es' if n != 1 else ''} {mark}")
            if n:
                break
        print()
    print("--- first 600 chars of page text ---")
    print((c.text(tab) or "")[:600])


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "threads"
    with Chrome() as c:
        tab = open_voice(c) if cmd != "probe" else c.open(MESSAGES_URL)
        if cmd == "probe":
            time.sleep(3)          # let the app render before describing it
            probe(c, tab)
        elif cmd == "threads":
            _, tl = threads(c, tab)
            for t in tl:
                print(f"{t['who'][:28]:28}  {t['preview'][:60]}")
        elif cmd == "read":
            hit, msgs = read(c, tab, args[1])
            print(f"--- {hit['who']} ---")
            for m in msgs:
                print(" ", m.replace("\n", " ")[:160])
        elif cmd == "send":
            print(json.dumps(send(c, tab, args[1], args[2]), indent=2))
        elif cmd == "unread":
            for u in unread(c, tab):
                print(" ", u)
        else:
            print(__doc__)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
