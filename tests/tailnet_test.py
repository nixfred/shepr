#!/usr/bin/env python3
"""`server.py PORT --tailnet`: the host serves its tailnet address itself. Own TLS (the certificate `tailscale cert` hands
out), every connection checked with `tailscale whois` before a byte of it is read, the listener tied to one interface,
connections kept between requests only when that is safe.

Nothing here touches a real tailnet. A fake `tailscale` on PATH answers status, whois and cert from a fixture file; the
"tailnet address" is 127.0.0.2 on lo, and a test chooses which device it is by the loopback address it connects from.
The certificate is self-signed, made with the openssl command (the TLS checks are skipped, and say so, without it).
Run: python3 tests/tailnet_test.py"""
import argparse, contextlib, functools, hashlib, http.client, importlib.util, io, json, os, shutil, socket, ssl, stat, subprocess, sys, tempfile, threading, time

here = os.path.dirname(os.path.abspath(__file__))
os.environ["SHEPR_STATE"] = tempfile.mkdtemp()
for _k in ("SHEPR_NEW_CMD", "SHEPR_CLAUDE_CMD", "SHEPR_CODEX_CMD", "SHEPR_PROJECTS", "SHEPR_PUSH_ALLOW", "SHEPR_READONLY", "SHEPR_OWNER",
           "SHEPR_ALLOW_HOSTS", "SHEPR_ALLOW_PROXY", "SHEPR_TAILNET", "SHEPR_TAILNET_DEV", "SHEPR_KEEPALIVE", "SHEPR_URL"):
    os.environ.pop(_k, None)
TMP = tempfile.mkdtemp()
BIN = os.path.join(TMP, "bin"); os.makedirs(BIN)
os.environ["FAKE_TS_DIR"] = TMP
os.environ["PATH"] = BIN + os.pathsep + os.environ["PATH"]
REAL_STDERR = sys.stderr
sys.excepthook = lambda *a: __import__("traceback").print_exception(*a, file=REAL_STDERR)   # a mistake in this file is shown even while the server's journal is captured

FAKE = r'''#!/usr/bin/env python3
"""A stand-in for the tailscale command: status, whois and cert, answered from $FAKE_TS_DIR/fake.json."""
import json, os, shutil, sys, time
d = os.environ["FAKE_TS_DIR"]
fx = json.load(open(os.path.join(d, "fake.json")))
a = sys.argv[1:]
with open(os.path.join(d, "calls.log"), "a") as f:
    f.write(" ".join(a) + "\n")
if a[:2] == ["status", "--json"]:
    if fx.get("status") is None:
        print("failed to connect to local tailscaled; it doesn't appear to be running", file=sys.stderr); sys.exit(1)
    print(json.dumps(fx["status"])); sys.exit(0)
if a[:2] == ["whois", "--json"]:
    ip = a[2]
    if ip in fx.get("hang", []):
        time.sleep(20)
    if ip in fx.get("garbage", []):
        print("not json at all"); sys.exit(0)
    w = fx.get("whois", {}).get(ip)
    if w is None:
        print("peer not found", file=sys.stderr); sys.exit(1)
    print(json.dumps(w)); sys.exit(0)
if a[:3] == ["serve", "status", "--json"]:
    sv = fx.get("serve", {})
    if sv == "garbage":
        print("no json here"); sys.exit(0)
    print(json.dumps(sv)); sys.exit(0)
if a[:1] == ["cert"]:
    how = fx.get("cert", "ok")
    if how == "denied":
        print("Access denied: cert access denied", file=sys.stderr); sys.exit(1)
    if how == "nohttps":
        print("500 Internal Server Error: your Tailscale account does not support getting TLS certs (HTTPS is off)", file=sys.stderr); sys.exit(1)
    crt, key = a[a.index("--cert-file") + 1], a[a.index("--key-file") + 1]
    shutil.copyfile(os.path.join(d, fx.get("crt", "a.crt")), crt)
    shutil.copyfile(os.path.join(d, fx.get("key", "a.key")), key)
    os.chmod(key, 0o644)   # the server must close it up itself
    print("Wrote public cert to", crt); print("Wrote private key to", key); sys.exit(0)
sys.exit(2)
'''
with open(os.path.join(BIN, "tailscale"), "w") as f:
    f.write(FAKE)
os.chmod(os.path.join(BIN, "tailscale"), 0o755)

DNS, OWNER_LOGIN, TS_IP = "box.tailnet.ts.net", "fred@example.com", "127.0.0.2"
PHONE, TAGGED, OTHER, UNKNOWN, HANGS, GARBAGE = "127.0.0.10", "127.0.0.11", "127.0.0.12", "127.0.0.13", "127.0.0.14", "127.0.0.15"


def who(login, node, tags=None):
    return {"Node": {"ComputedName": node, "Name": node + ".tailnet.ts.net.", "Tags": tags}, "UserProfile": {"LoginName": login}, "CapMap": {}}


FX = {"status": {"BackendState": "Running", "Self": {"DNSName": DNS + ".", "UserID": 7, "TailscaleIPs": [TS_IP]},
                 "User": {"7": {"LoginName": "Fred@Example.com"}}},
      "whois": {PHONE: who("Fred@Example.com", "phone"), TS_IP: who(OWNER_LOGIN, "box"), "::1": who(OWNER_LOGIN, "box"),
                TAGGED: who("tagged-devices", "backup", ["tag:server"]), OTHER: who("mallory@example.com", "laptop")},
      "hang": [HANGS], "garbage": [GARBAGE], "cert": "ok", "crt": "a.crt", "key": "a.key"}


def fake(**kw):
    FX.update(kw)
    tmp = os.path.join(TMP, "fake.json.tmp")
    with open(tmp, "w") as f:
        json.dump(FX, f)
    os.replace(tmp, os.path.join(TMP, "fake.json"))


def calls(what=None):
    try:
        with open(os.path.join(TMP, "calls.log")) as f:
            ls = f.read().splitlines()
    except OSError:
        ls = []
    return [l for l in ls if what is None or l.startswith(what)]


fake()
spec = importlib.util.spec_from_file_location("shepr_server", os.path.join(here, "..", "server.py"))
srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)
ok = True


def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(extra)[:300]) if (extra and not cond) else "")); ok = ok and bool(cond)


def raises(fn, exc):
    try:
        fn()
    except exc as e:
        return e
    return None


def gen_cert(name, days=2, cn=DNS):
    crt, key = os.path.join(TMP, name + ".crt"), os.path.join(TMP, name + ".key")
    r = subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", key,
                        "-out", crt, "-days", str(days), "-subj", "/CN=" + cn, "-addext", "subjectAltName=DNS:" + cn],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("openssl: " + r.stderr[-300:])
    return crt, key


# ============================================================================================
# the small pieces
# ============================================================================================
check("norm_ip: an IPv4 address stays, an IPv6 one is written the short way, a scope is dropped, anything else is empty",
      srv.norm_ip("100.64.0.7") == "100.64.0.7" and srv.norm_ip("FD7A:115C:A1E0:0000:0000:0000:0000:0007") == "fd7a:115c:a1e0::7"
      and srv.norm_ip("fe80::1%eth0") == "fe80::1" and srv.norm_ip("box") == "" and srv.norm_ip("") == "")


def hdrs(text):
    return http.client.parse_headers(io.BytesIO(text.encode() + b"\r\n"))


check("declared_body: no Content-Length means no body", srv.declared_body(hdrs("Host: x\r\n")) == 0)
check("declared_body: one plain number is that number", srv.declared_body(hdrs("Content-Length: 12\r\n")) == 12 and srv.declared_body(hdrs("Content-Length:  5 \r\n")) == 5)
check("declared_body: two lengths, a word, a sign, a float or an absurd length are not a length (the connection will not be reused)",
      all(srv.declared_body(hdrs(h)) is None for h in ("Content-Length: 5\r\nContent-Length: 5\r\n", "Content-Length: abc\r\n", "Content-Length: -1\r\n",
                                                        "Content-Length: +5\r\n", "Content-Length: 1e3\r\n", "Content-Length: 5, 5\r\n", "Content-Length: " + "9" * 13 + "\r\n")))
check("declared_body: Transfer-Encoding is never read here, so it is not a length either",
      srv.declared_body(hdrs("Transfer-Encoding: chunked\r\n")) is None and srv.declared_body(hdrs("Transfer-Encoding: chunked\r\nContent-Length: 3\r\n")) is None)

check("owner_login: the user of a node that has none (\"tagged-devices\") is never an owner", (srv.OWNER.update(login="tagged-devices") or srv.owner_login()) is None)
srv.OWNER.update(login=OWNER_LOGIN)
check("is_owner: the owner's login on a device with no tags, and nothing else",
      srv.is_owner({"login": OWNER_LOGIN, "node": "p", "tagged": False}) and not srv.is_owner({"login": OWNER_LOGIN, "node": "p", "tagged": True})
      and not srv.is_owner({"login": "mallory@example.com", "node": "p", "tagged": False}) and not srv.is_owner(None))

w = srv.ts_whois(PHONE)
check("ts_whois: the owner's phone, login in lower case", w == {"login": OWNER_LOGIN, "node": "phone", "tagged": False}, w)
w = srv.ts_whois(TAGGED)
check("ts_whois: a tagged device is marked as one", w and w["tagged"] and w["node"] == "backup", w)
check("ts_whois: a login of tagged-devices is tagged even with no Tags field", srv.ts_whois(OTHER)["tagged"] is False
      and (fake(whois=dict(FX["whois"], **{"127.0.0.20": who("tagged-devices", "odd")})) or srv.ts_whois("127.0.0.20")["tagged"]))
