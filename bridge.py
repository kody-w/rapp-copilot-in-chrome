#!/usr/bin/env python3
"""bridge.py — drive your real Chrome from Python. No vendor in the chain.

    python3 bridge.py token                     # print/create the shared token
    python3 bridge.py tabs                      # list open tabs
    python3 bridge.py open https://example.com
    python3 bridge.py text  <tabId>
    python3 bridge.py eval  <tabId> "document.title"
    python3 bridge.py click <tabId> "button.send"

    from bridge import Chrome
    with Chrome() as c:
        tab = c.open("https://voice.google.com")
        print(c.text(tab)[:400])

────────────────────────────────────────────────────────────────────────────
WHY THIS EXISTS

The published route to browser control here is Anthropic's claude-in-chrome:

    script -> claude binary -> native messaging host -> Anthropic extension -> tabs

That is four dependencies to read one page, and it failed on this machine at
two of them — no native-messaging manifest, and the extension reporting
"Browser extension is not connected", which also requires being logged into
claude.ai with a matching account. Worse, the vendor's own `doctor` reported
"Chrome reachable (live round trip) [ok]" while that was true, because the
refusal comes back as an ordinary text response with no error flag.

This route is:

    script -> localhost WebSocket -> our extension -> tabs

The extension dials OUT, so there is no manifest to register and no browser
restart when things change. There is no vendor binary and no account.

ONE-SHOT BY DESIGN. This starts a server, waits for the extension to dial in
(it retries every 2s), does the work, and exits. No daemon to supervise, no
pidfile to go stale — which matters on this machine, where launchd will not
reliably spawn scheduled jobs at all.

NO DEPENDENCIES. The WebSocket server is ~120 lines of stdlib below, matching
the rest of this stack. `pip install websockets` inside a watchdog is one more
thing that can be missing at 3am.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import socket
import struct
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

CONF_DIR = Path.home() / ".rappter-chrome"
TOKEN_FILE = CONF_DIR / "token"
CONFIG_FILE = CONF_DIR / "config.json"
DEFAULT_PORT = 8777
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def token(create=True):
    if TOKEN_FILE.exists():
        value = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    if not create:
        return ""
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    # Publish a fully written token atomically. O_EXCL alone still lets a loser
    # read the file between the winner's create and write.
    fd, temp_name = tempfile.mkstemp(prefix=".token-", dir=CONF_DIR)
    temp = Path(temp_name)
    try:
        value = secrets.token_urlsafe(24)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, TOKEN_FILE)
            return value
        except FileExistsError:
            existing = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if not existing:
                raise BridgeError("token file exists but is empty")
            return existing
    finally:
        temp.unlink(missing_ok=True)


class BridgeError(RuntimeError):
    pass


def preferred_instance():
    from_env = os.environ.get("RAPPTER_CHROME_INSTANCE", "").strip()
    if from_env:
        return from_env
    try:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return str(config.get("browser_instance") or "").strip()
    except Exception:
        return ""


def _proof(shared_token, message):
    digest = hmac.new(
        shared_token.encode(),
        message.encode(),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


# ── minimal RFC6455 server ──────────────────────────────────────────────────

def _accept_key(key):
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise BridgeError("socket closed mid-frame")
        buf += chunk
    return buf


def _read_frame(sock):
    """Return (opcode, payload). Reassembles continuation frames."""
    opcode_out, data = None, b""
    while True:
        b1, b2 = _recv_exact(sock, 2)
        fin = b1 & 0x80
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        if not masked:
            raise BridgeError("client WebSocket frames must be masked")
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", _recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack(">Q", _recv_exact(sock, 8))[0]
        if length > 64 * 1024 * 1024:
            raise BridgeError("WebSocket frame exceeds 64MB")
        mask = _recv_exact(sock, 4) if masked else b""
        payload = _recv_exact(sock, length) if length else b""
        if masked:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        if opcode_out is None and opcode != 0:
            opcode_out = opcode
        data += payload
        if fin:
            return opcode_out, data


def _send_frame(sock, payload, opcode=0x1):
    if isinstance(payload, str):
        payload = payload.encode()
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < (1 << 16):
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    sock.sendall(bytes(header) + payload)


class Chrome:
    """A one-shot bridge session. Use as a context manager."""

    def __init__(
        self,
        port=DEFAULT_PORT,
        wait=30,
        timeout=60,
        verbose=False,
        instance=None,
    ):
        self.port = port
        self.wait = wait
        self.timeout = timeout
        self.verbose = verbose
        self.instance = preferred_instance() if instance is None else instance
        self.srv = None
        self.conn = None
        self.hello = None
        self.instance_id = None
        self._id = 0

    # -- lifecycle --
    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self):
        expected = token()
        if not expected:
            raise BridgeError("no token; run: python3 bridge.py token")

        deadline = time.monotonic() + self.wait
        while True:
            self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self.srv.bind(("127.0.0.1", self.port))  # never 0.0.0.0
                break
            except OSError as exc:
                self.srv.close()
                self.srv = None
                if exc.errno not in (48, 98) or time.monotonic() >= deadline:
                    raise BridgeError(
                        f"cannot bind 127.0.0.1:{self.port}: {exc}"
                    ) from exc
                # Another MCP client owns the one browser channel. Serialize
                # rather than failing; the extension reconnects when it exits.
                time.sleep(0.2)
        self.srv.listen(1)
        self.srv.settimeout(max(0.1, deadline - time.monotonic()))

        last_error = None
        while time.monotonic() < deadline:
            self.srv.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                break
            conn.settimeout(self.timeout)
            try:
                hello = self._authenticate(conn, expected)
            except BridgeError as exc:
                last_error = exc
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            instance_id = str(hello.get("instanceId") or "")
            if self.instance and instance_id != self.instance:
                last_error = BridgeError(
                    f"extension instance {instance_id or '(unknown)'} does not "
                    f"match configured instance {self.instance}"
                )
                try:
                    _send_frame(conn, b"", opcode=0x8)
                    conn.close()
                except OSError:
                    pass
                continue
            self.conn = conn
            self.hello = hello
            self.instance_id = instance_id
            if self.verbose:
                print(f"# connected: {hello}", file=sys.stderr)
            return self
        detail = f" Last refusal: {last_error}" if last_error else ""
        raise BridgeError(
            f"no authenticated extension dialled in within {self.wait}s.\n"
            "  - is the extension loaded at edge://extensions or "
            "chrome://extensions?\n"
            "  - does its popup show the same token and port?\n"
            f"  - configured instance: {self.instance or '(any)'}."
            f"{detail}"
        )

    def _authenticate(self, conn, expected):
        # -- WebSocket handshake --
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = conn.recv(4096)
            if not chunk:
                conn.close()
                raise BridgeError("peer closed during WebSocket handshake")
            raw += chunk
            if len(raw) > 64 * 1024:
                conn.close()
                raise BridgeError("WebSocket handshake exceeded 64KB")
        head = raw.decode("latin-1").split("\r\n")
        request = head[0]
        headers = {}
        for line in head[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        origin = headers.get("origin", "")
        # A web page can guess a port. It cannot forge this origin: Chrome sets
        # it, and only an extension gets a chrome-extension:// one. Without this
        # check any site you visit could drive your logged-in browser.
        if not origin.startswith("chrome-extension://"):
            self._reject(conn, "403 Forbidden")
            raise BridgeError(f"refused non-extension origin: {origin!r}")

        key = headers.get("sec-websocket-key", "")
        conn.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + _accept_key(key).encode() + b"\r\n\r\n")

        hello = self._recv_json_from(conn)
        if (
            not isinstance(hello, dict)
            or hello.get("hello") != "rappter-chrome"
            or hello.get("version") != 2
            or not isinstance(hello.get("clientNonce"), str)
            or not isinstance(hello.get("instanceId"), str)
        ):
            raise BridgeError("extension sent an invalid authentication hello")

        client_nonce = hello["clientNonce"]
        instance_id = hello["instanceId"]
        server_nonce = secrets.token_urlsafe(24)
        server_proof = _proof(
            expected,
            f"server:{client_nonce}:{server_nonce}:{instance_id}",
        )
        _send_frame(
            conn,
            json.dumps(
                {
                    "authChallenge": server_nonce,
                    "serverProof": server_proof,
                }
            ),
        )
        response = self._recv_json_from(conn)
        wanted = _proof(
            expected,
            f"client:{client_nonce}:{server_nonce}:{instance_id}",
        )
        if (
            not isinstance(response, dict)
            or response.get("instanceId") != instance_id
            or not secrets.compare_digest(
                str(response.get("authResponse") or ""),
                wanted,
            )
        ):
            raise BridgeError("extension mutual authentication failed")
        return hello

    def _reject(self, conn, status):
        try:
            conn.sendall(f"HTTP/1.1 {status}\r\nConnection: close\r\n\r\n".encode())
            conn.close()
        except OSError:
            pass

    def close(self):
        for s in (self.conn, self.srv):
            try:
                if s:
                    s.close()
            except OSError:
                pass
        self.conn = self.srv = None

    # -- protocol --
    def _recv_json(self):
        return self._recv_json_from(self.conn)

    def _recv_json_from(self, conn):
        while True:
            op, data = _read_frame(conn)
            if op == 0x8:
                raise BridgeError("extension closed the connection")
            if op == 0x9:                              # ping -> pong
                _send_frame(conn, data, opcode=0xA)
                continue
            if op == 0xA:
                continue
            return json.loads(data.decode())

    def call(self, cmd, **args):
        self._id += 1
        mid = self._id
        _send_frame(self.conn, json.dumps({"id": mid, "cmd": cmd, "args": args}))
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            msg = self._recv_json()
            if msg.get("id") != mid:
                continue
            if not msg.get("ok"):
                raise BridgeError(f"{cmd}: {msg.get('error')}")
            return msg.get("result")
        raise BridgeError(f"{cmd}: no reply within {self.timeout}s")

    # -- the verbs --
    def ping(self):                        return self.call("ping")
    def tabs(self):                        return self.call("tabs")
    def find_tab(self, match):             return self.call("find_tab", match=match)
    def create(self, url=None, active=False, timeout=20000):
        return self.call("create", url=url, active=active, timeout=timeout)
    def close_tab(self, tab):              return self.call("close", tabId=tab)
    def activate(self, tab):               return self.call("activate", tabId=tab)
    def navigate(self, tab, url, timeout=20000):
        return self.call("navigate", tabId=tab, url=url, timeout=timeout)
    def text(self, tab):                   return self.call("text", tabId=tab)
    def html(self, tab):                   return self.call("html", tabId=tab)
    def query(self, tab, selector, limit=40):
        return self.call("query", tabId=tab, selector=selector, limit=limit)
    def click(self, tab, selector, index=0):
        return self.call("click", tabId=tab, selector=selector, index=index)
    def type(self, tab, selector, text, submit=False):
        return self.call("type", tabId=tab, selector=selector, text=text, submit=submit)
    def waitfor(self, tab, selector, timeout=15000):
        return self.call("waitfor", tabId=tab, selector=selector, timeout=timeout)
    def eval(self, tab, code, await_promise=True):
        return self.call("eval", tabId=tab, code=code, awaitPromise=await_promise)
    def screenshot(self, tab):             return self.call("screenshot", tabId=tab)
    def batch(self, actions):              return self.call("batch", actions=actions)

    def open(self, url, reuse=True):
        """Open a URL, reusing an existing tab on the same origin when possible.

        Reuse matters for logged-in apps: a fresh tab per run leaves a drift of
        twenty Google Voice tabs after a day of cron.
        """
        if reuse:
            host = urllib.parse.urlparse(url).netloc
            hit = self.find_tab(host)
            if hit:
                self.navigate(hit["tabId"], url)
                return hit["tabId"]
        return self.create(url=url)["tabId"]


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "help"

    if cmd == "token":
        print(token())
        return 0
    if cmd == "help":
        print(__doc__)
        return 0

    try:
        with Chrome(
            verbose=True,
            instance="" if cmd == "identity" else None,
        ) as c:
            if cmd == "identity":
                print(json.dumps(c.hello, indent=2))
            elif cmd == "ping":
                print(json.dumps(c.ping()))
            elif cmd == "tabs":
                for t in c.tabs():
                    print(f"{t['tabId']:>10}  {(t['title'] or '')[:52]:52}  {t['url'][:60]}")
            elif cmd == "open":
                print(c.open(args[1]))
            elif cmd == "text":
                print(c.text(int(args[1]))[:6000])
            elif cmd == "html":
                print(c.html(int(args[1]))[:6000])
            elif cmd == "eval":
                print(json.dumps(c.eval(int(args[1]), args[2]), indent=2)[:6000])
            elif cmd == "click":
                print(json.dumps(c.click(int(args[1]), args[2])))
            elif cmd == "type":
                print(json.dumps(c.type(int(args[1]), args[2], args[3],
                                        submit="--submit" in args)))
            elif cmd == "query":
                print(json.dumps(c.query(int(args[1]), args[2]), indent=2)[:6000])
            else:
                print(f"unknown command: {cmd}")
                return 2
    except BridgeError as e:
        print(f"bridge error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
