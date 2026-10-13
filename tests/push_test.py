"""Web push from the host: a local stand-in for Apple's push service receives the requests, so the signed VAPID header, the
empty body, the endpoint allow-list, pruning and the triggers are all checked for real. Pure Python.
Run: python3 tests/push_test.py"""
import base64, http.server, importlib.util, json, os, sys, tempfile, threading, time
os.environ["SHEPR_STATE"] = tempfile.mkdtemp()
for _k in ("SHEPR_NEW_CMD", "SHEPR_CLAUDE_CMD", "SHEPR_CODEX_CMD", "SHEPR_PROJECTS", "SHEPR_PUSH_ALLOW", "SHEPR_READONLY"): os.environ.pop(_k, None)   # a test pins its own settings, not the shell's
os.environ["SHEPR_POLL"] = "15"
here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("shepr_server", os.path.join(here, "..", "server.py"))
srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)
ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name); ok = ok and bool(cond)

GOT, STATUS = [], {"code": 201, "location": None}
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        GOT.append({"path": self.path, "h": {k.lower(): v for k, v in self.headers.items()}, "body": self.rfile.read(n)})
        self.send_response(STATUS["code"])
        if STATUS["location"]: self.send_header("Location", STATUS["location"])
        self.end_headers()
    def log_message(self, *a): pass
def serve():
    s = http.server.HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=s.serve_forever, daemon=True).start(); return s, s.server_address[1]
S1, P1 = serve(); S2, P2 = serve()
srv.PUSH_EXTRA.add(f"127.0.0.1:{P1}")           # only the first stand-in is an allowed push host
EP = f"http://127.0.0.1:{P1}/push/abc"
u64 = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

# ---- the signing primitive, checked against the verifier the host already trusts
d, pub = srv.vapid_keys(); x, y = int.from_bytes(pub[1:33], "big"), int.from_bytes(pub[33:], "big")
check("sign: 25 random messages verify, and a changed message does not", all(srv.ecdsa_verify(x, y, m, srv.ecdsa_sign(d, m)) for m in (os.urandom(40) for _ in range(25))) and not srv.ecdsa_verify(x, y, b"other", srv.ecdsa_sign(d, b"msg")))
check("vapid: the public key is an uncompressed P-256 point on the curve", len(pub) == 65 and pub[0] == 4 and srv.on_curve(x, y))
mode = os.stat(srv.state_path("vapid.json")).st_mode & 0o777
srv._vapid.clear(); d2, pub2 = srv.vapid_keys()
check("vapid: the key is kept 0600 and is the same after a restart", mode == 0o600 and pub2 == pub and d2 == d)

# ---- subscribing
srv.push_subscribe("devA", {"subscription": {"endpoint": EP, "keys": {"p256dh": "k", "auth": "a"}}, "prefs": {"blocked": True, "done": False}})
check("subscribe: stored 0600 with the phone's preferences", os.stat(srv.state_path("push.json")).st_mode & 0o777 == 0o600 and srv.push_config("devA")["subscribed"] and srv.push_config("devA")["prefs"] == {"blocked": True, "done": False})
for bad in ("https://evil.example/x", "http://web.push.apple.com/x", "https://web.push.apple.com.evil.com/x", "https://127.0.0.1/x", "https://localhost/x",
            "https://user:pw@web.push.apple.com/x", "https://web.push.apple.com:8443/x", "http://127.0.0.1:1/x", "ftp://web.push.apple.com/x", "https://169.254.169.254/x", "https://web.push.apple.com/" + "a" * 800, ""):
    try: srv.push_subscribe("devX", {"subscription": {"endpoint": bad}}); accepted = True
    except srv.HerdrError: accepted = False
    if accepted: check(f"endpoint allow-list refuses {bad[:40]!r}", False)
check("endpoint allow-list: refuses plain http to Apple, look-alike hosts, IPs, localhost, credentials, odd ports, other schemes, and 800-char URLs", "devX" not in srv.PUSH)
check("endpoint allow-list: accepts the real push services", all(srv.push_endpoint_ok(u) for u in ("https://web.push.apple.com/abc", "https://a1.push.apple.com/abc", "https://fcm.googleapis.com/fcm/send/x", "https://updates.push.services.mozilla.com/x", "https://dm1.notify.windows.com/x")))

# ---- what actually goes out
code = srv.send_push("devA")
r = GOT[-1]
auth = r["h"]["authorization"]
t, k = auth[len("vapid t="):].split(", k=")
head, claims, sig = t.split(".")
hd, cl = json.loads(u64(head)), json.loads(u64(claims))
check("push: an empty POST, TTL and urgency set, answered 201", code == 201 and r["body"] == b"" and r["h"]["ttl"] == "3600" and r["h"]["urgency"] == "high" and r["path"] == "/push/abc")
check("push: Authorization is 'vapid t=<jwt>, k=<this host's public key>'", auth.startswith("vapid t=") and u64(k) == pub)
check("push: the JWT is ES256 for this push service, with a contact URL and an expiry within 24 h", hd == {"typ": "JWT", "alg": "ES256"} and cl["aud"] == f"http://127.0.0.1:{P1}" and cl["sub"] == srv.VAPID_SUB and time.time() < cl["exp"] <= time.time() + 86400)
check("push: the JWT signature verifies against the public key", srv.ecdsa_verify(x, y, (head + "." + claims).encode(), u64(sig)))
check("push: a JWT with a changed claim does NOT verify", not srv.ecdsa_verify(x, y, (head + "." + b64 if (b64 := base64.urlsafe_b64encode(b'{"aud":"x"}').rstrip(b"=").decode()) else "").encode(), u64(sig)))

