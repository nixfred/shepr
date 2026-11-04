"""Shepr Live: the Lock Screen Live Activity, host side. Pure Python (curl and a local stand-in for Apple's push service).
Checks the sealed payload against RFC 8439 and the `cryptography` library, the Apple provider token, the push headers and body,
when a push is sent and when it is not, what happens on each answer Apple can give, the HTTP routes, and the CLI.
Run: python3 tests/live_test.py            (python3 tests/live_test.py --fixture   prints the cross-language fixture the Swift tests read)"""
import base64, functools, hashlib, http.client, http.server, importlib.util, json, os, shutil, subprocess, sys, tempfile, threading, time
STATE = tempfile.mkdtemp()
os.environ["SHEPR_STATE"] = STATE
for _k in ("SHEPR_NEW_CMD", "SHEPR_CLAUDE_CMD", "SHEPR_CODEX_CMD", "SHEPR_PROJECTS", "SHEPR_PUSH_ALLOW", "SHEPR_READONLY", "SHEPR_APNS_URL"): os.environ.pop(_k, None)
os.environ["SHEPR_POLL"] = "15"
here = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(here, "..", "server.py")
spec = importlib.util.spec_from_file_location("shepr_server", SERVER_PY)
srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)
live = srv.shepr_live
ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name); ok = ok and bool(cond)
u64 = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

NOW = 1_700_000_000
KEY = bytes(range(32))
AG = [
    {"host": "gus", "pane": "w1:p1", "status": "blocked", "wlabel": "pipeline", "project": "pipe", "since": NOW - 700},
    {"host": "gus", "pane": "w2:p1", "status": "blocked", "wlabel": "", "project": "herder.remote.iphone", "since": NOW - 100},
    {"host": "vic", "pane": "w3:p1", "status": "done", "wlabel": "mkimage", "project": "x", "since": NOW - 300},
    {"host": "vic", "pane": "w4:p1", "status": "done", "wlabel": "old-one", "project": "x", "since": NOW - 5 * 3600},
    {"host": "gus", "pane": "w5:p1", "status": "working", "wlabel": "sigkill", "project": "s"},
    {"host": "gus", "pane": "w6:p1", "status": "idle", "wlabel": "idle-one", "project": "i"},
]
HOSTS = [{"id": "gus", "status": "ok", "asleep": 2}, {"id": "vic", "status": "down", "asleep": 0}]
VIEW = {"agents": [{"id": "claude", "name": "Claude", "rows": [{"label": "Session (5-hour)", "used": 0.63, "resets_ms": (NOW + 5400) * 1000},
                                                          {"label": "Weekly", "used": 0.2, "resets_ms": 0}]},
                   {"id": "codex", "name": "Codex", "rows": []}]}
if "--fixture" in sys.argv:
    st = live.build_status(AG, HOSTS, live.meters_from_view(VIEW), NOW)
    print(json.dumps({"key": KEY.hex(), "nonce": bytes(range(12)).hex(), "plain": json.dumps(st, separators=(",", ":"), ensure_ascii=False), "status": st, "sealed": live.seal_status(st, KEY, bytes(range(12)))}, indent=1))
    sys.exit(0)

# ---- the cipher: RFC 8439 vectors, then the cryptography library on random inputs
blk = live.chacha20_block(bytes(range(32)), 1, bytes.fromhex("000000090000004a00000000"))
check("chacha20 block matches RFC 8439 2.3.2", blk.hex() == "10f1e7e4d13b5915500fdd1fa32071c4c7d1f4c733c068030422aa9ac3d46c4ed2826446079faa0914c2d705d98b02a2b5129cd1de164eb9cbd083e8a2503c4e")
check("poly1305 matches RFC 8439 2.5.2", live.poly1305(bytes.fromhex("85d6be7857556d337f4452fe42d506a80103808afb0db2fd4abff6af4149f51b"), b"Cryptographic Forum Research Group").hex() == "a8061dc1305136c6c22b8baf0c0127a9")
pt = b"Ladies and Gentlemen of the class of '99: If I could offer you only one tip for the future, sunscreen would be it."
aead = live.aead_seal(bytes(range(0x80, 0xa0)), bytes.fromhex("070000004041424344454647"), pt, bytes.fromhex("50515253c0c1c2c3c4c5c6c7"))
check("aead matches RFC 8439 2.8.2 (ciphertext start and tag)", aead[:16].hex() == "d31a8d34648e60db7b86afbc53ef7ec2" and aead[-16:].hex() == "1ae10b594f09e26a7e902ecbd0600691")
try:
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    same = True
    for n in (0, 1, 15, 16, 17, 63, 64, 65, 130, 300, 1000):
        k, nc, a, m = os.urandom(32), os.urandom(12), os.urandom(n % 20), os.urandom(n)
        same = same and live.aead_seal(k, nc, m, a) == ChaCha20Poly1305(k).encrypt(nc, m, a) and live.aead_open(k, nc, ChaCha20Poly1305(k).encrypt(nc, m, a), a) == m
    check("aead equals the cryptography library for 11 lengths, both directions", same)
