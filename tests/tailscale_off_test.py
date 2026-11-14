"""Issue #5: with Tailscale off the unit cannot come up (`tailscale serve` fails), and `shepr pair` used to answer "the server is not
running, start it" with no hint that Tailscale was the reason. Now `pair` and `status` say so, and only when the server is down.
Tailscale is faked with a script on PATH (real tailscale 1.104.7 outputs are in server.py next to TAILSCALE_STATES); nothing
touches a real tailnet.  Run: python3 tests/tailscale_off_test.py"""
import importlib.util, json, os, stat, subprocess, sys, tempfile
here = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(here, "..", "server.py")
ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name); ok = ok and bool(cond)

root = tempfile.mkdtemp()
def fake_bin(name, body):
    d = os.path.join(root, name); os.makedirs(d)
    p = os.path.join(d, "tailscale")
    with open(p, "w") as f: f.write("#!/bin/sh\necho x >> '" + os.path.join(root, name + ".calls") + "'\n" + body + "\n")
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
    return d
state_json = lambda s: "echo '" + json.dumps({"BackendState": s, "Self": {"DNSName": "", "UserID": 0}}) + "'"
BINS = {"Stopped": fake_bin("stopped", state_json("Stopped")), "NeedsLogin": fake_bin("login", state_json("NeedsLogin")),
        "Starting": fake_bin("starting", state_json("Starting")), "Weird": fake_bin("weird", state_json("SomethingNew")),
        "Running": fake_bin("running", state_json("Running")),
        "down": fake_bin("down", "echo 'failed to connect to local tailscaled' >&2; exit 1"),
        "garbage": fake_bin("garbage", "echo '[1,2]'")}
empty = os.path.join(root, "nothing"); os.makedirs(empty)   # a PATH with no tailscale in it

def run(sub, bin_dir, extra_state=None):
    home, st = tempfile.mkdtemp(), tempfile.mkdtemp()
    if extra_state: json.dump(extra_state, open(os.path.join(st, "server.json"), "w"))
    # PATH is the fake dir alone: a real tailscale in /usr/bin would otherwise be found. HERDR_BIN points at nothing, so `status` never reads a real herdr.
    env = dict(os.environ, HOME=home, SHEPR_STATE=st, PATH=bin_dir, HERDR_BIN=os.path.join(root, "no-herdr"))
    for k in ("SHEPR_URL", "HERDR_SOCKET_PATH", "SHEPR_SESSIONS"): env.pop(k, None)
    r = subprocess.run([sys.executable, SERVER] + sub, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    return r.returncode, r.stdout, r.stderr

# ---- pair, server down
rc, out, err = run(["pair", "--no-wait"], BINS["Stopped"])
check("pair with Tailscale turned off names Tailscale as the reason, says how to fix it, and still names how to start Shepr",
      rc == 1 and "Tailscale is turned off" in err and "tailscale up" in err and "the unit you installed" in err)   # no unit in the temp HOME, so the hint is the generic one
check("...and it does not print the server's own 'tailnet requests will be refused' line first (pair never serves a request)", "will be refused" not in err and "KeyError" not in err)
check("...it is one message, no traceback", "Traceback" not in err and err.startswith("shepr: the server is not running here"))
rc, out, err = run(["pair", "--no-wait"], BINS["NeedsLogin"])
check("a logged-out node says logged out", "Tailscale is logged out" in err)
rc, out, err = run(["pair", "--no-wait"], BINS["Starting"])
check("a node that is still connecting says to wait, not to log in", "still connecting" in err and "wait a few seconds" in err)
rc, out, err = run(["pair", "--no-wait"], BINS["Weird"])
check("a state this version does not know is still named, with the generic fix", "SomethingNew" in err and "tailscale up" in err)
rc, out, err = run(["pair", "--no-wait"], BINS["down"])
check("tailscaled itself not running is told apart from Tailscale being off", "tailscaled" in err and "systemctl enable --now tailscaled" in err)
rc, out, err = run(["pair", "--no-wait"], empty)
check("no tailscale command at all says it is not installed", "not installed" in err)

# ---- the old answer stays exactly the old answer when Tailscale is fine, or cannot be told
for label, b in (("connected", "Running"), ("an unexpected reply", "garbage")):
    rc, out, err = run(["pair", "--no-wait"], BINS[b])
    check(f"Tailscale {label}: the old 'start it first' message, with no word about Tailscale", rc == 1 and "Start it first:" in err and "Tailscale" not in err)

# ---- status
rc, out, err = run(["status", "--json"], BINS["Stopped"])
d = json.loads(out)
check("status --json carries the reason when the server is down, for the bar widget", d["running"] is False and d["tailscale"] == "Tailscale is turned off on this machine. Fix: tailscale up")
rc, out, err = run(["status"], BINS["Stopped"])
check("status prints it as a warning line", "warning: Tailscale is turned off on this machine" in out)
rc, out, err = run(["status", "--json"], BINS["Running"])
check("status with Tailscale connected has no reason", json.loads(out)["tailscale"] is None)

# ---- a running server is never charged a tailscale call: the bar widget polls status
st = {"pid": os.getpid(), "port": 8742, "url": "http://127.0.0.1:8742", "started": 0}
calls = os.path.join(root, "stopped.calls")
before = os.path.getsize(calls) if os.path.exists(calls) else 0
rc, out, err = run(["status", "--json"], BINS["Stopped"], extra_state=st)
d = json.loads(out)
after = os.path.getsize(calls) if os.path.exists(calls) else 0
check("status with the server up reports running and no Tailscale reason", d["running"] is True and d["tailscale"] is None)
check("...and never ran the tailscale command at all", after == before)
rc, out, err = run(["pair", "--no-wait", "--json"], BINS["Stopped"], extra_state=st)
check("pair with the server up never complains about Tailscale", "Tailscale" not in err and rc == 0)

# ---- a tailscale that hangs is "cannot tell", never a hang or a wrong accusation
os.environ["SHEPR_STATE"] = tempfile.mkdtemp()
spec = importlib.util.spec_from_file_location("shepr_server", SERVER)
srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)
real = srv.subprocess.run
def hang(*a, **k): raise subprocess.TimeoutExpired(a[0], 6)
srv.subprocess.run = hang
check("a tailscale call that times out is 'cannot tell': no reason is given", srv.tailscale_problem() is None)
srv.subprocess.run = real

sys.exit(0 if ok else 1)