check("ts_whois: an address Tailscale does not know is None", srv.ts_whois(UNKNOWN) is None)
check("ts_whois: an answer that is not JSON is an error, not a guess", raises(lambda: srv.ts_whois(GARBAGE), ValueError) is not None)
fake(whois=dict(FX["whois"], **{"127.0.0.21": who("fred@example.com", "evil\x1b[31m\nname" + "x" * 200)}))
fake(whois=dict(FX["whois"], **{"127.0.0.22": {"Node": ["not", "a", "dict"], "UserProfile": {"LoginName": OWNER_LOGIN}},
                                "127.0.0.23": {"Node": {"ComputedName": "x"}, "UserProfile": "fred@example.com"}}))
check("ts_whois: an answer whose parts are not what they should be is an error, not a crash and not a guess",
      raises(lambda: srv.ts_whois("127.0.0.22"), ValueError) is not None and raises(lambda: srv.ts_whois("127.0.0.23"), ValueError) is not None)
fake(serve={"TCP": {"8742": {"HTTPS": True}}, "Web": {}})
check("serve_in_front: sees a `tailscale serve` entry for the port, and none for another", srv.serve_in_front(8742) is True and srv.serve_in_front(8743) is False)
fake(serve={})
check("serve_in_front: no serve configuration at all is a plain no", srv.serve_in_front(8742) is False)
fake(serve="garbage")
check("serve_in_front: an answer that cannot be read is 'cannot tell', not a yes or a no", srv.serve_in_front(8742) is None)
fake(serve={})
w = srv.ts_whois("127.0.0.21")
check("ts_whois: a node name is cut to 64 characters and keeps nothing that could forge a journal line (no line break, no escape)",
      len(w["node"]) == 64 and w["node"].startswith("evil31mname") and "\x1b" not in w["node"] and "\n" not in w["node"] and "[" not in w["node"], w)

srv.WHO.clear()
check("admit: the owner on their own device is let in", srv.admit(PHONE) == ({"login": OWNER_LOGIN, "node": "phone", "tagged": False}, None))
r = srv.admit(TAGGED); check("admit: a tagged device is not, and the reason says tagged", r[0] is None and "tagged" in r[1], r)
r = srv.admit(OTHER); check("admit: another tailnet user is not", r[0] is None and "another tailnet user" in r[1], r)
r = srv.admit(UNKNOWN); check("admit: an address Tailscale does not know is not", r[0] is None and "no such peer" in r[1], r)
r = srv.admit(GARBAGE); check("admit: an answer that cannot be read lets nobody in", r[0] is None, r)
srv.OWNER.update(login="tagged-devices"); srv.WHO.clear()
r = srv.admit("127.0.0.20")
check("admit: on a host that is itself a tagged device, other tagged devices are NOT the owner", r[0] is None and "owner" in r[1], r)
srv.OWNER.update(login=None)
check("admit: a server that does not know its owner lets nobody in", srv.admit(PHONE)[0] is None)
srv.OWNER.update(login=OWNER_LOGIN); srv.WHO.clear()

n0 = len(calls("whois"))
for _ in range(5):
    srv.whois_cached(PHONE)
check("whois_cached: five questions about one address ask Tailscale once", len(calls("whois")) - n0 == 1, len(calls("whois")) - n0)
srv.WHO.clear(); n0 = len(calls("whois"))
ts = [threading.Thread(target=srv.whois_cached, args=(PHONE,)) for _ in range(6)]
[t.start() for t in ts]; [t.join() for t in ts]
check("whois_cached: six connections arriving at once ask Tailscale once", len(calls("whois")) - n0 == 1, len(calls("whois")) - n0)
srv.WHO.clear(); srv.WHO_OK_S, srv.WHO_NO_S, srv.WHO_ERR_S = 30.0, 0.6, 0.2
srv.whois_cached(OTHER); srv.whois_cached(UNKNOWN); srv.whois_cached(PHONE); n0 = len(calls("whois")); time.sleep(0.3)
srv.whois_cached(OTHER); srv.whois_cached(UNKNOWN); srv.whois_cached(PHONE)
check("whois_cached: no answer is believed for the shortest time (asked again), a refusal longer, the owner longest", len(calls("whois")) - n0 == 1, calls("whois")[n0:])
time.sleep(0.4); n0 = len(calls("whois")); srv.whois_cached(OTHER); srv.whois_cached(PHONE)
check("whois_cached: a refusal is asked about again once its time is up, the owner still is not", len(calls("whois")) - n0 == 1, calls("whois")[n0:])
srv.WHO_OK_S, srv.WHO_NO_S, srv.WHO_ERR_S = 120.0, 30.0, 3.0; srv.WHO.clear()

cap = io.StringIO(); sys.stderr = cap
srv.note_refused("127.0.0.99", "why"); srv.note_refused("127.0.0.99", "why"); srv.note_refused("127.0.0.98", "why")
sys.stderr = REAL_STDERR
check("note_refused: one journal line per address, however often it knocks", cap.getvalue().count("127.0.0.99") == 1 and cap.getvalue().count("127.0.0.98") == 1, cap.getvalue())

if sys.platform.startswith("linux"):
    ia = srv.iface_addrs()
    check("iface_addrs: the kernel says 127.0.0.1 is on lo", ia.get("127.0.0.1") == "lo", ia)
else:
    print("SKIP iface_addrs (Linux only)")

import errno
check("bind_words: a taken port names `tailscale serve` as the likely holder, and how to take it down",
      "tailscale serve --https=8742 off" in srv.bind_words("100.64.0.1", 8742, "tailscale0", OSError(errno.EADDRINUSE, "Address already in use")))
check("bind_words: a refusal to tie the socket to the interface says so", "tailscale0" in srv.bind_words("100.64.0.1", 8742, "tailscale0", OSError(errno.EPERM, "Operation not permitted")))

# ---- boot_safe: a unit that serves the tailnet itself comes back after a reboot with no help
UD = tempfile.mkdtemp(); srv._unit_dir = lambda: UD


def unit(name, text, dropins=()):
    with open(os.path.join(UD, name + ".service"), "w") as f:
        f.write(text)
    if dropins:
        os.makedirs(os.path.join(UD, name + ".service.d"), exist_ok=True)
    for i, d in enumerate(dropins):
        with open(os.path.join(UD, name + ".service.d", f"{i:02d}.conf"), "w") as f:
            f.write(d)
    return srv.boot_safe(name)


BASE = "[Service]\nExecStart=/usr/bin/python3 /x/server.py 8742\n"
check("boot_safe: a plain unit behind `tailscale serve` with no wait is not", not unit("a", BASE + "ExecStartPost=/usr/bin/tailscale serve --bg --https=8742 http://127.0.0.1:8742\n"))
check("boot_safe: the same with the fix-boot drop-in is", unit("b", BASE + "ExecStartPost=/usr/bin/tailscale serve --bg 8742\n", [srv.BOOT_CONF]))
check("boot_safe: a unit started with --tailnet is, with nothing else", unit("c", "[Service]\nExecStart=/usr/bin/python3 /x/server.py 8742 --tailnet\n"))
check("boot_safe: --tailnet that still runs `tailscale serve` afterwards is not (that command can fail the unit)",
      not unit("d", "[Service]\nExecStart=/usr/bin/python3 /x/server.py 8742 --tailnet\nExecStartPost=/usr/bin/tailscale serve --bg 8742\n"))
check("boot_safe: a drop-in that empties ExecStart and ExecStartPost and starts it with --tailnet makes an old unit safe",
      unit("e", BASE + "ExecStartPost=/usr/bin/sudo -n /usr/bin/tailscale serve --bg --https=8742 http://127.0.0.1:8742\n",
           ["[Service]\nExecStart=\nExecStart=/usr/bin/python3 /x/server.py 8742 --tailnet\nExecStartPost=\nExecStopPost=\n"]))
check("boot_safe: SHEPR_TAILNET=1 in the unit's environment counts as --tailnet", unit("f", BASE + "Environment=SHEPR_TAILNET=1\n") and unit("f2", BASE + 'Environment="SHEPR_AUTOADD=1" "SHEPR_TAILNET=1"\n'))
check("boot_safe: a word that merely starts with --tailnet, or SHEPR_TAILNET=10, does not", not unit("g", "[Service]\nExecStart=/usr/bin/python3 /x/server.py 8742 --tailnetx\n")
      and not unit("g2", BASE + "Environment=SHEPR_TAILNET=10\n"))
check("boot_safe: a later drop-in that takes --tailnet away again is seen", not unit("h", "[Service]\nExecStart=/x/server.py 8742 --tailnet\n", ["[Service]\nExecStart=\nExecStart=/x/server.py 8742\n"]))

# ============================================================================================
# certificates
# ============================================================================================
HAVE_SSL = bool(shutil.which("openssl"))
if not HAVE_SSL:
    print("SKIP every TLS check: the openssl command is not installed, so no test certificate can be made")
    print("OK" if ok else "FAILED"); sys.exit(0 if ok else 1)