except ImportError:
    print("SKIP cross-check against the cryptography library (not installed)")
tag_bad = bytearray(aead); tag_bad[3] ^= 1
def raises(fn, exc=Exception):
    try: fn()
    except exc: return True
    return False
check("aead refuses a flipped ciphertext bit, another AAD and the wrong key",
      raises(lambda: live.aead_open(bytes(range(0x80, 0xa0)), bytes.fromhex("070000004041424344454647"), bytes(tag_bad), bytes.fromhex("50515253c0c1c2c3c4c5c6c7")))
      and raises(lambda: live.aead_open(bytes(range(0x80, 0xa0)), bytes.fromhex("070000004041424344454647"), aead, b"other"))
      and raises(lambda: live.aead_open(bytes(32), bytes.fromhex("070000004041424344454647"), aead, bytes.fromhex("50515253c0c1c2c3c4c5c6c7"))))
st0 = live.build_status(AG, HOSTS, live.meters_from_view(VIEW), NOW)
sealed = live.seal_status(st0, KEY)
check("a sealed status opens to the same status, and two seals of it differ", live.open_status(sealed, KEY) == st0 and live.seal_status(st0, KEY) != sealed)
check("the sealed text is nonce + ciphertext + tag, and holds no session name", len(base64.b64decode(sealed)) == 12 + len(json.dumps(st0, separators=(",", ":"), ensure_ascii=False).encode()) + 16 and "pipeline" not in sealed)

# ---- the Apple key
def fake_p8(d):
    der = bytes.fromhex("3041020100301306072a8648ce3d020106082a8648ce3d030107042730250201010420") + d.to_bytes(32, "big")
    begin, end = "-----BEGIN " + "PRIVATE KEY-----\n", "-----END " + "PRIVATE KEY-----\n"   # built from parts so a secret scanner does not mistake this made-up key for a real one
    return begin + base64.encodebytes(der).decode() + end
