#!/usr/bin/env python3
"""Prove bridge.py's WebSocket server is correct, and that its guards hold —
without needing Chrome. A fake client speaks real RFC6455 from a spoofed
chrome-extension:// origin.

Tests the parts I cannot verify by loading the extension: handshake maths,
frame encode/decode at every length class, masking, ping/pong, and the two
security checks that stand between a stray web page and a logged-in browser.
"""
import base64, hashlib, json, os, socket, struct, sys, threading, time
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path.home() / "Documents/GitHub/rappter-chrome"))
import bridge

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))

TOK = bridge.token()
PORT = 8911

print("\n0. CONCURRENT TOKEN CREATION PUBLISHES ONE COMPLETE VALUE")
original_dir, original_file = bridge.CONF_DIR, bridge.TOKEN_FILE
token_dir = Path(tempfile.mkdtemp(prefix="bridge-token-test-"))
bridge.CONF_DIR = token_dir
bridge.TOKEN_FILE = token_dir / "token"
values = []
threads = [threading.Thread(target=lambda: values.append(bridge.token())) for _ in range(20)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
check("all token callers agree", len(set(values)) == 1)
check("published token is complete", bridge.TOKEN_FILE.read_text().strip() == values[0])
bridge.CONF_DIR, bridge.TOKEN_FILE = original_dir, original_file

# ── a minimal client that behaves like the browser ──────────────────────────
def client(origin="chrome-extension://abcdef", tok=None, on_open=None):
    s = socket.create_connection(("127.0.0.1", PORT), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (f"GET /?token={tok if tok is not None else TOK} HTTP/1.1\r\n"
           f"Host: 127.0.0.1:{PORT}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
           f"Origin: {origin}\r\n\r\n")
    s.sendall(req.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = s.recv(4096)
        if not chunk:
            return s, None, key
        resp += chunk
    return s, resp.decode("latin-1"), key

def send_masked(sock, payload, opcode=0x1):
    if isinstance(payload, str): payload = payload.encode()
    mask = os.urandom(4)
    masked = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
    h = bytearray([0x80 | opcode]); n = len(payload)
    if n < 126: h.append(0x80 | n)
    elif n < (1 << 16): h.append(0x80 | 126); h += struct.pack(">H", n)
    else: h.append(0x80 | 127); h += struct.pack(">Q", n)
    sock.sendall(bytes(h) + mask + masked)

def read_server_frame(sock):
    b1, b2 = sock.recv(2)
    op = b1 & 0x0F; n = b2 & 0x7F
    if n == 126: n = struct.unpack(">H", sock.recv(2))[0]
    elif n == 127: n = struct.unpack(">Q", sock.recv(8))[0]
    buf = b""
    while len(buf) < n:
        buf += sock.recv(n - len(buf))
    return op, buf

print("\n1. HANDSHAKE + ROUND TRIP")
result = {}
def serve():
    try:
        with bridge.Chrome(port=PORT, wait=10) as c:
            result["tabs"] = c.call("tabs")
            result["big"] = c.call("big")
    except Exception as e:
        result["error"] = str(e)

t = threading.Thread(target=serve, daemon=True); t.start()
time.sleep(0.4)
sock, resp, key = client()
check("101 Switching Protocols", resp and "101" in resp.split("\r\n")[0], resp.split("\r\n")[0] if resp else "no response")
expect = base64.b64encode(hashlib.sha1((key + bridge.GUID).encode()).digest()).decode()
check("Sec-WebSocket-Accept is correct", expect in (resp or ""))

send_masked(sock, json.dumps({"hello": "test", "version": 1}))
op, data = read_server_frame(sock)
req = json.loads(data)
check("server sent a well-formed request", req.get("cmd") == "tabs", str(req))
send_masked(sock, json.dumps({"id": req["id"], "ok": True,
                              "result": [{"tabId": 7, "title": "t", "url": "u"}]}))

# a payload past the 16-bit boundary, to exercise the 64-bit length path
op, data = read_server_frame(sock)
req2 = json.loads(data)
big = "x" * 70000
send_masked(sock, json.dumps({"id": req2["id"], "ok": True, "result": big}))
t.join(timeout=10)
check("small payload round trip", result.get("tabs") == [{"tabId": 7, "title": "t", "url": "u"}])
check("70KB payload round trip (64-bit length)", result.get("big") == big,
      f"got {len(result.get('big') or '')} bytes")
sock.close()

print("\n2. PING/PONG keeps the session alive")
result2 = {}
def serve2():
    try:
        with bridge.Chrome(port=PORT, wait=10) as c:
            result2["r"] = c.call("ping")
    except Exception as e:
        result2["error"] = str(e)
t2 = threading.Thread(target=serve2, daemon=True); t2.start()
time.sleep(0.4)
sock2, resp2, _ = client()
send_masked(sock2, json.dumps({"hello": "test"}))
op, data = read_server_frame(sock2)
rid = json.loads(data)["id"]
send_masked(sock2, b"keepalive", opcode=0x9)          # client ping
op_pong, pong = read_server_frame(sock2)
check("server answers ping with pong", op_pong == 0xA and pong == b"keepalive",
      f"opcode={op_pong}")
send_masked(sock2, json.dumps({"id": rid, "ok": True, "result": {"pong": True}}))
t2.join(timeout=10)
check("call still resolves after a ping", result2.get("r") == {"pong": True})
sock2.close()

print("\n2B. UNMASKED CLIENT FRAMES ARE REJECTED")
left, right = socket.socketpair()
right.sendall(b"\x81\x05hello")
try:
    bridge._read_frame(left)
    check("unmasked frame rejected", False)
except bridge.BridgeError as exc:
    check("unmasked frame rejected", "masked" in str(exc))
finally:
    left.close()
    right.close()

print("\n3. A WEB PAGE CANNOT DRIVE YOUR BROWSER")
err = {}
def serve3():
    try:
        with bridge.Chrome(port=PORT, wait=10) as c:
            err["opened"] = True
    except Exception as e:
        err["msg"] = str(e)
t3 = threading.Thread(target=serve3, daemon=True); t3.start()
time.sleep(0.4)
sock3, resp3, _ = client(origin="https://evil.example.com")
check("rejected at HTTP layer", resp3 and "403" in resp3.split("\r\n")[0],
      resp3.split("\r\n")[0] if resp3 else "closed")
t3.join(timeout=10)
check("server refused the origin", "origin" in (err.get("msg") or "").lower(), err.get("msg", "")[:70])
sock3.close()

print("\n4. WRONG TOKEN IS REFUSED")
err4 = {}
def serve4():
    try:
        with bridge.Chrome(port=PORT, wait=10) as c:
            err4["opened"] = True
    except Exception as e:
        err4["msg"] = str(e)
t4 = threading.Thread(target=serve4, daemon=True); t4.start()
time.sleep(0.4)
sock4, resp4, _ = client(tok="not-the-token")
check("rejected at HTTP layer", resp4 and "401" in resp4.split("\r\n")[0],
      resp4.split("\r\n")[0] if resp4 else "closed")
t4.join(timeout=10)
check("server refused the token", "token" in (err4.get("msg") or "").lower(), err4.get("msg", "")[:70])
sock4.close()

print("\n5. PRE-HEADER DISCONNECT CANNOT HANG THE SERVER")
err5 = {}
def serve5():
    try:
        with bridge.Chrome(port=PORT, wait=10):
            err5["opened"] = True
    except Exception as e:
        err5["msg"] = str(e)
t5 = threading.Thread(target=serve5, daemon=True); t5.start()
time.sleep(0.4)
aborted = socket.create_connection(("127.0.0.1", PORT), timeout=10)
aborted.close()
t5.join(timeout=3)
check("handshake thread exits after peer EOF", not t5.is_alive())
check("handshake reports peer close", "closed" in (err5.get("msg") or "").lower(),
      err5.get("msg", "")[:70])

print("\n6. ERRORS FROM THE PAGE SURFACE AS EXCEPTIONS")
err5 = {}
def serve6():
    try:
        with bridge.Chrome(port=PORT, wait=10) as c:
            c.call("click", selector="#nope")
            err5["raised"] = False
    except bridge.BridgeError as e:
        err5["raised"] = True; err5["msg"] = str(e)
    except Exception as e:
        err5["other"] = str(e)
t6 = threading.Thread(target=serve6, daemon=True); t6.start()
time.sleep(0.4)
sock5, _, _ = client()
send_masked(sock5, json.dumps({"hello": "test"}))
op, data = read_server_frame(sock5)
rid5 = json.loads(data)["id"]
send_masked(sock5, json.dumps({"id": rid5, "ok": False, "error": "no element for #nope"}))
t6.join(timeout=10)
check("raises rather than returning the error text", err5.get("raised") is True,
      err5.get("msg", err5.get("other", ""))[:70])
sock5.close()

print(f"\n{'='*60}\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