CRT_A, KEY_A = gen_cert("a"); CRT_B, KEY_B = gen_cert("b", days=5)
want = ssl.cert_time_to_seconds(subprocess.run(["openssl", "x509", "-enddate", "-noout", "-in", CRT_A], capture_output=True, text=True).stdout.strip().split("=", 1)[1])
got = srv.cert_not_after(open(CRT_A).read())
check("cert_not_after: reads the same last day off the certificate as openssl does", got == want, (got, want))
check("cert_not_after: text that is no certificate has no date", srv.cert_not_after("hello") is None and srv.cert_not_after("") is None
      and srv.cert_not_after("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n") is None)

d, crt, key = srv.tls_files(DNS)
ctx, until = srv.fetch_cert(DNS)
check("fetch_cert: returns a TLS context and the certificate's last day", isinstance(ctx, ssl.SSLContext) and until == want)
check("fetch_cert: the key is kept owner-only (0600) in an owner-only directory (0700), whatever mode Tailscale wrote it with",
      stat.S_IMODE(os.stat(key).st_mode) == 0o600 and stat.S_IMODE(os.stat(d).st_mode) == 0o700 and os.path.dirname(key) == d)
check("fetch_cert: nothing is left behind beside the pair", sorted(os.listdir(d)) == [DNS + ".crt", DNS + ".key"], os.listdir(d))
check("fetch_cert: TLS 1.2 is the oldest version it will speak", ctx.minimum_version == ssl.TLSVersion.TLSv1_2)
fake(cert="denied")
e = raises(lambda: srv.fetch_cert(DNS), srv.TlsError)
check("fetch_cert: a refusal says what Tailscale said and how to fix it", e is not None and "denied" in str(e) and "tailscale set --operator" in str(e), e)
fake(cert="nohttps")
e = raises(lambda: srv.fetch_cert(DNS), srv.TlsError)
check("fetch_cert: a tailnet without HTTPS certificates says where to turn them on", e is not None and "admin console" in str(e), e)
before = open(crt).read()
fake(cert="ok", crt="a.crt", key="b.key")
e = raises(lambda: srv.fetch_cert(DNS), srv.TlsError)
check("fetch_cert: a key that does not belong to the certificate is refused, and the pair in use is left as it was",
      e is not None and "could not be loaded" in str(e) and open(crt).read() == before, e)
fake(crt="a.crt", key="a.key")
check("saved_cert: a pair kept from an earlier run is usable at once", (lambda s: s is not None and s[1] == want)(srv.saved_cert(DNS)))
check("saved_cert: none kept, none returned", srv.saved_cert("nobody.tailnet.ts.net") is None)
real_time = time.time; time.time = lambda: real_time() + 3 * 86400   # three days on: the two-day test certificate has run out
gone = srv.saved_cert(DNS)
time.time = real_time
check("saved_cert: one that has run out is not used", gone is None)
PATH0 = os.environ["PATH"]; os.environ["PATH"] = "/nonexistent"
e = raises(lambda: srv.fetch_cert(DNS), srv.TlsError)
os.environ["PATH"] = PATH0
check("fetch_cert: no tailscale command at all is said plainly", e is not None and "not installed" in str(e), e)
for p in (crt, key):
    os.remove(p)

# ============================================================================================
# the front, on real sockets: 127.0.0.2 on lo plays the tailnet address
# ============================================================================================
def free_port():
    s = socket.socket(); s.bind((TS_IP, 0)); p = s.getsockname()[1]; s.close(); return p


HAVE_V6 = False
try:
    _s = socket.socket(socket.AF_INET6); _s.bind(("::1", 0)); _s.close(); HAVE_V6 = True
except OSError:
    print("SKIP the IPv6 listener: this machine has no ::1")
IPS = [TS_IP] + (["::1"] if HAVE_V6 else [])
FX["status"]["Self"]["TailscaleIPs"] = IPS; fake()
ADDRS = {ip: "lo" for ip in IPS}; ADDRS["127.0.0.1"] = "lo"
srv.iface_addrs = lambda: dict(ADDRS)
srv.OWNER.update(login=None, dns=None, ips=[]); srv.OWNER["names"] = set(srv.LOOPBACK_NAMES)
srv.TAILNET["on"] = True
PORT = free_port()
handler = functools.partial(srv.Handler, directory=srv.HERE)
cap = io.StringIO(); sys.stderr = cap   # the server's journal, read by the checks below
plain = srv.CappedServer(("127.0.0.1", PORT), handler)
threading.Thread(target=plain.serve_forever, daemon=True).start()
front = srv.Front(PORT, handler)
front.tick()
check("front: one pass learns who this node is from Tailscale", srv.OWNER["dns"] == DNS and srv.OWNER["login"] == OWNER_LOGIN and srv.OWNER["ips"] == IPS, srv.OWNER)
check("front: and listens on every tailnet address, tied to the interface that holds it", sorted(front.srv) == sorted(IPS) and all(k[0] == "lo" for _, k in front.srv.values()), front.srv)
sj = json.load(open(srv.state_path("server.json")))
check("front: server.json says how the tailnet is served (what `shepr status` and `shepr pair` read)",
      sj.get("front", {}).get("mode") == "tailnet" and sj["front"]["listening"] == sorted(IPS) and sj["front"]["dev"] == "lo" and sj["front"]["error"] is None
      and sj["front"]["tls_until"] == want and sj["url"] == f"https://{DNS}:{PORT}", sj)
check("front: the journal says so once, with the address, the interface and the certificate's last day, and no key",
      cap.getvalue().count("serving the tailnet itself") == 1 and TS_IP in cap.getvalue() and " on lo" in cap.getvalue() and "PRIVATE" not in cap.getvalue(), cap.getvalue())
n0 = len(calls()); front.tick(); front.tick()
check("front: a pass that finds nothing changed starts no process and says nothing", len(calls()) == n0 and cap.getvalue().count("serving the tailnet itself") == 1, calls()[n0:])
import struct
check("front: the listening socket really is tied to the interface (SO_BINDTODEVICE)",
      front.srv[TS_IP][0].socket.getsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, 16).rstrip(b"\0") == b"lo")


class C:
    """One connection to the tailnet listener from a chosen loopback address: which device it is."""
    def __init__(self, src=PHONE, dst=TS_IP, port=None, tls=True, name=DNS, ca=CRT_A, timeout=8):
        self.raw = socket.create_connection((dst, port or PORT), timeout=timeout, source_address=(src, 0) if ":" not in dst else None)
        self.s = self.raw
        if tls:
            ctx = ssl.create_default_context(cafile=ca)
            ctx.set_alpn_protocols(["h2", "http/1.1"])
            self.s = ctx.wrap_socket(self.raw, server_hostname=name)
        self.f = self.s.makefile("rb")

    def send(self, data):
        self.s.sendall(data if isinstance(data, bytes) else data.encode())

    def get(self, path, extra="", host=None, version="1.1"):
        self.send(f"GET {path} HTTP/{version}\r\nHost: {host or DNS + ':' + str(PORT)}\r\n{extra}\r\n")
        return self.resp()

    def resp(self):
        """(status, headers, body, version) of the next answer, or None when the server has closed instead."""
        try:
            line = self.f.readline()
        except OSError:
            return None
        if not line:
            return None
        ver, code = line.split()[0].decode(), int(line.split()[1])
        h = {}
        while True:
            l = self.f.readline()
            if l in (b"\r\n", b"\n", b""):
                break
            k, _, v = l.decode().partition(":")
            h[k.strip().lower()] = v.strip()
        body = self.f.read(int(h.get("content-length", "0")))
        return code, h, body, ver

    def gone(self, wait=3.0):
        """True when the server closes this connection within `wait` seconds without sending anything more. It reads from
        the socket itself: a file object over a socket refuses every read after one that timed out, and that refusal
        would be taken for "closed" (it once made two deadline checks here unable to fail)."""
        self.s.settimeout(wait)
        try:
            return self.s.recv(1) == b""
        except socket.timeout:
            return False
        except OSError:
            return True

    def close(self):
        for x in (self.f, self.s):   # the file object holds the socket open: both, or the server never sees the phone leave
            with contextlib.suppress(OSError):
                x.close()


def knock(src, wait=4.0, dst=TS_IP):
    """Connect without TLS and wait: what does the server send, and does it close? -> (bytes it sent, closed)"""
    c = socket.create_connection((dst, PORT), timeout=wait, source_address=(src, 0))
    c.settimeout(wait); out = b""
    try:
        while True:
            d = c.recv(4096)
            if not d:
                return out, True
            out += d
    except socket.timeout:
        return out, False
    except OSError:
        return out, True
    finally:
        c.close()


def tls_refused(src):
    try:
        c = C(src=src, timeout=6); c.close(); return False
    except OSError:
        return True


