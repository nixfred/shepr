"""Regression tests for the bugs fixed in v49 (the bug hunt of 2026-11-15). Each one failed on v48.
herdr, tailscale and systemd are stubbed; a real server is started only on a free loopback port with a temp state folder.
Run: python3 tests/fixes_v49_test.py"""
import functools, http.client, importlib.util, json, os, socket, stat, subprocess, sys, tempfile, threading, time
os.environ["SHEPR_STATE"] = tempfile.mkdtemp()
for _k in ("SHEPR_NEW_CMD", "SHEPR_CLAUDE_CMD", "SHEPR_CODEX_CMD", "SHEPR_PROJECTS", "SHEPR_PUSH_ALLOW", "SHEPR_READONLY", "SHEPR_SESSIONS",
           "SHEPR_LOCAL_ONLY", "HERDR_SOCKET_PATH", "SHEPR_ALLOW_PROXY", "SHEPR_OWNER", "SHEPR_URL", "SHEPR_WATCH_FIXTURE"): os.environ.pop(_k, None)
here = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(here, "..", "server.py")
spec = importlib.util.spec_from_file_location("shepr_server", SERVER)
srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)
ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name); ok = ok and bool(cond)
def code_of(fn):
    try: fn(); return None
    except srv.HerdrError as e: return e.code
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

