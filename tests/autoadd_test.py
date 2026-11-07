"""Automatic tailnet adding: the probe's `running` answer, the scan's quiet re-probe rules, and auto_add's guards.
Pure Python, nothing touches a real machine.  Run: python3 tests/autoadd_test.py"""
import importlib.util, json, os, sys, tempfile, time, types
os.environ["SHEPR_STATE"] = tempfile.mkdtemp()
for _k in ("SHEPR_AUTOADD", "SHEPR_SCAN_S", "SHEPR_READONLY", "SHEPR_LOCAL_ONLY"): os.environ.pop(_k, None)   # a test pins its own settings, not the shell's
here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("shepr_server", os.path.join(here, "..", "server.py"))
srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)
ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name); ok = ok and bool(cond)

# ---- ssh_probe: what the remote said -> (state, detail, running)
real_run = srv.subprocess.run
calls = []
def fake_ssh(out="", err="", rc=0, boom=None):
    def run(argv, **kw):
        calls.append(argv)
        if boom: raise boom
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)
    srv.subprocess.run = run
J = lambda *sess: json.dumps({"sessions": [{"name": n, "default": n == "default", "running": r} for n, r in sess]})
fake_ssh("/usr/bin/herdr\n" + J(("default", True), ("hrtest", False)) + "\n")
check("herdr found and its default server is up -> ready, running", srv.ssh_probe("vic") == ("ready", "/usr/bin/herdr", True))
a = calls[-1]
check("the probe still never prompts and never trusts a new host key, and the target is the host ssh dials",
      "BatchMode=yes" in a and "StrictHostKeyChecking=yes" in a and a[a.index("StrictHostKeyChecking=yes") - 1] == "-o" and a[-2] == "vic" and a[-1] == srv.REMOTE_PROBE)
fake_ssh("/usr/bin/herdr\n" + J(("default", False), ("agents", True)) + "\n")
check("a server running only in ANOTHER session is not a running default (adding would start a new, empty one)", srv.ssh_probe("x") == ("ready", "/usr/bin/herdr", False))
fake_ssh("/usr/bin/herdr\n" + J(("default", False)) + "\n")
check("herdr installed, nothing running -> ready, running False", srv.ssh_probe("x")[0::2] == ("ready", False))
fake_ssh("/opt/homebrew/bin/herdr\n")
check("an older herdr that cannot list sessions -> ready, running unknown (None), never True", srv.ssh_probe("x") == ("ready", "/opt/homebrew/bin/herdr", None))
fake_ssh("/usr/bin/herdr\n{not json at all\n")
check("session list that is not JSON -> running unknown", srv.ssh_probe("x")[2] is None)
fake_ssh("/usr/bin/herdr\n" + json.dumps({"sessions": "nope"}) + "\n")
check("session list of the wrong shape -> running unknown", srv.ssh_probe("x")[2] is None)
fake_ssh("Welcome to box\nlast login: today\n/home/u/.local/bin/herdr\n" + J(("default", True)) + "\n")
check("login-profile chatter on stdout is ignored", srv.ssh_probe("x") == ("ready", "/home/u/.local/bin/herdr", True))
fake_ssh("", "", 1)
check("no herdr -> noherdr", srv.ssh_probe("x")[0] == "noherdr")
fake_ssh("", "user@x: Permission denied (publickey).", 255)
check("key not authorized -> nokey", srv.ssh_probe("x")[0] == "nokey")
fake_ssh("", "Host key verification failed.", 255)
check("unknown host key -> hostkey, never running", srv.ssh_probe("x") == ("hostkey", "first connect needed: run `ssh x` once and check the fingerprint", None))
fake_ssh("", "ssh: connect to host x port 22: Connection refused", 255)
check("refused -> down", srv.ssh_probe("x")[0] == "down")
fake_ssh(boom=srv.subprocess.TimeoutExpired("ssh", 14))
check("timeout -> down", srv.ssh_probe("x") == ("down", "timed out", None))
fake_ssh("", "** The server may need to be upgraded\nWarning: Permanently added x\n", 1)
check("ssh's advisory banners are not taken for a failure", srv.ssh_probe("x")[0] == "noherdr")
srv.subprocess.run = real_run