# ---- who gets a handshake
c = C()
check("owner: the phone gets a TLS handshake, and the certificate is good for the node's name (the client checked both)", c.s.version() in ("TLSv1.2", "TLSv1.3"), c.s.version())
check("owner: the server picks http/1.1 when the phone offers h2 as well", c.s.selected_alpn_protocol() == "http/1.1", c.s.selected_alpn_protocol())
r = c.get("/ping")
check("owner: /ping is answered over it", r and r[0] == 200 and r[2] == b"ok\n", r)
check("owner: the answer is HTTP/1.1 and says the connection is kept, and for how long", r[3] == "HTTP/1.1" and r[1].get("connection") == "keep-alive" and r[1].get("keep-alive") == f"timeout={srv.KEEP_HINT_S}", r[1])
r2 = c.get("/api/hello", extra="X-Shepr-Build: v-test\r\n")
check("keep-alive: a second request on the same connection is answered", r2 and r2[0] == 200 and json.loads(r2[2])["app"] == "shepr", r2)
r3 = c.get("/index.html")
check("keep-alive: a static file comes over it too, whole", r3 and r3[0] == 200 and len(r3[2]) == os.path.getsize(os.path.join(srv.HERE, "index.html")) and r3[1].get("connection") == "keep-alive")
r4 = c.get("/index.html", extra="If-Modified-Since: Fri, 01 Jan 2100 00:00:00 GMT\r\n")
r5 = c.get("/ping")
check("keep-alive: a 304 has no body and the next request still lines up", r4 and r4[0] == 304 and r4[2] == b"" and r5 and r5[0] == 200, (r4, r5))
c.close()
check("owner: the journal names the login and the device Tailscale gave, not an address", f"hello from {OWNER_LOGIN} on phone build=v-test" in cap.getvalue(), cap.getvalue()[-400:])
check("owner: a wrong name on the certificate would have been caught (a client asking for another name is refused by its own check)",
      raises(lambda: C(name="other.tailnet.ts.net"), ssl.SSLCertVerificationError) is not None)

for src, label in ((TAGGED, "a tagged device"), (OTHER, "another tailnet user"), (UNKNOWN, "an address Tailscale does not know"), (GARBAGE, "an address whois gives no readable answer for")):
    sent, closed = knock(src)
    check(f"refused: {label} is closed without a byte sent to it (no certificate, no HTTP)", closed and sent == b"", (sent[:60], closed))
    check(f"refused: {label} gets no TLS handshake", tls_refused(src))
n0 = len(calls("whois")); a1 = knock("127.0.0.22"); a2 = knock("127.0.0.22")
check("refused: an address whose whois answer has the wrong shape is closed without a byte, and Tailscale is not asked again at once",
      a1 == (b"", True) and a2 == (b"", True) and len(calls("whois")) - n0 == 1, (a1, a2, calls("whois")[n0:]))
srv.WHOIS_SECS = 1
t0 = time.time(); sent, closed = knock(HANGS, wait=6)
check("refused: when `tailscale whois` does not answer in time the connection is closed, not let in", closed and sent == b"" and time.time() - t0 < 4, (sent, closed, time.time() - t0))
srv.WHOIS_SECS = 4
j = cap.getvalue()
check("refused: the journal has one line per refused address, with the reason and no login of the other person",
      j.count(f"refused a tailnet connection from {TAGGED}") == 1 and "tagged device" in j and j.count(f"refused a tailnet connection from {OTHER}") == 1 and "mallory" not in j, j[-600:])

# ---- headers that name a user are not read on a direct connection; a proxy's headers are refused
c = C()
r = c.get("/api/hello", extra="Tailscale-User-Login: mallory@example.com\r\n")
check("gate: Tailscale-User-Login typed by the caller is ignored on a direct connection (the owner stays the owner)", r and r[0] == 200, r)
r = c.get("/api/hello", extra="X-Forwarded-For: 203.0.113.9\r\n")
check("gate: a request that came through a proxy on one of the owner's devices is refused 403", r and r[0] == 403 and json.loads(r[2])["error"] == "not_owner", r)
r = c.get("/api/hello", extra="Tailscale-Funnel-Request: ?1\r\n")
check("gate: Funnel traffic is refused 403", r and r[0] == 403 and json.loads(r[2])["error"] == "funnel", r)
for h in ("localhost", "127.0.0.1", TS_IP, "evil.example.com"):
    r = c.get("/api/hello", host=f"{h}:{PORT}")
    check(f"gate: on the tailnet listener the Host must be the node's name ({h} is refused 421)", r and r[0] == 421, r)
r = c.get("/api/hello", host=DNS.upper())
check("gate: the node's name passes in any case, with or without a port", r and r[0] == 200, r)
c.close()

# ---- a connection that is kept is asked about again: the owner's device today may be somebody else's tomorrow
c = C(); r = c.get("/ping")
fake(whois=dict(FX["whois"], **{PHONE: who("tagged-devices", "phone", ["tag:server"])})); srv.WHO.clear()
r2 = c.get("/ping")
check("kept connection: once Tailscale no longer says it is the owner's device (here: the phone was tagged), it gets no further answer",
      r and r[0] == 200 and r2 is None and c.gone(), (r and r[0], r2))
c.close()
check("kept connection: and a new connection from it is refused like any tagged device", knock(PHONE) == (b"", True))
fake(whois=dict(FX["whois"], **{PHONE: who("Fred@Example.com", "phone")})); srv.WHO.clear()
c = C(); r = c.get("/ping"); r2 = c.get("/ping")
check("kept connection: the owner's device is let in again as soon as Tailscale says so, and its connection is kept", r and r[0] == 200 and r2 and r2[0] == 200, (r, r2))
c.close()
n0 = len(calls("whois")); c = C()
for _ in range(20):
    c.get("/ping")
c.close()
check("kept connection: twenty requests on one connection do not ask Tailscale twenty times (the answer is remembered)", len(calls("whois")) - n0 <= 1, len(calls("whois")) - n0)

# ---- the loopback listener is what it always was
p = http.client.HTTPConnection("127.0.0.1", PORT, timeout=8)
p.request("GET", "/ping"); pr = p.getresponse(); pbody = pr.read()
check("loopback: plain HTTP still answers on 127.0.0.1", pr.status == 200 and pbody == b"ok\n")
check("loopback: one request per connection, as before (HTTP/1.0, nothing about keeping it)", pr.version == 10 and pr.getheader("Keep-Alive") is None and pr.getheader("Connection") is None, (pr.version, pr.getheaders()))
p.close()
p = http.client.HTTPConnection("127.0.0.1", PORT, timeout=8)
p.request("GET", "/api/hello", headers={"Tailscale-User-Login": "mallory@example.com"}); pr = p.getresponse(); pr.read(); p.close()
check("loopback: there the header `tailscale serve` sets is still what counts (another user is refused 403)", pr.status == 403)

# ---- a signed request, and a POST, over the kept connection
CD = 0x0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF % srv._N
cx, cy = srv._mul(CD, srv._G)
DID = hashlib.sha256(b"\x04" + cx.to_bytes(32, "big") + cy.to_bytes(32, "big")).hexdigest()[:16]
srv.mutate_devices(lambda ds: ds.__setitem__(DID, {"name": "Test phone", "x": srv.b64u(cx.to_bytes(32, "big")), "y": srv.b64u(cy.to_bytes(32, "big")),
                                                   "added": int(time.time()), "last_seen": int(time.time()), "ua": ""}))


def signed(method, path, raw=b""):
    ts, nonce = str(int(time.time() * 1000)), srv.b64u(os.urandom(12))
    msg = "\n".join([ts, nonce, method, path, hashlib.sha256(raw).hexdigest()]).encode()
    return f"X-Shepr-Device: {DID}\r\nX-Shepr-Ts: {ts}\r\nX-Shepr-Nonce: {nonce}\r\nX-Shepr-Sig: {srv.b64u(srv.ecdsa_sign(CD, msg))}\r\n"


def post(c, path, raw, extra="", sign=True, shepr=True):
    c.send((f"POST {path} HTTP/1.1\r\nHost: {DNS}:{PORT}\r\n" + ("X-Shepr: 1\r\n" if shepr else "") + "Content-Type: application/json\r\n"
            f"Content-Length: {len(raw)}\r\n" + (signed("POST", path, raw) if sign else "") + extra + "\r\n").encode() + raw)
    return c.resp()


c = C()
r = c.get("/api/live/status")
check("signed: without a signature the API still says no (401), and the connection is kept", r and r[0] in (401, 403) and r[1].get("connection") == "keep-alive", r)
r = c.get("/api/live/status", extra=signed("GET", "/api/live/status"))
check("signed: a paired phone's signed read is answered over the tailnet listener", r and r[0] == 200 and "status" in json.loads(r[2]), r)
body = json.dumps({"host": "nope", "pane": "w1:p1", "kind": "text", "text": "hi"}).encode()
r = post(c, "/api/send/prepare", body)
check("signed: a POST whose body was read is answered (here: no such host) and the connection is kept", r and r[0] >= 400 and r[1].get("connection") == "keep-alive" and b"error" in r[2], r)
r = c.get("/ping")
check("signed: and the next request on it lines up", r and r[0] == 200, r)
c.close()

# ---- a request that was not read to its end closes the connection: what is left of it is never taken for a request
SMUGGLED = f"GET /api/hello HTTP/1.1\r\nHost: {DNS}:{PORT}\r\nX-Shepr-Build: SMUGGLED\r\n\r\n".encode()


def one_answer(c, first):
    """After `first`, does the server close without answering anything else? (The smuggled request must never run.)"""
    rest = b""
    c.s.settimeout(4)
    try:
        while True:
            d = c.f.read(4096)
            if not d:
                break
            rest += d
    except OSError:
        pass
    return first is not None and first[1].get("connection") == "close" and rest == b"" and "SMUGGLED" not in cap.getvalue()