P8SCALAR = 0x1F2E3D4C5B6A79880123456789ABCDEF0F1E2D3C4B5A69788796A5B4C3D2E1F0 % live.N
P8 = os.path.join(STATE, "AuthKey_ABCDE12345.p8")
with open(P8, "w") as f: f.write(fake_p8(P8SCALAR))
check("a .p8 key gives back its private scalar", live.load_p8(P8) == P8SCALAR)
if shutil.which("openssl"):
    pem = os.path.join(STATE, "real.pem")
    subprocess.run(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", pem], check=True, capture_output=True)
    pub = subprocess.run(["openssl", "pkey", "-in", pem, "-pubout", "-outform", "DER"], check=True, capture_output=True).stdout[-65:]
    d2 = live.load_p8(pem); x2, y2 = srv._mul(d2, srv._G)
    check("a key made by openssl (with its public half inside) is read correctly", pub == b"\x04" + x2.to_bytes(32, "big") + y2.to_bytes(32, "big"))
bad = os.path.join(STATE, "bad.p8"); open(bad, "w").write("-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n")
check("a file that is not a P-256 key is refused", raises(lambda: live.load_p8(bad), live.LiveError) and raises(lambda: live.load_p8(os.path.join(STATE, "nope")), live.LiveError))

# ---- the status
check("status: counts are blocked 2, finished 1 (the old one is idle now), working 1, idle 2", (st0["b"], st0["d"], st0["w"], st0["i"]) == (2, 1, 1, 2))
check("status: rows are longest-blocked first, then finished, then working, named by workspace else project",
      [(r["n"], r["s"]) for r in st0["r"]] == [("pipeline", "b"), ("herder.remote.iphone", "b"), ("mkimage", "d"), ("sigkill", "w")])
check("status: a blocked row carries when it began, so the phone can count up by itself", st0["r"][0]["a"] == NOW - 700 and st0["r"][3]["a"] == 0)
check("status: hosts show who answers, sleepers are summed", st0["h"] == [{"n": "gus", "o": True}, {"n": "vic", "o": False}] and st0["z"] == 2)
check("status: the meter shows each plan's busiest window with a short label", st0["m"] == [{"n": "Claude", "l": "5h", "p": 63, "e": NOW + 5400}])
check("status: no conversation text or paths travel, only names", all(set(r) == {"n", "h", "s", "a"} for r in st0["r"]))
many = [{"host": "gus", "pane": f"w{i}", "status": ["blocked", "done", "working"][i % 3], "wlabel": "x" * 80, "since": NOW - i} for i in range(60)]
big = live.build_status(many, HOSTS, live.meters_from_view(VIEW), NOW)
check("status: sixty sessions with 80-character names stay at eight rows of 32 characters", len(big["r"]) == 8 and all(len(r["n"]) <= 32 for r in big["r"]))
same_time = live.build_status(AG, HOSTS, live.meters_from_view(VIEW), NOW + 99)
check("status signature ignores the build time but notices a change", live.status_sig(st0) == live.status_sig(same_time) and live.status_sig(st0) != live.status_sig(dict(st0, w=2)))

# ---- the push body
for ev in ("start", "update", "end"):
    b = json.loads(live.build_payload(ev, st0, KEY, NOW, "gus"))["aps"]
    exp = {"timestamp": NOW, "event": ev}
    good = b["timestamp"] == NOW and b["event"] == ev and live.open_status(b["content-state"]["sealed"], KEY) == st0
    if ev == "start": good = good and b["attributes-type"] == "ShepActivityAttributes" and b["attributes"] == {"host": "gus"} and "alert" in b and b["stale-date"] == NOW + live.STALE_S
    if ev == "update": good = good and "alert" not in b and b["stale-date"] == NOW + live.STALE_S
    if ev == "end": good = good and b["dismissal-date"] == NOW and "stale-date" not in b
    check(f"payload ({ev}) has the keys Apple documents", good)
huge = dict(st0, r=[{"n": "n" * 32, "h": "gus", "s": "w", "a": 0}] * 300)
body = live.build_payload("update", huge, KEY, NOW, "gus")
check("an oversize status drops rows until the push fits Apple's 4096 bytes", len(body.encode()) <= live.MAX_PAYLOAD and live.open_status(json.loads(body)["aps"]["content-state"]["sealed"], KEY)["r"] != huge["r"])

# ---- a local stand-in for Apple
GOT, ANSWERS = [], []
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        GOT.append({"path": self.path, "h": {k.lower(): v for k, v in self.headers.items()}, "body": self.rfile.read(n).decode()})
        code, text = ANSWERS.pop(0) if ANSWERS else (200, "")
        self.send_response(code); self.send_header("apns-id", "AAAAAAAA-0000-0000-0000-000000000000"); self.send_header("Content-Length", str(len(text))); self.end_headers(); self.wfile.write(text.encode())
    def log_message(self, *a): pass
APNS = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=APNS.serve_forever, daemon=True).start()
URL = "http://127.0.0.1:%d" % APNS.server_address[1]
def take():
    out = GOT[:]; GOT.clear(); return out
def jwt_info(auth):
    h, p, s = auth.split(" ", 1)[1].split(".")
    x, y = srv._mul(P8SCALAR, srv._G)
    return srv.ecdsa_verify(x, y, (h + "." + p).encode(), u64(s)), json.loads(u64(h)), json.loads(u64(p))

T = [float(NOW)]
VALID = {"ids": None}
L = live.Live(tempfile.mkdtemp(), srv.ecdsa_sign, "gus", valid=lambda: VALID["ids"], apns_url=URL, clock=lambda: T[0], log=lambda m: None)
D1, D2 = "a1" * 8, "b2" * 8
TOK1, TOK2, TOKS = "ab" * 32, "cd" * 32, "ef" * 32
ACT1, ACT2 = "11111111-AAAA", "22222222-BBBB"
K1B = base64.b64encode(KEY).decode()
check("before setup nothing is configured, and process sends nothing", not L.configured() and L.process(st0) == [] and take() == [])
L.register(D1, {"kind": "update", "token": TOK1, "id": ACT1, "env": "sandbox", "key": K1B})
for bad_body in ({"kind": "x", "token": TOK1, "env": "sandbox", "key": K1B}, {"kind": "start", "token": "zz", "env": "sandbox", "key": K1B},
                 {"kind": "start", "token": TOK1, "env": "moon", "key": K1B}, {"kind": "start", "token": TOK1, "env": "sandbox", "key": "AAAA"},
                 {"kind": "update", "token": TOK1, "env": "sandbox", "key": K1B}):
    if not raises(lambda: L.register(D1, bad_body), live.LiveError): check("register refuses " + json.dumps(bad_body)[:50], False)