# ---- what the push service can tell us
STATUS["code"] = 410
n = len(GOT); c = srv.send_push("devA")
check("a push service that says 410 Gone gets the subscription removed", c == 410 and not srv.push_config("devA")["subscribed"])
STATUS["code"] = 201
srv.push_subscribe("devA", {"subscription": {"endpoint": EP, "keys": {}}, "prefs": {"blocked": True, "done": False}})
STATUS["code"], STATUS["location"] = 302, f"http://127.0.0.1:{P2}/internal"
GOT2 = []; n2 = len(GOT)
c = srv.send_push("devA")
time.sleep(0.3)
check("a redirect is never followed (it could point at an internal address)", c == 302 and len(GOT) == n2 + 1)
STATUS["code"], STATUS["location"] = 201, None

# ---- who gets told what
srv.PUSH.clear(); srv.PUSH_LAST.clear()
srv.mutate_devices(lambda devs: devs.update({n: {"name": n, "x": "", "y": ""} for n in ("A", "B", "C")}))   # only a paired phone can subscribe
for dev, prefs in (("A", {"blocked": True, "done": False}), ("B", {"blocked": False, "done": True}), ("C", {"blocked": False, "done": False})):
    srv.push_subscribe(dev, {"subscription": {"endpoint": EP + "/" + dev, "keys": {}}, "prefs": prefs})
GOT.clear(); srv.dispatch_push(["blocked"]); who1 = sorted(g["path"][-1] for g in GOT)
GOT.clear(); srv.PUSH_LAST.clear(); srv.dispatch_push(["done"]); who2 = sorted(g["path"][-1] for g in GOT)
GOT.clear(); srv.PUSH_LAST.clear(); srv.dispatch_push(["blocked", "done"]); who3 = sorted(g["path"][-1] for g in GOT)
check("triggers: 'blocked' reaches only phones that want it, 'done' only those that want that, both reach both, none reach C", who1 == ["A"] and who2 == ["B"] and who3 == ["A", "B"])
GOT.clear(); srv.dispatch_push(["blocked", "done"])
check("triggers: a second burst within 20 s is not sent again (the phone reads the whole queue anyway)", GOT == [])

# ---- the poller only reports transitions it watched
host = {"id": "gus", "kind": "local", "machine": None, "enabled": True}
ag = lambda seq, st: {"pane_id": "w1:p1", "state_change_seq": seq, "agent_status": st, "cwd": "/x", "agent_session": {}}
srv.PUSH_EVENTS.clear(); srv.STATE["hosts"].clear(); srv.STATE["seen"].clear()
srv.note_seen(host, [ag(5, "blocked")], time.time())
first = list(srv.PUSH_EVENTS)
srv.note_seen(host, [ag(5, "blocked")], time.time() + 1)     # same state again
same = list(srv.PUSH_EVENTS)
srv.note_seen(host, [ag(6, "blocked")], time.time() + 2)     # a NEW blocked state we watched begin
new = list(srv.PUSH_EVENTS)
srv.note_seen(host, [ag(6, "blocked"), {**ag(1, "done"), "pane_id": "w2:p1"}], time.time() + 3)
fin = list(srv.PUSH_EVENTS)
check("poller: what was already blocked when the host started is not news", first == [] and same == [])
check("poller: a new blocked state and a new finished state, once each, are", new == ["blocked"] and fin == ["blocked", "done"])

# ---- a revoked phone stops getting pushes
srv.forget_push("A")
check("revoke: forgetting a device removes its subscription from disk too", "A" not in json.load(open(srv.state_path("push.json"))))
# `shepr revoke` runs in ANOTHER process: it edits devices.json and push.json, never this server's PUSH in memory
srv.push_subscribe("B", {"subscription": {"endpoint": EP + "/B", "keys": {}}, "prefs": {"blocked": True, "done": True}})
cli = ("import argparse, importlib.util; s = importlib.util.spec_from_file_location('m', %r); m = importlib.util.module_from_spec(s); "
       "s.loader.exec_module(m); m.cli_revoke(argparse.Namespace(who='B'))" % os.path.join(here, "..", "server.py"))
import subprocess
rv = subprocess.run([sys.executable, "-c", cli], capture_output=True, text=True)
GOT.clear(); srv.PUSH_LAST.clear(); srv.dispatch_push(["blocked", "done"])
check("revoke from the CLI: the revoked phone gets no more pushes from the running server", rv.returncode == 0 and not any(g["path"].endswith("/B") for g in GOT))
srv.push_subscribe("C", {"subscription": {"endpoint": EP + "/C", "keys": {}}, "prefs": {"blocked": True, "done": False}})
check("revoke from the CLI: a later save by the server does not write the revoked phone's subscription back", "B" not in json.load(open(srv.state_path("push.json"))))
sys.exit(0 if ok else 1)