c = C(); r = post(c, "/api/hello", SMUGGLED, sign=False, shepr=False)
check("desync: a POST refused at the gate (what any web page can send) closes the connection; the request hidden in its body is not run", r and r[0] == 400 and one_answer(c, r), r)
c.close()
c = C(); r = post(c, "/api/send/prepare", SMUGGLED, sign=False)
check("desync: a POST from a phone that is not paired, refused before its body is read, likewise", r and r[0] in (401, 403) and one_answer(c, r), r)
c.close()
c = C(); c.send(f"GET /ping HTTP/1.1\r\nHost: {DNS}\r\nContent-Length: {len(SMUGGLED)}\r\n\r\n".encode() + SMUGGLED); r = c.resp()
check("desync: a GET that carries a body is answered and then closed", r and r[0] == 200 and one_answer(c, r), r)
c.close()
c = C(); c.send(f"POST /api/hello HTTP/1.1\r\nHost: {DNS}\r\nX-Shepr: 1\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n".encode()
                + hex(len(SMUGGLED))[2:].encode() + b"\r\n" + SMUGGLED + b"\r\n0\r\n\r\n"); r = c.resp()
check("desync: a chunked body is never read, so that connection is closed", r and one_answer(c, r), r)
c.close()
c = C(); c.send(f"POST /api/hello HTTP/1.1\r\nHost: {DNS}\r\nX-Shepr: 1\r\nContent-Type: application/json\r\nContent-Length: 0\r\nContent-Length: {len(SMUGGLED)}\r\n\r\n".encode() + SMUGGLED); r = c.resp()
check("desync: two Content-Length headers close it", r and one_answer(c, r), r)
c.close()
c = C(); c.send(f"POST /api/hello HTTP/1.1\r\nHost: {DNS}\r\nX-Shepr: 1\r\nContent-Type: application/json\r\nContent-Length: abc\r\n\r\n".encode() + SMUGGLED); r = c.resp()
check("desync: a Content-Length that is not a number closes it", r and one_answer(c, r), r)
c.close()
c = C(); c.send(f"POST /index.html HTTP/1.1\r\nHost: {DNS}\r\nContent-Length: {len(SMUGGLED)}\r\n\r\n".encode() + SMUGGLED); r = c.resp()
check("desync: a POST to something that is not the API (405) closes it", r and r[0] == 405 and one_answer(c, r), r)
c.close()
c = C(); c.send(f"OPTIONS /api/hello HTTP/1.1\r\nHost: {DNS}\r\nContent-Length: {len(SMUGGLED)}\r\n\r\n".encode() + SMUGGLED); r = c.resp()
check("desync: a preflight (never answered) closes it", r and r[0] == 405 and one_answer(c, r), r)
c.close()
c = C(); c.send(b"GARBAGE\r\n\r\n" + SMUGGLED); r = c.resp()
check("desync: a line that is no request closes it", r and r[0] == 400 and one_answer(c, r), r)
c.close()
c = C(); r = c.get("/nope.txt"); check("a file that is not on the list is 404 and the connection is closed", r and r[0] == 404 and r[1].get("connection") == "close" and c.gone(), r); c.close()
c = C(); r = c.get("/ping", extra="Connection: close\r\n")
check("a phone that asks to close gets Connection: close, and it is closed", r and r[0] == 200 and r[1].get("connection") == "close" and c.gone(), r); c.close()
c = C(); r = c.get("/ping", version="1.0")
check("an HTTP/1.0 request is answered and closed", r and r[0] == 200 and r[1].get("connection") == "close" and c.gone(), r); c.close()
c = C(); c.send(f"GET /ping HTTP/1.1\r\nHost: {DNS}\r\n\r\nGET /api/hello HTTP/1.1\r\nHost: {DNS}\r\n\r\n"); ra, rb = c.resp(), c.resp()
check("two honest requests sent at once are answered in order", ra and rb and ra[0] == 200 and ra[2] == b"ok\n" and rb[0] == 200 and b"shepr" in rb[2], (ra, rb)); c.close()

# ---- an answer that goes out before the body was read: the rest of the body is taken in, so the phone can read the answer
BIG = 12 * 1024 * 1024
UP = f"POST /api/upload?host=box&ext=png HTTP/1.1\r\nHost: {DNS}:{PORT}\r\nX-Shepr: 1\r\nContent-Type: application/octet-stream\r\nContent-Length: {BIG}\r\n\r\n"


def pour(c, total, piece=65536, pause=0.0):
    """Send `total` bytes of body. False as soon as the connection breaks under it."""
    try:
        for _ in range(total // piece):
            c.send(b"\0" * piece)
            if pause:
                time.sleep(pause)
    except OSError:
        return False
    return True


c = C(); c.send(UP); poured = pour(c, BIG); r = c.resp() if poured else None
check("linger: a refused 12 MB upload (this phone is not paired) is taken in to its end, so the phone reads the refusal and not a reset",
      poured and r and r[0] in (401, 403) and r[1].get("connection") == "close" and c.gone(), (poured, r))
c.close()
def still_open(c):
    """Can more of the body still be sent? False once the server has closed the connection for good (it stops sending at
    once, so only a send that fails shows that it has also stopped listening)."""
    try:
        c.send(b"\0"); time.sleep(0.15); c.send(b"\0")
        return True
    except OSError:
        return False


srv.LINGER_IDLE = 1
c = C(); c.send(UP); pour(c, 65536); r = c.resp(); time.sleep(0.3); early = still_open(c); time.sleep(1.8); late = still_open(c)
check("linger: an upload that stops coming is waited for (still taken in after 0.3 s) but only for LINGER_IDLE (closed after 1.8 s of nothing)",
      r and r[0] in (401, 403) and early and not late, (r and r[0], early, late))
c.close()
srv.LINGER_IDLE = 5; srv.LINGER_SECS = 2
c = C(); c.send(UP); t0 = time.time(); cut = not pour(c, 60 * 1024, piece=1024, pause=0.1)
check("linger: an upload trickled a little at a time is cut at LINGER_SECS", cut and 1.5 < time.time() - t0 < 5, time.time() - t0)
c.close()
srv.LINGER_SECS = 30; srv.LINGER_MAX = 256 * 1024
c = C(); c.send(UP); poured = pour(c, 2 * 1024 * 1024); time.sleep(0.4)
check("linger: no more than LINGER_MAX is taken in: past it the connection is closed at once, not after the idle wait", not poured or not still_open(c), poured)
c.close()
srv.LINGER_MAX = 2 * srv.UPLOAD_MAX

# ---- the corners of HTTP/1.1 on a kept connection
def free(sem, cap):
    """How many places a semaphore has free right now (taken and given straight back)."""
    got = 0
    while got <= cap and sem.acquire(blocking=False):
        got += 1
    for _ in range(got):
        sem.release()
    return got


def settled(want_conn=48, want_idle=None, wait=6.0):
    """True once every connection slot and every idle place has been given back."""
    want_idle = free(srv.IDLE_SLOTS, 64) if want_idle is None else want_idle
    end = time.time() + wait
    while time.time() < end:
        if free(srv.CappedServer._slots, 64) == want_conn and free(srv.IDLE_SLOTS, 64) == want_idle:
            return True
        time.sleep(0.1)
    return False


IDLE_CAP = 24
check("slots: before the corner cases every connection slot and idle place is free", settled(48, IDLE_CAP), (free(srv.CappedServer._slots, 64), free(srv.IDLE_SLOTS, 64)))
c = C(); c.send(f"HEAD /index.html HTTP/1.1\r\nHost: {DNS}\r\n\r\n")
line = c.f.readline(); hh = {}
while True:
    l = c.f.readline()
    if l in (b"\r\n", b""):
        break
    k, _, v = l.decode().partition(":"); hh[k.strip().lower()] = v.strip()
r = c.get("/ping")
check("HEAD: the answer has the file's length and no body, and the next request on the connection lines up",
      b" 200 " in line and int(hh.get("content-length", 0)) == os.path.getsize(os.path.join(srv.HERE, "index.html")) and hh.get("connection") == "keep-alive" and r and r[0] == 200 and r[2] == b"ok\n", (line, hh, r))
c.close()
body = json.dumps({"code": "AAAA-BBBB-CCCC", "jwk": {}, "name": "x"}).encode()
c = C(); c.send(f"POST /api/pair HTTP/1.1\r\nHost: {DNS}:{PORT}\r\nX-Shepr: 1\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nExpect: 100-continue\r\n\r\n")
first = c.f.readline()
while c.f.readline() not in (b"\r\n", b""):
    pass
c.send(body); r = c.resp(); r2 = c.get("/ping")
check("Expect: 100-continue: the server says go on, reads the body, answers, and the connection is still good for the next request",
      b" 100 " in first and r and r[0] >= 400 and b"error" in r[2] and r[1].get("connection") == "keep-alive" and r2 and r2[0] == 200, (first, r, r2))
c.close()
c = C(); r = post(c, "/api/pair", body, sign=False); r2 = c.get("/ping")
check("pair with a wrong code: refused after its body was read, so the connection is kept", r and r[0] >= 400 and r[1].get("connection") == "keep-alive" and r2 and r2[0] == 200, (r, r2))
c.close()
c = C(); r = c.get("/vendor/jsQR.js"); r2 = c.get("/ping")
check("a large file (the QR reader, 250 KB) comes whole over a kept connection and the next request lines up",
      r and r[0] == 200 and len(r[2]) == os.path.getsize(os.path.join(srv.HERE, "vendor", "jsQR.js")) and hashlib.sha256(r[2]).hexdigest() == hashlib.sha256(open(os.path.join(srv.HERE, "vendor", "jsQR.js"), "rb").read()).hexdigest()
      and r2 and r2[0] == 200, (r and (r[0], len(r[2])), r2))
c.close()
c = C(); c.send(f"GET /ping HTTP/1.1\r\nHost: {DNS}\r\n\r\n"); socket.socket.shutdown(c.s, socket.SHUT_WR); r = c.resp()
check("a phone that stops sending right after its request (half-close) still gets its answer, and then the connection ends", r and r[0] == 200 and r[2] == b"ok\n" and c.gone(), r)
c.close()
# abrupt ends: the phone vanishes mid-request, mid-answer, mid-handshake. Nothing may leak, nothing may be logged as a fault.
j0 = len(cap.getvalue())
for _ in range(6):
    c = C(); c.send(f"GET /vendor/jsQR.js HTTP/1.1\r\nHost: {DNS}\r\n\r\n"); c.f.read(100)
    c.s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)); c.close()        # a reset in the middle of the file