check("register refuses a bad kind, token, env, key and a missing activity id", True)
check("setup refuses a bad key id", raises(lambda: L.setup(P8, "short", "TEAM123456", "com.example.shepr"), live.LiveError))
L.setup(P8, "ABCDE12345", "TEAM123456", "com.example.shepr")
modes = [os.stat(os.path.join(L.dir, n)).st_mode & 0o777 for n in ("AuthKey.p8", "apns.json", "live.json")]
check("the Apple key, its settings and the token file are mode 0600", modes == [0o600] * 3)
check("configured now, and the phone shows one card", L.configured() and L.push_info(D1)["activities"] == 1 and L.push_info(D1)["start"] is False)

res = L.process(st0); rq = take()
check("first status: exactly one update goes out", len(res) == 1 and len(rq) == 1)
r = rq[0]; h = r["h"]; aps = json.loads(r["body"])["aps"]
check("push goes to the card's token path", r["path"] == "/3/device/" + TOK1)
check("headers: liveactivity type, the bundle's liveactivity topic, priority 10 for 'someone is blocked', an expiry",
      h["apns-push-type"] == "liveactivity" and h["apns-topic"] == "com.example.shepr.push-type.liveactivity" and h["apns-priority"] == "10" and int(h["apns-expiration"]) == NOW + 600)
good, jh, jp = jwt_info(h["authorization"])
check("the provider token is ES256 over the key id and team id, signed by the .p8 key", good and jh == {"alg": "ES256", "kid": "ABCDE12345"} and jp == {"iss": "TEAM123456", "iat": NOW})
check("the body is an update whose sealed status opens to ours", aps["event"] == "update" and live.open_status(aps["content-state"]["sealed"], KEY) == st0 and len(r["body"]) < 4096)
check("no session name is in the clear on the wire", not any(w in r["body"] for w in ("pipeline", "mkimage", "sigkill", "herder")))
check("an unchanged status sends nothing", L.process(st0) == [] and take() == [])
st1 = dict(st0, w=2)
T[0] += 3
check("a change inside 10 seconds waits", L.process(st1) == [] and take() == [])
T[0] += 8
res = L.process(st1); rq = take()
check("after the gap it goes, at the cheap priority 5", len(rq) == 1 and rq[0]["h"]["apns-priority"] == "5")
T[0] += 20; st2 = dict(st1, b=3)
L.process(st2); rq = take()
check("one more blocked session is priority 10", len(rq) == 1 and rq[0]["h"]["apns-priority"] == "10")
T[0] += 20; st3 = dict(st1, b=0)
L.process(st3); rq = take()
check("blocked going to zero is priority 10", len(rq) == 1 and rq[0]["h"]["apns-priority"] == "10")
T[0] += live.HEARTBEAT_S + 1
L.process(st3); rq = take()
check("an unchanged status is still re-sent after 20 minutes, at priority 5, so a quiet card is not marked stale", len(rq) == 1 and rq[0]["h"]["apns-priority"] == "5")
T[0] += 20; ANSWERS.append((410, '{"reason":"Unregistered"}'))
L.process(dict(st3, w=5)); take()
check("Apple saying the card is gone drops it, and the host does not restart it by itself", L.push_info(D1)["activities"] == 0 and L._load()[D1]["auto"] is False and L.process(dict(st3, w=6)) == [])
L.register(D1, {"kind": "update", "token": TOK1, "id": ACT1, "env": "sandbox", "key": K1B})
check("starting a card again turns the host's care back on", L._load()[D1]["auto"] is True)
T[0] += 20; ANSWERS.append((429, '{"reason":"TooManyRequests"}'))
L.process(dict(st3, w=7)); take(); T[0] += 15
check("Apple saying slow down holds that card for a minute", L.process(dict(st3, w=7)) == [] and take() == [])
T[0] += live.BACKOFF_S
check("then it is tried again", len(L.process(dict(st3, w=7))) == 1 and len(take()) == 1)
T[0] += 20; ANSWERS.extend([(403, '{"reason":"ExpiredProviderToken"}'), (200, "")])
res = L.process(dict(st3, w=8)); rq = take()
check("an expired provider token is renewed and the push retried once", len(rq) == 2 and res[0]["code"] == 200)