# ---- tailnet_peers: automation may touch only the online Linux and macOS nodes of THIS user that are not saved yet
ts = {"Self": {"DNSName": "gus.tail.ts.net.", "UserID": 1},
      "Peer": {k: dict({"DNSName": k + ".tail.ts.net.", "UserID": 1, "Online": True, "OS": "linux", "TailscaleIPs": ["100.0.0.1"]}, **v) for k, v in {
          "mine": {}, "mac": {"OS": "macOS"}, "saved": {}, "asleep": {"Online": False}, "phone": {"OS": "iOS"},
          "theirs": {"UserID": 2}, "bytarget": {}}.items()}}
def fake_ts(out):
    srv.subprocess.run = lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout=json.dumps(out), stderr="")
real_profiles = srv.machine_profiles
srv.machine_profiles = lambda: [{"label": "saved", "target": "saved"}, {"label": "other", "target": "bytarget"}]
fake_ts(ts)
names = [p["name"] for p in srv.tailnet_peers()]
srv.subprocess.run = real_run; srv.machine_profiles = real_profiles
check("only your own, online, Linux or macOS nodes that are not saved (by label or by target); never a shared-in node, a phone or this host", names == ["mac", "mine"])

# ---- scan_tailnet: a background scan stays quiet, the Hosts tab's scan is always fresh
peers_now = [{"name": n, "dns": n + ".ts.net", "os": "linux", "ip": "100.0.0.1", "target": n} for n in ("nohe", "locked", "newkey", "off", "good", "idle")]
answers = {"nohe": ("noherdr", "no herdr", None), "locked": ("nokey", "no key", None), "newkey": ("hostkey", "first", None),
           "off": ("down", "refused", None), "good": ("ready", "/usr/bin/herdr", True), "idle": ("ready", "/usr/bin/herdr", False)}
probed = []
srv.tailnet_peers = lambda: [dict(p) for p in peers_now]
srv.ssh_probe = lambda t: (probed.append(t), answers[t])[1]
srv.AUTOADD = False
srv.SCAN_MEMO.clear(); srv.SCAN.update(at=0, running=True, peers=[])
srv.scan_tailnet(force=True)
check("a scan from the Hosts tab asks every node", sorted(probed) == sorted(answers) and not srv.SCAN["running"])
check("the scan remembers how many nodes it actually asked", srv.SCAN["probed"] == len(answers))
check("each peer carries its state and whether a server is running", {p["name"]: (p["state"], p["running"]) for p in srv.SCAN["peers"]}["good"] == ("ready", True)
      and {p["name"]: p["running"] for p in srv.SCAN["peers"]}["idle"] is False)
probed.clear(); srv.SCAN["running"] = True
srv.scan_tailnet(force=False)
check("a background scan right after does not log in again to no-herdr, needs-key or first-connect nodes", sorted(probed) == ["good", "idle", "off"])
check("...and says it asked only the three it had to", srv.SCAN["probed"] == 3)
check("...yet still reports their last answer", {p["name"]: p["state"] for p in srv.SCAN["peers"]}["nohe"] == "noherdr")
probed.clear(); srv.SCAN["running"] = True
t_old = time.time() - 7200
srv.SCAN_MEMO["nohe"] = (t_old,) + srv.SCAN_MEMO["nohe"][1:]
srv.scan_tailnet(force=False)
check("after an hour it asks that node again", "nohe" in probed and "locked" not in probed)
probed.clear(); srv.SCAN["running"] = True
srv.scan_tailnet(force=True)
check("and the Hosts tab's Scan always asks everyone, whatever was remembered", sorted(probed) == sorted(answers))

# ---- auto_add: which scanned peers get saved without a tap
adds, polls = [], []
srv.run_herdr = lambda machine, args, timeout=10, want_json=True: adds.append(list(args)) or ""
srv.poll_all = lambda wait=2.0: polls.append(1)
P = lambda name, state="ready", running=True, target=None: {"name": name, "target": target or name, "state": state, "running": running, "detail": "", "os": "linux", "ip": ""}
def audits():
    try: return [json.loads(l) for l in open(srv.state_path("sends.jsonl"))]
    except OSError: return []