for _ in range(6):
    c = C(); c.send(f"POST /api/pair HTTP/1.1\r\nHost: {DNS}\r\nX-Shepr: 1\r\nContent-Type: application/json\r\nContent-Length: 500\r\n\r\nhalf")
    c.s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)); c.close()        # a reset in the middle of a body
for _ in range(6):
    raw = socket.create_connection((TS_IP, PORT), timeout=5, source_address=(PHONE, 0)); raw.sendall(b"\x16\x03\x01\x02\x00\x01\x00"); raw.close()   # gone in the middle of the handshake
for _ in range(6):
    c = C(); c.send(b"GET /pi"); c.close()                                                                # gone in the middle of the request line
check("abrupt ends: after 24 connections that died in the middle of something, every slot and idle place is back", settled(48, IDLE_CAP, wait=12), (free(srv.CappedServer._slots, 64), free(srv.IDLE_SLOTS, 64)))
jj = cap.getvalue()[j0:]
check("abrupt ends: none of them left a traceback or an 'internal error' in the journal", "Traceback" not in jj and "internal error" not in jj and "Exception" not in jj, jj[-600:])
# the cap: 48 connections at once, the rest are closed at the door, and it all comes back
held, turned = [], 0
for _ in range(60):
    try:
        held.append(C(timeout=4))
    except OSError:
        turned += 1
check("cap: of 60 connections opened at once, 48 are served and the rest are closed at the door", len(held) == 48 and turned == 12, (len(held), turned))
r = held[0].get("/ping")
check("cap: a connection that got in is answered while the server is full", r and r[0] == 200, r)
check("cap: the loopback listener shares the same 48 (a new connection there is turned away too)",
      raises(lambda: (lambda p: (p.request("GET", "/ping"), p.getresponse()))(http.client.HTTPConnection("127.0.0.1", PORT, timeout=3)), (OSError, http.client.HTTPException)) is not None)
for c in held:
    c.close()
check("cap: once they are gone every slot and idle place is free again", settled(48, IDLE_CAP, wait=12), (free(srv.CappedServer._slots, 64), free(srv.IDLE_SLOTS, 64)))
c = C(); r = c.get("/ping"); check("cap: and the server answers as before", r and r[0] == 200 and r[1].get("connection") == "keep-alive", r); c.close()

# ---- eight phones at once, a hundred requests each, of every kind: answers must not cross, slots must not leak
CSS_LEN = os.path.getsize(os.path.join(srv.HERE, "app.css"))
errs = []


def phone(seed):
    import random
    rnd = random.Random(seed)
    c = C()
    try:
        for i in range(100):
            kind = rnd.choice(["ping", "ping", "hello", "static", "static", "signed", "badpost", "smuggle", "close", "notfound"])
            keep = True
            if kind == "ping":
                r = c.get("/ping"); good = r and r[0] == 200 and r[2] == b"ok\n"
            elif kind == "hello":
                r = c.get("/api/hello"); good = r and r[0] == 200 and b'"app": "shepr"' in r[2]
            elif kind == "static":
                r = c.get("/app.css"); good = r and r[0] == 200 and len(r[2]) == CSS_LEN
            elif kind == "signed":
                r = c.get("/api/live/status", extra=signed("GET", "/api/live/status")); good = r and r[0] == 200 and b'"status"' in r[2]
            elif kind == "badpost":
                r = post(c, "/api/send/prepare", b'{"host": "nope", "pane": "w1:p1", "kind": "text", "text": "x"}'); good = r and r[0] == 404 and b"bad_host" in r[2]
            elif kind == "smuggle":
                r = post(c, "/api/hello", SMUGGLED, sign=False, shepr=False); good = r and r[0] == 400; keep = False
            elif kind == "close":
                r = c.get("/ping", extra="Connection: close\r\n"); good = r and r[0] == 200 and r[2] == b"ok\n"; keep = False
            else:
                r = c.get("/nope.txt"); good = r and r[0] == 404; keep = False
            said = r[1].get("connection") if r else None
            if not good or (not keep and said != "close") or said not in ("keep-alive", "close"):
                errs.append((seed, i, kind, r and r[0], said))
            if said != "keep-alive":
                c.close(); c = C()
    except Exception as e:
        errs.append((seed, "died", repr(e)))
    finally:
        c.close()


j0 = len(cap.getvalue())
ts = [threading.Thread(target=phone, args=(k,)) for k in range(8)]
t0 = time.time(); [t.start() for t in ts]; [t.join() for t in ts]
check("stress: 800 requests of every kind from eight phones at once, each one answered with its own answer", not errs, errs[:6])
check("stress: afterwards every connection slot and idle place is free", settled(48, IDLE_CAP, wait=12), (free(srv.CappedServer._slots, 64), free(srv.IDLE_SLOTS, 64)))
jj = cap.getvalue()[j0:]
check("stress: no request hidden in a refused body was ever run, and nothing was logged as a fault", "SMUGGLED" not in cap.getvalue() and "Traceback" not in jj and "internal error" not in jj, jj[-500:])

# ---- deadlines: a kept connection is not kept for ever, and nothing may be trickled
quiet0 = cap.getvalue()
srv.IDLE_SECS = 1
c = C(); c.get("/ping"); t0 = time.time()
check("idle: a kept connection that stays silent is closed after IDLE_SECS", c.gone(4) and 0.7 < time.time() - t0 < 3, time.time() - t0); c.close()
srv.IDLE_SECS = 45; srv.FIRST_SECS = 1
c = C(); t0 = time.time()
check("idle: a new connection that never sends a request is closed after FIRST_SECS", c.gone(4) and 0.7 < time.time() - t0 < 3, time.time() - t0); c.close()
srv.FIRST_SECS = 10; srv.HANDSHAKE_SECS = 1
t0 = time.time(); sent, closed = knock(PHONE, wait=5)
check("handshake: an owner's connection that never starts TLS is closed after HANDSHAKE_SECS, with nothing sent", closed and sent == b"" and 0.7 < time.time() - t0 < 3.5, (sent, closed, time.time() - t0))
raw = socket.create_connection((TS_IP, PORT), timeout=6, source_address=(PHONE, 0)); raw.settimeout(6); t0 = time.time(); cut = False
try:
    for b in b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03" + b"\x00" * 40:   # the start of a ClientHello, a byte every 0.2 s
        raw.sendall(bytes([b])); time.sleep(0.2)
except OSError:
    cut = True
check("handshake: one trickled byte at a time does not stretch the deadline", cut and time.time() - t0 < 4, time.time() - t0); raw.close()
srv.HANDSHAKE_SECS = 10; srv.HEADER_SECS = 1
c = C(); c.send(f"GET /ping HTTP/1.1\r\nHost: {DNS}\r\n"); t0 = time.time(); cut = False
try:
    for _ in range(30):
        c.send("X-Slow: 1\r\n"); time.sleep(0.2)
        if c.gone(0.01):
            cut = True; break
except OSError:
    cut = True
check("headers: a request trickled a line at a time is cut at HEADER_SECS, on the first request", cut and time.time() - t0 < 4, time.time() - t0); c.close()
c = C(); c.get("/ping"); time.sleep(1.5); r = c.get("/ping")
check("headers: time spent idle between requests is not counted against the next request's headers", r and r[0] == 200, r)
c.send(f"GET /ping HTTP/1.1\r\nHost: {DNS}\r\n"); t0 = time.time(); cut = False
try:
    for _ in range(30):
        c.send("X-Slow: 1\r\n"); time.sleep(0.2)
        if c.gone(0.01):
            cut = True; break
except OSError:
    cut = True
check("headers: and a trickled later request on a kept connection is cut just the same", cut and time.time() - t0 < 4, time.time() - t0); c.close()
srv.HEADER_SECS = 5; srv.BODY_SECS = 1
half = b"x" * 500
c = C(); c.send((f"POST /api/send/prepare HTTP/1.1\r\nHost: {DNS}:{PORT}\r\nX-Shepr: 1\r\nContent-Type: application/json\r\nContent-Length: 500\r\n"
                 + signed("POST", "/api/send/prepare", half) + "\r\n").encode() + half[:10]); t0 = time.time(); cut = False
try:
    for _ in range(40):
        c.send(b"x"); time.sleep(0.2)
        if c.gone(0.01):
            cut = True; break
except OSError:
    cut = True