# ---- S1: a second server that cannot have the port must not replace server.json
st = tempfile.mkdtemp(); port = free_port()
env = dict(os.environ, SHEPR_STATE=st, SHEPR_LOCAL_ONLY="1", SHEPR_USAGE="0", SHEPR_WATCH="0", HERDR_BIN=os.path.join(st, "no-herdr"), PATH="/usr/bin:/bin")
one = subprocess.Popen([sys.executable, SERVER, str(port)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
info = None
for _ in range(80):
    try: info = json.load(open(os.path.join(st, "server.json"))); break
    except (OSError, ValueError): time.sleep(0.1)
two = subprocess.run([sys.executable, SERVER, str(port)], env=env, capture_output=True, text=True, timeout=30)
after = json.load(open(os.path.join(st, "server.json")))
check("S1: a second server on a taken port exits with a plain message", two.returncode != 0 and "cannot listen" in two.stderr and "Traceback" not in two.stderr)
check("S1: ...and server.json still names the server that IS running", info and after["pid"] == one.pid == info["pid"])
one.terminate(); one.wait(timeout=10)

# ---- S2: the tailnet identity is learned later when Tailscale was not ready at start, and server.json follows
real_run = srv.subprocess.run
answers = {"out": json.dumps({"BackendState": "NeedsLogin", "Self": {"DNSName": "", "UserID": 0}})}
def fake_ts(cmd, *a, **k):
    if cmd[:2] == ["tailscale", "status"]:
        return subprocess.CompletedProcess(cmd, 0, answers["out"], "")
    return real_run(cmd, *a, **k)
srv.subprocess.run = fake_ts
srv.OWNER.update(login=None, dns=None)
check("S2: a node that is not connected yet leaves the identity unknown", srv.learn_tailnet(quiet=True) is False and not srv.tailnet_known())
srv._tailnet.update(tried=0.0, port=8742)
check("S2: asking again while it is still not connected changes nothing", srv.relearn_tailnet() is False)
check("S2: ...and is not asked again inside the retry gap", srv.relearn_tailnet() is False and time.time() - srv._tailnet["tried"] < 2)
answers["out"] = json.dumps({"BackendState": "Running", "Self": {"DNSName": "box.tail1234.ts.net.", "UserID": 7}, "User": {"7": {"LoginName": "Owner@Example.com"}}})
srv._tailnet["tried"] = 0.0
check("S2: once Tailscale is connected the identity is learned without a restart", srv.relearn_tailnet() is True and srv.OWNER["dns"] == "box.tail1234.ts.net" and srv.OWNER["login"] == "owner@example.com")
check("S2: ...the Host allow-list has the tailnet name", "box.tail1234.ts.net" in srv.OWNER["names"])
check("S2: ...and server.json now carries the tailnet URL for `shepr pair`", json.load(open(srv.state_path("server.json")))["url"] == "https://box.tail1234.ts.net:8742")
srv.OWNER["login"] = "kept@example.com"; srv.OWNER["dns"] = None
srv.learn_tailnet(quiet=True)
check("S2: a login that is already known is never replaced by a later answer", srv.OWNER["login"] == "kept@example.com")
srv.subprocess.run = real_run

# ---- S3 + S4: the gate
class Hdrs(dict):
    def get(self, k, d=None):
        for key, v in self.items():
            if key.lower() == k.lower(): return v
        return d
class Fake:
    def __init__(self, headers, path="/api/queue"): self.headers, self.path, self.out = Hdrs(headers), path, None
    def json_out(self, code, obj): self.out = (code, obj.get("error"))
def gate(headers):
    f = Fake(headers); return srv.Handler.network_gate(f, False), f.out
srv.OWNER.update(login="owner@example.com", dns="box.tail1234.ts.net")
H = {"Host": "box.tail1234.ts.net:8742"}
check("S3: the owner through tailscale serve passes", gate(dict(H, **{"Tailscale-User-Login": "owner@example.com", "X-Forwarded-For": "100.64.0.9"}))[0] == "owner@example.com")
check("S3: a proxied request with no tailnet user on it (a tagged device) is refused", gate(dict(H, **{"X-Forwarded-For": "100.64.0.9"})) == (None, (403, "not_owner")))
check("S3: ...also when only X-Forwarded-Host is there", gate(dict(H, **{"X-Forwarded-Host": "box.tail1234.ts.net"})) == (None, (403, "not_owner")))
check("S3: a caller on this machine itself (no proxy headers) is still local", gate({"Host": "127.0.0.1:8742"})[0] == "local")
srv.ALLOW_PROXY = True
check("S3: SHEPR_ALLOW_PROXY=1 lets a reverse proxy of your own through", gate(dict(H, **{"X-Forwarded-For": "10.0.0.5"}))[0] == "local")
srv.ALLOW_PROXY = False
srv.OWNER["login"] = "fréd@example.com"
check("S4: a login that is not plain ASCII arrives RFC 2047 encoded and is decoded before it is compared",
      srv.header_login("=?utf-8?q?fr=C3=A9d@example.com?=") == "fréd@example.com" and gate(dict(H, **{"Tailscale-User-Login": "=?utf-8?q?fr=C3=A9d@example.com?="}))[0] == "fréd@example.com")
check("S4: a plain login is only lower-cased", srv.header_login("Owner@Example.com") == "owner@example.com" and srv.header_login(None) == "")
srv.OWNER.update(login=None, dns=None); srv.OWNER["names"].discard("box.tail1234.ts.net")
check("S2: a request for the tailnet name is refused while that name is not known yet", gate(H) == (None, (421, "bad_host")))

# ---- S5 + S6: anything unforeseen is ANSWERED (a dropped connection reads as "offline" on the phone)
srv.OWNER["names"].update({"127.0.0.1", "localhost"})
http_srv = srv.CappedServer(("127.0.0.1", 0), functools.partial(srv.Handler, directory=srv.HERE))
threading.Thread(target=http_srv.serve_forever, daemon=True).start()
def call(method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", http_srv.server_address[1], timeout=10)
    c.request(method, path, body=body, headers=dict({"Host": "127.0.0.1"}, **(headers or {})))
    r = c.getresponse(); data = r.read(); c.close()
    try: return r.status, json.loads(data)
    except ValueError: return r.status, {}
real_build = srv.shell_build
srv.shell_build = lambda: (_ for _ in ()).throw(TypeError("boom"))
import io, contextlib
err = io.StringIO()
with contextlib.redirect_stderr(err):
    status, body = call("GET", "/api/hello")
srv.shell_build = real_build
check("S5: an internal error is answered as a 500 with a plain message, not a dropped connection", status == 500 and body.get("error") == "internal" and "internal error" in body.get("message", ""))
check("S5: ...and the traceback is in the host's journal", "TypeError" in err.getvalue() and "boom" in err.getvalue())
status, body = call("POST", "/api/pair", body="[" * 6000 + "]" * 6000, headers={"Content-Type": "application/json", "X-Shepr": "1"})
check("S5: a body nested thousands deep is a 400, not a crash", status == 400 and body.get("error") == "bad_request")
status, body = call("GET", "/api/hello")
check("S5: the server still answers afterwards", status == 200 and body.get("app") == "shepr")
h = srv.Handler.__new__(srv.Handler); h.client_address = ("127.0.0.1", 1); h.requestline = "x"
with contextlib.redirect_stderr(io.StringIO()) as e6:
    try: h.log_message("Request timed out: %r", TimeoutError("t")); six = True
    except IndexError: six = False
check("S6: a one-argument log line (a timeout) no longer raises IndexError", six and "timed out" in e6.getvalue())
http_srv.shutdown()

# ---- S7: an open session costs one read of the pane, whatever its state
calls = []
PANE = "\n".join(["line %d" % i for i in range(60)] + ["Do you want to proceed?", "❯ 1. Yes", "  2. No"])
def fake_run(machine, args, timeout=10, want_json=True):
    calls.append(list(args))
    if args[:2] == ["agent", "get"]: return {"result": {"agent": {"pane_id": args[2], "agent_status": state["st"], "state_change_seq": 1, "cwd": "/x/alpha", "agent": "claude"}}}
    if args[:2] == ["agent", "read"]: return "\n".join(PANE.split("\n")[-int(args[args.index("--lines") + 1]):])
    raise srv.HerdrError("error", "not stubbed")
state = {"st": "blocked"}
real_herdr = srv.run_herdr
srv.run_herdr = fake_run
host = {"id": "h", "machine": None, "kind": "local", "enabled": True}
v = srv.pane_view(host, "w1:p1", 40)
reads = [c for c in calls if c[:2] == ["agent", "read"]]
check("S7: a blocked session is read ONCE (it used to be read twice: the text, then the menu)", len(reads) == 1 and reads[0][reads[0].index("--lines") + 1] == "80")
check("S7: ...the phone still gets the 40 lines it asked for, and the menu found in the 80", len(v["text"].split("\n")) == 40 and v["menu"] and [o["label"] for o in v["menu"]["options"]] == ["Yes", "No"])
calls.clear(); state["st"] = "working"
v = srv.pane_view(host, "w1:p1", 40)
check("S7: a session that is not blocked is read once, with just the lines asked for, and has no menu", [c[c.index("--lines") + 1] for c in calls if c[:2] == ["agent", "read"]] == ["40"] and v["menu"] is None)

# ---- S14: a key that is not a string is a bad key, not a crash
state["st"] = "blocked"
srv.SNAP["hosts"] = [{"id": "h", "status": "ok"}]
real_discover = srv.discover_hosts
srv.discover_hosts = lambda: [host]
check("S14: a list or an object where a key name belongs is refused as a bad key", code_of(lambda: srv.build_plan({"kind": "keys", "host": "h", "pane": "w1:p1", "keys": [["enter"]]})) == "bad_keys"
      and code_of(lambda: srv.build_plan({"kind": "keys", "host": "h", "pane": "w1:p1", "keys": [{"k": 1}]})) == "bad_keys")
srv.run_herdr = real_herdr

# ---- S8, S9, S13: files that are not what was expected
ch = tempfile.mkdtemp(); os.makedirs(os.path.join(ch, "sessions", "d"))
open(os.path.join(ch, "sessions", "d", "a.jsonl"), "w").write("[1, 2, 3]\n")
open(os.path.join(ch, "sessions", "d", "b.jsonl"), "w").write('"just a string"\n')
open(os.path.join(ch, "sessions", "d", "c.jsonl"), "w").write(json.dumps({"type": "session_meta", "payload": {"cwd": "/x/alpha"}}) + "\n")
srv.CODEX_HOME = ch; srv._codex_idx.update(t=0.0, map={}, cwd_of={})
check("S8: Codex session files whose first line is not an object are skipped, and the good one is still found", srv.codex_last_active("/x/alpha") is not None)
hh = tempfile.mkdtemp()
open(os.path.join(hh, "history.jsonl"), "w").write('[1]\n"x"\n{"display": 5}\n{"display": "/deploy now"}\nnot json\n')
srv.CLAUDE_HOME = hh
check("S9: prompt history lines that are JSON but not what Claude Code writes are skipped", srv.recent_commands() == ["deploy"])
srv.transcript_file = lambda a: os.path.join(hh, "gone.jsonl")
check("S13: a session file that vanished between the look and the stat gives no time, not an error", srv.transcript_mtime({}) is None)

# ---- S10 + S16: push
srv.PUSH["old"] = {"sub": {"endpoint": "https://web.push.apple.com/x"}, "t": 1}   # saved by a build that had no prefs at all
check("S10: a subscription saved without preferences reads as the defaults", srv.push_config("old")["prefs"] == srv.PUSH_PREF_DEFAULTS)
check("S10: ...and can have one preference changed without the others being lost", srv.push_set_prefs("old", {"quiet": True})["prefs"] == dict(srv.PUSH_PREF_DEFAULTS, quiet=True))
check("S10: the new kinds are off until asked for, the fleet stopping included", srv.PUSH_PREF_DEFAULTS == {"blocked": True, "done": False, "quiet": False, "stopped": False, "risk": False})
open(srv.state_path("vapid.json"), "w").write('{"d": 12345}')
srv._vapid.clear(); err = io.StringIO()
with contextlib.redirect_stderr(err): d, pub = srv.vapid_keys()
check("S16: a push key file that cannot be read is replaced loudly (every phone must subscribe again, and the journal says so)", "new one is made" in err.getvalue() and len(pub) == 65)
srv._vapid.clear(); err = io.StringIO()
with contextlib.redirect_stderr(err): d2, _ = srv.vapid_keys()
check("S16: ...and the new key is kept, quietly, from then on", d2 == d and err.getvalue() == "")

# ---- S11: named sessions are listed in the Hosts tab
srv.discover_hosts = real_discover
def fake_run2(machine, args, timeout=10, want_json=True):
    if args[:2] == ["session", "list"]: return {"sessions": [{"default": True, "name": "default", "running": True, "socket_path": "/c/herdr.sock"}, {"default": False, "name": "work", "running": True, "socket_path": "/c/s/work/herdr.sock"}]}
    if args[:2] == ["machine", "list"]: return [{"id": "m1", "label": "vic", "target": "vic", "enabled": True}]
    raise srv.HerdrError("error", "x")
srv.run_herdr = fake_run2; srv._sessions.update(at=0.0, found=[], read=False)
srv.SNAP["hosts"] = [{"id": srv.LOCAL, "status": "ok", "counts": {}}, {"id": f"{srv.LOCAL}/work", "status": "ok", "counts": {"blocked": 2}}]
hv = srv.hosts_view()["saved"]
check("S11: the Hosts tab lists a named session under this machine, before the saved machines", [(x["id"], x["kind"]) for x in hv] == [(srv.LOCAL, "local"), (f"{srv.LOCAL}/work", "session"), ("vic", "remote")])
check("S11: ...with its own counts", hv[1]["counts"] == {"blocked": 2} and hv[1]["session"] == "work")
srv.run_herdr = real_herdr

# ---- S12: why a unit that should be running is not (systemd and its journal are faked on PATH)
root = tempfile.mkdtemp()
def fake_bin(name, body):
    p = os.path.join(root, name)
    with open(p, "w") as f: f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
fake_bin("tailscale", "echo '{\"BackendState\": \"Running\"}'")
JOURNAL = ["Starting Shepr...", "sending serve config: 500 Unauthorized: must be root, or be an operator and able to run 'sudo tailscale' to serve a path or Unix socket",
           "shepr.service: Control process exited, code=exited, status=1/FAILURE", "shepr.service: Failed with result 'exit-code'.", "Failed to start Shepr.",
           "shepr.service: Scheduled restart job, restart counter is at 33662."]
open(os.path.join(root, "journal.txt"), "w").write("\n".join(JOURNAL) + "\n")
fake_bin("journalctl", 'while IFS= read -r l; do echo "$l"; done < "' + os.path.join(root, "journal.txt") + '"')   # shell builtins only: PATH holds nothing but the fakes
def status(show):
    fake_bin("systemctl", "printf '%s\\n' " + " ".join("'%s'" % l for l in show))
    home, stt = tempfile.mkdtemp(), tempfile.mkdtemp()
    d = os.path.join(home, ".config", "systemd", "user"); os.makedirs(d)
    open(os.path.join(d, "shepr.service"), "w").write(f"[Service]\nExecStart=/usr/bin/python3 {os.path.realpath(SERVER)} 8742\n")
    e = dict(os.environ, HOME=home, SHEPR_STATE=stt, PATH=root, HERDR_BIN=os.path.join(root, "no-herdr"))
    j = subprocess.run([sys.executable, SERVER, "status", "--json"], env=e, capture_output=True, text=True, timeout=60)
    t = subprocess.run([sys.executable, SERVER, "status"], env=e, capture_output=True, text=True, timeout=60)
    pr = subprocess.run([sys.executable, SERVER, "pair", "--no-wait"], env=e, capture_output=True, text=True, timeout=60)
    return json.loads(j.stdout), t.stdout, pr.stderr
j, text, pair = status(["ActiveState=activating", "SubState=auto-restart", "Result=exit-code", "NRestarts=33662"])
ue = j["unit_error"]
check("S12: status --json says which unit is failing, how often it restarted and its last real error line",
      ue and ue["unit"] == "shepr" and ue["restarts"] == 33662 and ue["last"].startswith("sending serve config") and "Scheduled restart" not in ue["last"])
check("S12: ...with the fix for the cause it recognises", ue["fix"] == "sudo tailscale set --operator=$USER")
check("S12: status prints it as a `why` line", "why: the unit shepr is failing to start" in text and "Unauthorized" in text)
check("S12: `shepr pair` explains instead of saying only 'start it first'", "failing to start" in pair and "Unauthorized" in pair and "journalctl --user -u shepr" in pair)
j, text, pair = status(["ActiveState=inactive", "SubState=dead", "Result=success", "NRestarts=0"])
check("S12: a unit that is simply stopped is not called failing: 'start it' is right", j["unit_error"] is None and "Start it first" in pair)

sys.exit(0 if ok else 1)