srv.READONLY = False; srv.AUTO_FAILED.clear()
srv.auto_add([P("good"), P("idle", running=False), P("old", running=None), P("newkey", "hostkey", None), P("nohe", "noherdr", None), P("off", "down", None), P("nokey", "nokey", None),
               P("odd1", "hostkey", True), P("odd2", "nokey", True), P("odd3", "down", True)])   # records the probe cannot produce today: the state gate alone must stop them
check("only the node whose default server is running is added, with the same command the Add button runs",
      adds == [["machine", "add", "good", "--label", "good"]])
check("...and the queue is refreshed so it shows up at once", len(polls) == 1)
au = [a for a in audits() if a["kind"] == "host_add_auto"]
check("the add is in the audit log, marked automatic", len(au) == 1 and au[0]["ok"] is True and au[0]["steps"][0]["args"] == ["add", "good", "--label", "good"])
adds.clear(); polls.clear()
srv.auto_add([P("9lives"), P("Upper"), P("-oProxy"), P("has.dot"), P("x" * 40), P("ok-one", target="-oProxyCommand=x")])
check("a name herdr would refuse or ssh could read as an option is never passed on (label or target)", adds == [] and polls == [])
srv.auto_add([P("n%d" % i) for i in range(9)])
check("one scan adds at most five", len(adds) == 5 and adds[-1][-1] == "n4")
adds.clear()
srv.READONLY = True
srv.auto_add([P("good2")])
check("a read-only server adds nothing", adds == [])
srv.READONLY = False
def failing(machine, args, timeout=10, want_json=True):
    adds.append(list(args)); raise srv.HerdrError("error", "label already in use")
srv.run_herdr = failing; srv.AUTO_FAILED.clear(); adds.clear(); polls.clear()
srv.auto_add([P("clash")])
check("a failed add is audited with herdr's own words and refreshes nothing", polls == [] and any(a["kind"] == "host_add_auto" and a["ok"] is False and "label already in use" in a["err"] for a in audits()))
srv.auto_add([P("clash")])
check("...and is not tried again straight away", len(adds) == 1)
srv.AUTO_FAILED["clash"] = time.time() - srv.AUTOADD_RETRY_S - 5
srv.auto_add([P("clash")])
check("...but is tried again after an hour", len(adds) == 2)

# ---- the switch: a finished scan only adds when SHEPR_AUTOADD is on
srv.run_herdr = lambda machine, args, timeout=10, want_json=True: adds.append(list(args)) or ""
srv.AUTO_FAILED.clear(); adds.clear()
peers_now[:] = [{"name": "good", "dns": "good.ts.net", "os": "linux", "ip": "100.0.0.1", "target": "good"}]
srv.SCAN_MEMO.clear(); srv.SCAN["running"] = True; srv.AUTOADD = False
srv.scan_tailnet(force=True)
check("with auto-add off a scan finds the node and adds nothing", adds == [] and srv.SCAN["peers"][0]["state"] == "ready")
srv.SCAN_MEMO.clear(); srv.SCAN["running"] = True; srv.AUTOADD = True
srv.scan_tailnet(force=True)
check("with auto-add on the same scan adds it", adds == [["machine", "add", "good", "--label", "good"]])
import subprocess
def autoadd_with(extra_env):
    code = "import importlib.util as u,sys;s=u.spec_from_file_location('s',sys.argv[1]);m=u.module_from_spec(s);s.loader.exec_module(m);print(m.AUTOADD)"
    r = subprocess.run([sys.executable, "-c", code, os.path.join(here, "..", "server.py")], env=dict(os.environ, **extra_env), capture_output=True, text=True)
    return r.stdout.strip()
check("the switch is on only for SHEPR_AUTOADD=1, and never on a read-only server",
      [autoadd_with(e) for e in ({}, {"SHEPR_AUTOADD": "1"}, {"SHEPR_AUTOADD": "yes"}, {"SHEPR_AUTOADD": "1", "SHEPR_READONLY": "1"})] == ["False", "True", "False", "False"])
sys.exit(0 if ok else 1)