# a push-to-start token and nothing running
L.register(D2, {"kind": "start", "token": TOKS, "env": "production", "key": K1B})
check("a phone with only a start token reports it", L.push_info(D2) == {"configured": True, "env": "production", "start": True, "activities": 0, "last": None})
T[0] += 20
check("with nobody blocked, nothing starts (and the host learns the blocked count)", [x for x in L.process(dict(st3, b=0)) if x["device"] == D2] == [])
T[0] += 20; L.process(dict(st3, b=1)); rq = [x for x in take() if x["path"].endswith(TOKS)]
aps = json.loads(rq[0]["body"])["aps"] if rq else {}
check("a session becoming blocked starts a card with the push-to-start token", len(rq) == 1 and aps.get("event") == "start" and aps.get("attributes-type") == "ShepActivityAttributes" and aps.get("attributes") == {"host": "gus"} and "alert" in aps and rq[0]["h"]["apns-priority"] == "10")
T[0] += 20; L.process(dict(st3, b=2))
check("a second one right after does not start another", [x for x in take() if x["path"].endswith(TOKS)] == [])
# rollover before Apple's 8 hour limit
L.register(D2, {"kind": "update", "token": TOK2, "id": ACT2, "env": "production", "key": K1B})
T[0] += live.ROLL_S + 1
res = [x for x in L.process(dict(st3, b=2)) if x["device"] == D2]
ev = [json.loads(x["body"])["aps"]["event"] for x in take() if x["path"].endswith(TOK2) or x["path"].endswith(TOKS)]
check("after 7h45m the card is ended and a fresh one started", ev == ["end", "start"] and [x["event"] for x in res] == ["end", "start"])
check("the ended card is forgotten", L.push_info(D2)["activities"] == 0)
VALID["ids"] = {D1}
L.process(dict(st3, b=2))
check("a revoked phone is dropped from the host's list", D2 not in L._load() and D1 in L._load())
VALID["ids"] = None
out = L.test(D1); rq = take()
check("test sends a plain card to a phone's running cards", len(out) == 1 and out[0]["code"] == 200 and live.open_status(json.loads(rq[0]["body"])["aps"]["content-state"]["sealed"], KEY)["x"].startswith("Test from the host"))
check("unregister with no id ends all of a phone's cards and stops auto-start", L.unregister(D1)["activities"] == 0 and L._load()[D1]["auto"] is False)
check("test refuses when no key is set up", raises(lambda: live.Live(tempfile.mkdtemp(), srv.ecdsa_sign, "gus").test(), live.LiveError))

# ---- the HTTP routes, on the real handler with a real signed client
httpd = srv.CappedServer(("127.0.0.1", 0), functools.partial(srv.Handler, directory=srv.HERE))
threading.Thread(target=httpd.serve_forever, daemon=True).start()
PORT = httpd.server_address[1]
srv.OWNER["names"].add("127.0.0.1"); srv.OWNER["login"] = None
CD = 0x0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF % srv._N
cx, cy = srv._mul(CD, srv._G)
DID = hashlib.sha256(b"\x04" + cx.to_bytes(32, "big") + cy.to_bytes(32, "big")).hexdigest()[:16]
srv.mutate_devices(lambda ds: ds.__setitem__(DID, {"name": "Test phone", "x": srv.b64u(cx.to_bytes(32, "big")), "y": srv.b64u(cy.to_bytes(32, "big")), "added": NOW, "last_seen": int(time.time()), "ua": ""}))
def call(method, path, obj=None, signed=True):
    raw = json.dumps(obj).encode() if obj is not None else b""
    hd = {"Host": "127.0.0.1:%d" % PORT}
    if method == "POST": hd.update({"Content-Type": "application/json", "X-Shepr": "1"})
    if signed:
        ts, nonce = str(int(time.time() * 1000)), live.b64u(os.urandom(12))
        msg = "\n".join([ts, nonce, method, path, hashlib.sha256(raw).hexdigest()]).encode()
        hd.update({"X-Shepr-Device": DID, "X-Shepr-Ts": ts, "X-Shepr-Nonce": nonce, "X-Shepr-Sig": srv.b64u(srv.ecdsa_sign(CD, msg))})
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=20)
    c.request(method, path, body=raw if method == "POST" else None, headers=hd)
    r = c.getresponse(); data = r.read(); c.close()
    try: return r.status, json.loads(data)
    except ValueError: return r.status, data
