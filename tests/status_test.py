#!/usr/bin/env python3
"""`shepr status`: what the Omarchy bar widget polls. It only reads, never needs the server or a phone, and every
fact comes from a stub herdr and a temp state directory. Run: python3 tests/status_test.py"""
import json, os, stat, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.realpath(os.path.join(HERE, "..", "server.py"))
bad = 0


def check(name, ok, extra=""):
    global bad
    bad += 0 if ok else 1
    print(("PASS " if ok else "FAIL ") + name + ((" :: " + str(extra)[:240]) if (extra and not ok) else ""))


def stub_herdr(path, agents, workspaces, fail=None):
    """A fake herdr that answers `agent list` and `workspace list`, or fails like the real one does."""
    body = ["#!/usr/bin/env python3", "import json, sys", f"A = {agents!r}", f"W = {workspaces!r}", f"FAIL = {fail!r}",
            "if FAIL:", "    print(json.dumps({'error': {'code': 'boom', 'message': FAIL}}), file=sys.stderr); sys.exit(1)",
            "args = [a for a in sys.argv[1:] if not a.startswith('--')]",
            "if args[:2] == ['agent', 'list']: print(json.dumps({'result': {'agents': A}}))",
            "elif args[:2] == ['workspace', 'list']: print(json.dumps({'result': {'workspaces': W}}))",
            "else: sys.exit(2)"]
    with open(path, "w") as f:
        f.write("\n".join(body) + "\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


def status(home, state, herdr, *flags):
    env = {"PATH": "/usr/bin:/bin", "HOME": home, "SHEPR_STATE": state, "HERDR_BIN": herdr, "XDG_STATE_HOME": os.path.join(home, "s"),
           "XDG_CONFIG_HOME": os.path.join(home, "c")}
    return subprocess.run([sys.executable, "-I", SERVER, "status", *flags], env=env, capture_output=True, text=True, timeout=60)


AG = [
    {"pane_id": "w1:p1", "workspace_id": "w1", "agent": "claude", "agent_status": "blocked", "cwd": "/x/alpha", "terminal_title_stripped": "Do you want to proceed?"},
    {"pane_id": "w2:p1", "workspace_id": "w2", "agent": "codex", "agent_status": "working", "cwd": "/x/beta", "terminal_title_stripped": ""},
    {"pane_id": "w3:p1", "workspace_id": "w9", "agent": "claude", "agent_status": "blocked", "cwd": "/x/gamma", "terminal_title_stripped": ""},
    {"pane_id": "w4:p1", "workspace_id": "w4", "agent": "claude", "agent_status": "idle", "cwd": "/x/delta", "terminal_title_stripped": ""},
]
WS = [{"workspace_id": "w1", "label": "alpha-project"}, {"workspace_id": "w2", "label": "beta"}, {"workspace_id": "w4", "label": "delta"}]

with tempfile.TemporaryDirectory() as td:
    home, state = os.path.join(td, "home"), os.path.join(td, "state")
    os.makedirs(home); os.makedirs(state)
    herdr = os.path.join(td, "herdr"); stub_herdr(herdr, AG, WS)

    # 1. nothing running, nothing paired, no unit: it says so and exits 0 (a status call is not a failure)
    r = status(home, state, herdr, "--json")
    d = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {}
    check("status: exits 0 and prints one JSON object even when the server is down", r.returncode == 0 and isinstance(d, dict) and d, r.stderr)
    check("status: reports the server as not running, no url, no phones, no unit", d.get("running") is False and d.get("url") is None and d.get("devices") == 0 and d.get("units") == [], d)
    check("status: counts agents by state and lists the blocked ones with their project name",
          d.get("herdr") is True and d.get("counts") == {"blocked": 2, "working": 1, "idle": 1}
          and [w["project"] for w in d["waiting"]] == ["alpha-project", "gamma"] and d["waiting"][0]["title"] == "Do you want to proceed?", d)
    check("status: a workspace herdr no longer lists falls back to the folder name", d.get("waiting", [{}, {}])[1].get("project") == "gamma", d)

    # 2. a live server.json (this process is alive), two devices, a unit that runs this server.py
    with open(os.path.join(state, "server.json"), "w") as f:
        json.dump({"pid": os.getpid(), "port": 8742, "url": "https://box.example.ts.net:8742", "started": 1}, f)
    with open(os.path.join(state, "devices.json"), "w") as f:
        json.dump({"a": {"name": "phone"}, "b": {"name": "ipad"}}, f)
    udir = os.path.join(home, ".config", "systemd", "user"); os.makedirs(udir)
    with open(os.path.join(udir, "shepr.service"), "w") as f:
        f.write(f"[Service]\nExecStart=/usr/bin/python3 {SERVER} 8742\n")
    d = json.loads(status(home, state, herdr, "--json").stdout)
    check("status: running, with the url the server wrote, the phone count and the unit that runs this file",
          d["running"] is True and d["url"] == "https://box.example.ts.net:8742" and d["devices"] == 2 and d["units"] == ["shepr"], d)

    # 3. herdr fails or is missing: the answer says why, counts stay empty, still exit 0
    bad_herdr = os.path.join(td, "herdr-bad"); stub_herdr(bad_herdr, [], [], fail="no server socket")
    r = status(home, state, bad_herdr, "--json"); d = json.loads(r.stdout)
    check("status: when herdr fails it says why and lists nothing", r.returncode == 0 and d["herdr"] is False and "no server socket" in d.get("herdr_error", "") and d["waiting"] == [], d)
    d = json.loads(status(home, state, os.path.join(td, "no-such-herdr"), "--json").stdout)
    check("status: when herdr is not installed it says so", d["herdr"] is False and "not found" in d.get("herdr_error", ""), d)
    odd = os.path.join(td, "herdr-odd")
    with open(odd, "w") as f:
        f.write("#!/bin/sh\necho '{\"result\": 5}'\n")
    os.chmod(odd, 0o755)
    r = status(home, state, odd, "--json"); d = json.loads(r.stdout)
    check("status: a shape it does not know is reported, never a traceback", r.returncode == 0 and d["herdr"] is False and d.get("herdr_error"), (r.stderr, d))

    # 4. the human form names the state, the phones and what is waiting, and a down server gets the start hint
    r = status(home, state, herdr)
    check("status: the plain text form names the url, the phones and who is waiting",
          "running at https://box.example.ts.net:8742" in r.stdout and "2 phone(s) paired" in r.stdout and "waiting: alpha-project - Do you want to proceed?" in r.stdout, r.stdout)
    os.remove(os.path.join(state, "server.json"))
    r = status(home, state, herdr)
    check("status: a stopped server prints the command that starts it", "not running" in r.stdout and "systemctl --user start shepr" in r.stdout, r.stdout)
    check("status: it never touches the state directory (nothing new appears in it)", sorted(os.listdir(state)) == ["devices.json"], os.listdir(state))

# 5. `pair --cancel` drops a waiting code and needs no server (the bar widget's Cancel button runs it)
with tempfile.TemporaryDirectory() as td:
    home, state = os.path.join(td, "home"), os.path.join(td, "state")
    os.makedirs(home); os.makedirs(state)
    env = {"PATH": "/usr/bin:/bin", "HOME": home, "SHEPR_STATE": state, "XDG_STATE_HOME": os.path.join(home, "s")}
    with open(os.path.join(state, "pairing.json"), "w") as f:
        json.dump({"hash": "x", "expires": 4102444800, "created": 1, "used_by": None}, f)
    r = subprocess.run([sys.executable, "-I", SERVER, "pair", "--cancel"], env=env, capture_output=True, text=True, timeout=60)
    check("pair --cancel: a waiting code is removed and the command succeeds with no server running",
          r.returncode == 0 and not os.path.exists(os.path.join(state, "pairing.json")) and "cancelled" in r.stdout, (r.returncode, r.stdout, r.stderr))
    r = subprocess.run([sys.executable, "-I", SERVER, "pair", "--cancel"], env=env, capture_output=True, text=True, timeout=60)
    check("pair --cancel: with nothing waiting it says so and still succeeds", r.returncode == 0 and "no pairing code" in r.stdout, (r.returncode, r.stdout, r.stderr))

sys.exit(1 if bad else 0)