check("body: a paired phone's JSON body trickled a byte at a time is cut at BODY_SECS, over TLS too", cut and 0.7 < time.time() - t0 < 4, time.time() - t0); c.close()
srv.BODY_SECS = 10
c = C(); c.send(f"GET /ping HTTP/1.1\r\nHost: {DNS}\r\n"); time.sleep(2.2); c.send("\r\n"); r = c.resp()
check("headers: with the deadline back at 5 s, a request that takes 2 s to finish its headers is answered (the trickle checks above can fail)", r and r[0] == 200, r); c.close()
c = C(); c.get("/ping"); c.send(b"GET /ping\r\n\r\n"); c.s.settimeout(4); bare = b""
try:
    while True:
        d = c.f.read(64)
        if not d:
            break
        bare += d
except OSError:
    pass
check("HTTP/0.9 on a kept connection: the bare answer, then the end (nothing is kept after a request with no version)", bare == b"ok\n", bare); c.close()
time.sleep(0.3)
quiet = cap.getvalue()[len(quiet0):]
check("deadlines: connections that end in these ordinary ways leave no line and no traceback in the journal", "Traceback" not in quiet and "timed out" not in quiet and "Exception" not in quiet, quiet[-500:])

# ---- only so many connections may sit idle; the answer says when one will not be kept
real_slots = srv.IDLE_SLOTS; srv.IDLE_SLOTS = threading.BoundedSemaphore(2)
a, b, d = C(), C(), C()
time.sleep(0.4)   # let the server reach "waiting for a request" on all three: the first two hold the places, the third has none
ra, rb, rd = a.get("/ping"), b.get("/ping"), d.get("/ping")
check("idle slots: with two places, two connections are kept and the third is told Connection: close", ra[1].get("connection") == "keep-alive" and rb[1].get("connection") == "keep-alive"
      and rd[1].get("connection") == "close" and d.gone(), [x[1].get("connection") for x in (ra, rb, rd)])
check("idle slots: a kept connection still works", a.get("/ping")[0] == 200)
a.close(); b.close(); d.close(); time.sleep(0.5)
got = [srv.IDLE_SLOTS.acquire(blocking=False) for _ in range(3)]
check("idle slots: every place is given back when its connection ends (two free, not three)", got == [True, True, False], got)
srv.IDLE_SLOTS = real_slots

# ---- what is not TLS gets nothing
sock = socket.create_connection((TS_IP, PORT), timeout=5, source_address=(PHONE, 0)); sock.settimeout(5)
sock.sendall(f"GET /api/hello HTTP/1.1\r\nHost: {DNS}\r\n\r\n".encode()); out = b""
try:
    while True:
        dd = sock.recv(4096)
        if not dd:
            break
        out += dd
except OSError:
    pass
sock.close()
check("plain HTTP sent to the tailnet port is answered with no page and no data (at most a TLS alert)", b"shepr" not in out and b"HTTP/" not in out and len(out) < 64, out[:80])

# ---- SHEPR_KEEPALIVE=0: one request per connection
srv.KEEPALIVE = False
c = C(); r = c.get("/ping")
check("keep-alive off: the answer is HTTP/1.0 and the connection is closed after it", r and r[0] == 200 and r[3] == "HTTP/1.0" and "keep-alive" not in r[1] and c.gone(), r); c.close()
srv.KEEPALIVE = True

if HAVE_V6:
    c = C(dst="::1"); r = c.get("/ping")
    check("IPv6: the node's other address answers too, with the same checks", r and r[0] == 200 and r[1].get("connection") == "keep-alive", r); c.close()

# ---- the certificate is replaced without anything restarting
c_old = C(); c_old.get("/ping")
fake(crt="b.crt", key="b.key"); front.cert_next = 0.0; front.tick(); front.tick()
c_new = C(ca=CRT_B)
der_b = ssl.PEM_cert_to_DER_cert(open(CRT_B).read())
check("renewal: after Tailscale hands out a new certificate, new connections are served with it", c_new.s.getpeercert(True) == der_b and c_new.get("/ping")[0] == 200)
check("renewal: a connection that was open keeps working", c_old.get("/ping")[0] == 200)
check("renewal: the listener was not rebound for it, and server.json has the new last day", sorted(front.srv) == sorted(IPS)
      and json.load(open(srv.state_path("server.json")))["front"]["tls_until"] == srv.cert_not_after(open(CRT_B).read()))
c_old.close(); c_new.close()
fake(cert="denied"); front.cert_next = 0.0; front.tick(); front.tick()
c = C(ca=CRT_B)
check("renewal: when asking fails, the certificate in hand keeps serving", c.get("/ping")[0] == 200); c.close()
check("renewal: and `shepr status` is told why it was not refreshed", "denied" in (srv.TAILNET["error"] or "") and srv.TAILNET["listening"] == sorted(IPS), srv.TAILNET)
check("renewal: the next try is in a quarter of an hour, not at once", 800 < front.cert_next - time.monotonic() < 1000, front.cert_next - time.monotonic())
fake(cert="ok"); front.cert_next = 0.0; front.tick(); front.tick()
check("renewal: once it works again the warning is gone", srv.TAILNET["error"] is None, srv.TAILNET)

until_real = front.until; front.until = time.time() - 5; front.report(None)
check("renewal: a certificate that has run out while Tailscale gives no new one is said plainly, with the day", "ran out on 20" in (srv.TAILNET["error"] or ""), srv.TAILNET)
check("renewal: and it counts as 'phones cannot reach it' although the listener is up (what `shepr pair` warns on)",
      srv.TAILNET["dead"] is True and srv.TAILNET["listening"] == sorted(IPS) and json.load(open(srv.state_path("server.json")))["front"]["dead"] is True, srv.TAILNET)
out = io.StringIO()
with contextlib.redirect_stdout(out):
    srv.HERDR = "/nonexistent/herdr"; srv.cli_status(argparse.Namespace(json=False))
check("renewal: `shepr status` says phones cannot reach it and why", "phones cannot reach it: the certificate ran out" in out.getvalue(), out.getvalue())
front.until = until_real; front.report(None)
check("renewal: and that warning goes when the certificate is good again", srv.TAILNET["error"] is None, srv.TAILNET)

# ---- a `tailscale serve` entry left behind for the same port: tailscaled would take the phones' traffic before this listener sees it
fake(serve={"TCP": {str(PORT): {"HTTPS": True}}}); front.serve_next = 0.0; front.tick()
check("leftover serve: the server says so, with the command that takes it down, and keeps listening",
      f"tailscale serve --https={PORT} off" in (srv.TAILNET["error"] or "") and srv.TAILNET["listening"] == sorted(IPS) and srv.TAILNET["dead"] is False, srv.TAILNET)
n0 = len(calls("serve")); front.tick(); front.tick()
check("leftover serve: it is looked for at a new listener and four times an hour, not on every pass", len(calls("serve")) == n0)
fake(serve={}); front.serve_next = 0.0; front.tick()
check("leftover serve: once it is gone the warning goes", srv.TAILNET["error"] is None, srv.TAILNET)

# ---- the interface is made anew (tailscaled restarted): a socket tied to the old one would listen for nothing
old_srv = front.srv[TS_IP][0]; said0 = cap.getvalue().count("serving the tailnet itself")
real_idx = socket.if_nametoindex; socket.if_nametoindex = lambda dev: real_idx(dev) + 1000
front.tick()
socket.if_nametoindex = real_idx
check("rebind: when the interface's index changes the listener is closed and bound again", front.srv[TS_IP][0] is not old_srv and front.srv[TS_IP][1][1] == real_idx("lo") + 1000)
check("rebind: and the journal says it is serving again (a listener made anew is news even when nothing else changed)", cap.getvalue().count("serving the tailnet itself") == said0 + 1, cap.getvalue()[-300:])
c = C(ca=CRT_B); check("rebind: and it answers", c.get("/ping")[0] == 200); c.close()
front.tick()
check("rebind: back to the real index, bound once more", front.srv[TS_IP][1][1] == real_idx("lo"))

# ---- the address goes away (tailscale down) and comes back
ADDRS.pop(TS_IP); ADDRS.pop("::1", None); front.learn_next = 0.0; front.tick()
check("address gone: the listeners are closed", front.srv == {} and srv.TAILNET["listening"] == [])
check("address gone: nothing answers on it any more", raises(lambda: C(ca=CRT_B, timeout=2), OSError) is not None)
sj = json.load(open(srv.state_path("server.json")))
check("address gone: server.json says phones cannot reach it, and why", sj["front"]["listening"] == [] and "not on any interface" in (sj["front"]["error"] or ""), sj["front"])
out = io.StringIO()
with contextlib.redirect_stdout(out):
    srv.HERDR = "/nonexistent/herdr"; srv.cli_status(argparse.Namespace(json=False))
check("address gone: `shepr status` warns that phones cannot reach it", "phones cannot reach it" in out.getvalue() and "not on any interface" in out.getvalue(), out.getvalue())
out = io.StringIO()
with contextlib.redirect_stdout(out):
    srv.cli_status(argparse.Namespace(json=True))
check("address gone: `shepr status --json` carries the same under front", json.loads(out.getvalue())["front"]["listening"] == [] and json.loads(out.getvalue())["front"]["error"])
for ip in IPS:
    ADDRS[ip] = "lo"
front.tick()
check("address back: listening again, the warning gone", sorted(front.srv) == sorted(IPS) and srv.TAILNET["error"] is None, srv.TAILNET)
out = io.StringIO()
with contextlib.redirect_stdout(out):
    srv.cli_status(argparse.Namespace(json=False))