code, j = call("GET", "/api/live/status", signed=False)
check("live status needs a signature", code in (401, 403) )
code, j = call("GET", "/api/live/status")
check("live status answers a signed phone with the status, the push block and the host name", code == 200 and set(j) == {"status", "push", "host"} and set(j["status"]) >= {"v", "t", "b", "d", "w", "i", "z", "h", "r", "m"} and j["push"]["configured"] is False)
code, j = call("POST", "/api/live/register", {"kind": "update", "token": "nothex", "id": ACT1, "env": "sandbox", "key": K1B})
check("register refuses a bad token with a 400", code == 400 and j["error"] == "bad_request")
code, j = call("POST", "/api/live/register", {"kind": "update", "token": TOK1, "id": ACT1, "env": "sandbox", "key": K1B})
check("register accepts a card's token", code == 200 and j["activities"] == 1 and j["configured"] is False)
code, j = call("POST", "/api/live/test", {})
check("test says plainly that no Apple key is set up", code == 400 and j["error"] == "not_configured")
srv.LIVE.setup(P8, "ABCDE12345", "TEAM123456", "com.example.shepr"); srv.LIVE.apns_url = URL
take(); ANSWERS.clear()
code, j = call("POST", "/api/live/test", {}); rq = take()
check("test pushes to the phone's card through Apple's stand-in", code == 200 and j["ok"] is True and len(rq) == 1 and rq[0]["path"] == "/3/device/" + TOK1)
code, j = call("POST", "/api/live/unregister", {"id": ACT1})
check("unregister removes the card", code == 200 and j["activities"] == 0)
srv.SNAP.update(ts=time.time(), polls=1, hosts=[{"id": "gus", "kind": "local", "status": "ok", "error": None, "counts": {}, "asleep": 0}],
                agents=[{"host": "gus", "pane": "w1:p1", "agent": "claude", "status": "blocked", "seq": 5, "title": "a conversation title", "cwd": "/home/x", "project": "proj", "workspace": "ws", "wlabel": "ws", "session": "", "focused": False, "ask": None}])
code, j = call("GET", "/api/live/status")
check("the route's status is built from the live queue and carries no title or path", j["status"]["b"] == 1 and j["status"]["r"][0]["n"] == "ws" and "conversation" not in json.dumps(j) and "/home/x" not in json.dumps(j))
srv._publish()
check("the poller's publish hook runs without a card set up and without harm", True)

# ---- the CLI
env = dict(os.environ, SHEPR_STATE=tempfile.mkdtemp())
def cli(*a):
    p = subprocess.run([sys.executable, SERVER_PY, "live", *a], capture_output=True, text=True, env=env, timeout=60)
    return p.returncode, p.stdout + p.stderr
rc, out = cli("status")
check("shepr live status says Apple push is not set up", rc == 0 and "not set up" in out)
rc, out = cli("setup", "--key", P8, "--key-id", "BADID", "--team-id", "TEAM123456")
check("shepr live setup refuses a bad key id", rc != 0 and "key id" in out)
rc, out = cli("setup", "--key", P8, "--key-id", "ABCDE12345", "--team-id", "TEAM123456")
check("shepr live setup saves the key (bundle defaults to com.nixfred.shepr.live)", rc == 0 and "saved" in out and json.load(open(os.path.join(env["SHEPR_STATE"], "apns.json")))["bundle"] == "com.nixfred.shepr.live")
rc, out = cli("status")
check("shepr live status then says it is set up", rc == 0 and "set up (key ABCDE12345" in out)
rc, out = cli("test")
check("shepr live test with no running card says to tap Start", "Start" in out)
rc, out = cli("forget", "zz")
check("shepr live forget with no match says so", rc == 1 and "no phone" in out)
print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
sys.exit(0 if ok else 1)