check("`shepr status` says it serves the tailnet itself, on what, and until when the certificate is good",
      "serves the tailnet itself on " + TS_IP in out.getvalue() and "(lo)" in out.getvalue() and "certificate good until 20" in out.getvalue() and "warning" not in out.getvalue(), out.getvalue())

# ---- the port is taken on the tailnet address (a `tailscale serve` left behind)
P2 = free_port()
squat = socket.socket(); squat.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"lo\0"); squat.bind((TS_IP, P2)); squat.listen(1)
f2 = srv.Front(P2, handler); f2.told = None
f2.tick()
check("port taken: no listener on that address, and the reason names `tailscale serve` and how to take it down",
      TS_IP not in f2.srv and "already taken" in (srv.TAILNET["error"] or "") and f"tailscale serve --https={P2} off" in srv.TAILNET["error"], srv.TAILNET)
squat.close(); f2.tick()
check("port taken: once it is free the next pass listens", TS_IP in f2.srv and srv.TAILNET["error"] is None, (f2.srv.keys(), srv.TAILNET))
for ip in list(f2.srv):
    f2.close(ip)

# ---- starting before Tailscale answers, and before there is a certificate
for ip in list(front.srv):
    front.close(ip)
srv.OWNER.update(login=None, dns=None, ips=[]); srv.OWNER["names"] = set(srv.LOOPBACK_NAMES); srv.WHO.clear()
os.remove(crt); os.remove(key)
P3 = free_port(); status_ok = FX["status"]
fake(status=None, cert="denied", crt="a.crt", key="a.key")
f3 = srv.Front(P3, handler)
f3.tick()
check("boot: with tailscaled not answering there is no listener, and the reason is said in plain words", f3.srv == {} and "tailscaled" in (srv.TAILNET["error"] or ""), srv.TAILNET)
check("boot: the next question comes in a few seconds, not at once", 1 < f3.learn_next - time.monotonic() <= srv.TAILNET_RETRY_S + 0.1)
fake(status=dict(status_ok, BackendState="Stopped")); ADDRS.pop(TS_IP); ADDRS.pop("::1", None); f3.learn_next = 0.0; f3.tick()
check("boot: Tailscale switched off (its name known, its address not up) says so, with the fix", f3.srv == {} and "turned off" in (srv.TAILNET["error"] or "") and "tailscale up" in srv.TAILNET["error"], srv.TAILNET)
for ip in IPS:
    ADDRS[ip] = "lo"
fake(status=status_ok); f3.learn_next = 0.0; f3.tick()
check("boot: Tailscale up but no certificate to be had: still no listener (never plain HTTP on the tailnet), and the fix is named",
      f3.srv == {} and "tailscale set --operator" in (srv.TAILNET["error"] or ""), srv.TAILNET)
check("boot: nothing is listening on the tailnet address meanwhile", raises(lambda: socket.create_connection((TS_IP, P3), timeout=1, source_address=(PHONE, 0)), OSError) is not None)
fake(cert="ok"); f3.cert_next = 0.0; f3.tick()
check("boot: once the certificate is there the listeners come up in the same pass", sorted(f3.srv) == sorted(IPS) and srv.TAILNET["error"] is None, srv.TAILNET)
for ip in list(f3.srv):
    f3.close(ip)
fake(cert="denied")
f4 = srv.Front(free_port(), handler); f4.tick()
check("boot: a certificate kept from an earlier run opens the door at once, even while Tailscale refuses a fresh one",
      sorted(f4.srv) == sorted(IPS) and f4.ctx is not None, (f4.srv.keys(), srv.TAILNET))
f4.tick()
check("boot: and the refusal is reported as a warning beside a working listener", "denied" in (srv.TAILNET["error"] or "") and srv.TAILNET["listening"] == sorted(IPS), srv.TAILNET)
for ip in list(f4.srv):
    f4.close(ip)
fake(cert="ok")

# ---- a host that is itself a tagged device has no owner to answer to
srv.OWNER.update(login="tagged-devices"); srv.WHO.clear()
f5 = srv.Front(free_port(), handler); f5.tick()
check("tagged host: no listener, and the reason says to set SHEPR_OWNER", f5.srv == {} and "SHEPR_OWNER" in (srv.TAILNET["error"] or ""), srv.TAILNET)
srv.OWNER.update(login=OWNER_LOGIN)

sys.stderr = REAL_STDERR
j = cap.getvalue()
check("journal: through all of the above, not one traceback, and the private key never appears", "Traceback" not in j and "PRIVATE KEY" not in j, j[-800:])
plain.shutdown(); plain.server_close()

# ============================================================================================
# the real thing, started as a program
# ============================================================================================
SD = tempfile.mkdtemp(); HB = os.path.join(TMP, "herdr-stub")
with open(HB, "w") as f:
    f.write("#!/bin/sh\necho '{\"result\":{\"agents\":[],\"workspaces\":[],\"sessions\":[],\"machines\":[]}}'\n")
os.chmod(HB, 0o755)
E2E_IP = "127.0.0.3"
FX["status"]["Self"]["TailscaleIPs"] = [E2E_IP]; fake(whois=dict(FX["whois"], **{E2E_IP: who(OWNER_LOGIN, "box")}))
P9 = free_port()
env = dict(os.environ, SHEPR_STATE=SD, HERDR_BIN=HB, SHEPR_TAILNET_DEV="lo", SHEPR_LOCAL_ONLY="1", SHEPR_WATCH="0", SHEPR_USAGE="0", SHEPR_POLL="60", HOME=TMP)
proc = subprocess.Popen([sys.executable, os.path.join(here, "..", "server.py"), str(P9), "--tailnet"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
lines = []
threading.Thread(target=lambda: [lines.append(l) for l in proc.stderr], daemon=True).start()
end = time.time() + 20
while time.time() < end and not any("serving the tailnet itself" in l for l in lines):
    time.sleep(0.1)
check("program: `server.py PORT --tailnet` comes up and says it serves the tailnet itself", any("serving the tailnet itself" in l and E2E_IP in l for l in lines), lines[-6:])
PORT = P9
try:
    c = C(dst=E2E_IP); r = c.get("/api/hello")
    check("program: the owner's phone is answered over TLS", r and r[0] == 200 and json.loads(r[2])["name"], r); c.close()
except OSError as e:
    check("program: the owner's phone is answered over TLS", False, e)
sent, closed = knock(TAGGED, dst=E2E_IP)
check("program: a tagged device is closed without a byte", closed and sent == b"", (sent, closed))
p = http.client.HTTPConnection("127.0.0.1", P9, timeout=8); p.request("GET", "/ping"); pr = p.getresponse()
check("program: loopback answers plain HTTP beside it", pr.status == 200 and pr.read() == b"ok\n"); p.close()
st = subprocess.run([sys.executable, os.path.join(here, "..", "server.py"), "status"], env=env, capture_output=True, text=True, timeout=60)
check("program: `shepr status` (another process) reads it from server.json", f"running at https://{DNS}:{P9}" in st.stdout and "serves the tailnet itself on " + E2E_IP in st.stdout, st.stdout + st.stderr)
pj = subprocess.run([sys.executable, os.path.join(here, "..", "server.py"), "pair", "--no-wait", "--json"], env=env, capture_output=True, text=True, timeout=60)
check("program: `shepr pair` gives the tailnet URL and no warning while the listener is up",
      pj.returncode == 0 and json.loads(pj.stdout)["url"].startswith(f"https://{DNS}:{P9}/#shepr-pair=") and "warning" not in pj.stderr, pj.stdout + pj.stderr)
proc.terminate()
try:
    proc.wait(timeout=10)
except subprocess.TimeoutExpired:
    proc.kill()
time.sleep(0.3)
allout = "".join(lines)
check("program: its journal has no traceback and no key", "Traceback" not in allout and "PRIVATE KEY" not in allout, allout[-800:])
kmode = stat.S_IMODE(os.stat(os.path.join(SD, "tls", DNS + ".key")).st_mode)
check("program: the key it keeps is 0600 in a 0700 directory", kmode == 0o600 and stat.S_IMODE(os.stat(os.path.join(SD, "tls")).st_mode) == 0o700, oct(kmode))
# a server.json that says the listener is down: `shepr pair` must say so before showing a code that cannot work
with open(os.path.join(SD, "server.json"), "w") as f:
    json.dump({"pid": os.getpid(), "port": P9, "url": f"https://{DNS}:{P9}", "started": 1,
               "front": {"mode": "tailnet", "listening": [], "dev": None, "error": "Tailscale is turned off on this machine. Fix: tailscale up", "tls_until": None}}, f)
pj = subprocess.run([sys.executable, os.path.join(here, "..", "server.py"), "pair", "--no-wait", "--json"], env=env, capture_output=True, text=True, timeout=60)
check("program: `shepr pair` warns (on stderr, the JSON stays clean) when phones cannot reach the server",
      pj.returncode == 0 and "phones cannot reach this server right now" in pj.stderr and "tailscale up" in pj.stderr and json.loads(pj.stdout)["code"], pj.stdout + pj.stderr)
r = subprocess.run([sys.executable, os.path.join(here, "..", "server.py"), str(P9), "--tailnet", "--bind", "0.0.0.0"], env=env, capture_output=True, text=True, timeout=30)
check("program: --tailnet with --bind on every interface is refused (that would be plain HTTP on the tailnet)", r.returncode != 0 and "Leave --bind at 127.0.0.1" in r.stderr, r.stderr[-200:])

print("OK" if ok else "FAILED"); sys.exit(0 if ok else 1)
