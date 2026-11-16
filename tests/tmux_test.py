"""tmux hosts: read only, over ssh. What the poll and the pane view read, what a write meets, what the CLI saves.
A fake `ssh` stands in for the network and answers `tmux list-panes` / `tmux capture-pane` from a JSON file whose clock the test sets,
so nothing touches a real machine and no real herdr is asked.   Run: python3 tests/tmux_test.py"""
import argparse, importlib.util, json, os, stat, sys, tempfile, time
TMP = tempfile.mkdtemp()
FAKE_DIR = os.path.join(TMP, "fake"); os.makedirs(FAKE_DIR)
STATE = os.path.join(TMP, "state"); os.makedirs(STATE, mode=0o700)
fake = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_tmux_ssh.py")   # the stand-in for ssh, shared with tmux_ui.mjs
os.environ.update(SHEPR_STATE=STATE, SHEPR_SSH=fake, FAKE_TMUX_DIR=FAKE_DIR, SHEPR_LOCAL_ONLY="1", SHEPR_LABEL="hub")
for _k in ("SHEPR_READONLY", "SHEPR_AUTOADD"): os.environ.pop(_k, None)
here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("shepr_server", os.path.join(here, "..", "server.py"))
srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)
ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name); ok = ok and bool(cond)

NOW = 1_800_000_000
def world(**kw):
    st = {"now": NOW, "fail": None, "sessions": {
        "work": {"windows": [
            {"id": 3, "name": "main", "activity": NOW - 5, "panes": [{"id": 7, "path": "/srv/app", "cmd": "node", "text": "\n".join(f"line {i}" for i in range(1, 301))}]},
            {"id": 4, "name": "logs", "activity": NOW - 600, "panes": [{"id": 9, "path": "/var/log", "cmd": "tail", "text": "old\x1b[31m red\x1b[0m\nlast"}]}]},
        "secret": {"windows": [{"id": 8, "name": "private", "activity": NOW, "panes": [{"id": 12, "path": "/root", "cmd": "bash", "text": "do not show"}]}]}}}
    st.update(kw)
    json.dump(st, open(os.path.join(FAKE_DIR, "state.json"), "w"))
def calls():
    try: return [json.loads(l) for l in open(os.path.join(FAKE_DIR, "calls.log"))]
    except OSError: return []
def reset_calls():
    try: os.remove(os.path.join(FAKE_DIR, "calls.log"))
    except OSError: pass
def save(rows): srv.save_tmux_hosts(rows)
srv.TMUX_CACHE_S = 0   # a test changes the world between two reads

def tt(target="labbox", sessions=("work",)):
    t = srv.TmuxTarget(target); t.sessions = tuple(sessions); return t

# ---- the saved list: only exactly well-formed entries, nothing a shell or ssh could read as more than a name
world()
save([{"id": "lab", "target": "labbox", "sessions": ["work"]},
      {"id": "Upper", "target": "x", "sessions": []},                     # label rule
      {"id": "dash", "target": "-Jevilhost", "sessions": []},     # ssh would read it as an option
      {"id": "semi", "target": "x;rm -rf ~", "sessions": []},
      {"id": "badses", "target": "x", "sessions": ["a;b"]},
      {"id": "lab", "target": "dup", "sessions": []},                      # a repeat
      {"id": "hub", "target": "x", "sessions": []},                        # this machine's own name
      "junk", None, {"id": "ok2", "target": "user@192.168.5.9", "sessions": []}])
check("only well-formed entries are kept: a bad label, a target ssh would read as an option, shell characters, a bad session name, a repeat, this host's own name",
      [r["id"] for r in srv.tmux_hosts()] == ["lab", "ok2"])
check("the file is private (0600)", stat.S_IMODE(os.stat(srv.state_path("tmux_hosts.json")).st_mode) == 0o600)
open(srv.state_path("tmux_hosts.json"), "w").write("{not json")
check("an unreadable file is no hosts, never a crash", srv.tmux_hosts() == [])
open(srv.state_path("tmux_hosts.json"), "w").write('{"hosts": "nope"}')
check("a file of the wrong shape is no hosts", srv.tmux_hosts() == [])
save([{"id": "lab", "target": "labbox", "sessions": ["work"]}])

# ---- panes: shaped like herdr's agents, read from THAT machine's clock, only the sessions it may read
reset_calls()
agents, ws = srv.tmux_panes(tt(), fresh=True)
by = {a["pane_id"]: a for a in agents}
check("a pane is a Shepr pane id built from tmux's window and pane numbers", sorted(by) == ["w3:p7", "w4:p9"])
check("the session's name is the workspace label", ws == {"w3": "work", "w4": "work"})
check("a window that printed 5 s ago is working, one that was quiet for 10 minutes is idle", (by["w3:p7"]["agent_status"], by["w4:p9"]["agent_status"]) == ("working", "idle"))
check("it is never blocked or done, so it can never be a question in the queue or a push", {a["agent_status"] for a in agents} <= {"working", "idle"})
check("the row says what runs there and where", by["w3:p7"]["agent"] == "tmux" and by["w3:p7"]["terminal_title"] == "main (node)" and by["w3:p7"]["cwd"] == "/srv/app")
check("a session it was not told about is never listed", all(a["workspace_id"] != "w8" for a in agents))
c = calls()[0]
check("ssh is told: no prompt, a host key that must already be trusted, the target after --",
      "BatchMode=yes" in c and "StrictHostKeyChecking=yes" in c and c[c.index("--") + 1] == "labbox")
check("it asks for exactly that session, by exact name (=work)", "=work" in c[-1] and "-s" in c[-1])
world(now=NOW + 100)
check("the clock that decides working is the remote one: 100 s later the same window is idle even with our clock unchanged",
      {a["pane_id"]: a["agent_status"] for a in srv.tmux_panes(tt(), fresh=True)[0]}["w3:p7"] == "idle")
world()
agents_all, ws_all = srv.tmux_panes(tt("labbox", ()), fresh=True)
check("with no session named it lists every session (-a)", {a["workspace_id"] for a in agents_all} == {"w3", "w4", "w8"} and "-a" in calls()[-1][-1])
world(sessions={"work": {"windows": [{"id": 3, "name": "a\x1b]0;evil\x07b", "activity": NOW, "panes": [{"id": 7, "path": "/x\x1b[2J", "cmd": "sh", "text": ""}]}]}})
a0 = srv.tmux_panes(tt(), fresh=True)[0][0]
check("names and paths are cleaned of control characters", not any(c in a0["terminal_title"] + a0["cwd"] for c in "\x1b\x07"))
world()

# ---- the 2 s cache: the poll and the pane view that follows it are one ssh, not three
srv.TMUX_CACHE_S = 2.0; reset_calls()
srv.tmux_panes(tt(), fresh=True); srv.tmux_panes(tt()); srv.tmux_panes(tt())
check("one listing serves the next two reads", len(calls()) == 1)
srv.tmux_panes(tt(), fresh=True)
check("a poll always asks again", len(calls()) == 2)
srv.TMUX_CACHE_S = 0

# ---- failures say what they are
world(fail="Host key verification failed.")
try: srv.tmux_panes(tt(), fresh=True); err = None
except srv.HerdrError as e: err = (e.code, e.message)
check("an ssh failure is the host's error, in ssh's own words", err == ("tmux", "Host key verification failed."))
world()
try: srv.tmux_panes(tt("labbox", ("gone",)), fresh=True); err = None
except srv.HerdrError as e: err = (e.code, e.message)
check("a session that does not exist is that host's error", err == ("tmux", "can't find session: gone"))

# ---- the host list, the poll, and what a tmux host never does
hosts = srv.discover_hosts()
h = next(x for x in hosts if x["id"] == "lab")
check("a saved tmux host is a host of kind tmux whose machine is a TmuxTarget with its sessions",
      h["kind"] == "tmux" and isinstance(h["machine"], srv.TmuxTarget) and h["machine"].sessions == ("work",) and h["enabled"])
check("this host stays first", hosts[0]["id"] == "hub")
host, ag, ws2, err = srv.poll_host(h)
check("the poll reads its panes", err is None and sorted(a["pane_id"] for a in ag) == ["w3:p7", "w4:p9"] and ws2["w3"] == "work")
world(fail="boom")
host, ag, ws2, err = srv.poll_host(h)
check("a poll that fails is the host's error, not the queue's", ag is None and err == "tmux: boom")
world()
try: srv.run_herdr(h["machine"], ["pane", "run", "w3:p7", "ls"]); refused = None
except srv.HerdrError as e: refused = e.code
check("run_herdr refuses a tmux host, so nothing can ever be sent to it", refused == "tmux_read_only")
srv.HOST_SNAP["lab"] = {"entry": {"id": "lab", "kind": "tmux", "status": "ok", "counts": {}, "error": None}, "agents": []}
srv.SNAP.update(hosts=[{"id": "lab", "status": "ok", "kind": "tmux", "counts": {}}])
try: srv.need_write_host("lab"); refused = None
except srv.HerdrError as e: refused = e.code
check("a send, a start or a wake is refused when it is PREPARED, where the phone can say so", refused == "tmux_read_only")
check("reading it is allowed", srv.need_host("lab")["id"] == "lab")
for kind in ("text", "keys", "choose"):
    try: srv.build_plan({"kind": kind, "host": "lab", "pane": "w3:p7", "text": "hi", "keys": ["enter"], "option": 1}); refused = None
    except srv.HerdrError as e: refused = e.code
    check(f"build_plan({kind}) to a tmux pane is refused", refused == "tmux_read_only")
ob = srv.shape_agent(h, ag[0] if ag else srv.tmux_panes(h["machine"], fresh=True)[0][0], ws)
check("a tmux row carries readonly, and a herdr row does not",
      ob.get("readonly") is True and "readonly" not in srv.shape_agent({"id": "hub", "kind": "local"}, {"pane_id": "w1:p1", "agent_status": "idle"}, {}))
srv.READONLY = True
try: srv.build_plan({"kind": "text", "host": "lab", "pane": "w3:p7", "text": "hi"}); refused = None
except srv.HerdrError as e: refused = e.code
check("a read-only server still says so first", refused == "read_only")
srv.READONLY = False

# ---- a saved herdr machine with the same name: the tmux host keeps it, and the machine is not a second row
real_run, real_ls = srv.run_herdr, srv.local_sessions
os.environ.pop("SHEPR_LOCAL_ONLY")
srv.run_herdr = lambda m, a, **k: [{"label": "lab", "enabled": True}, {"label": "real", "enabled": True}]
srv.local_sessions = lambda: ([], True)
ids = [x["id"] for x in srv.discover_hosts()]
srv.run_herdr, srv.local_sessions = real_run, real_ls
os.environ["SHEPR_LOCAL_ONLY"] = "1"
check("a name used by a tmux host and a saved machine appears once, as the tmux host, and the other machines still come", ids == ["hub", "lab", "real"])

# ---- the pane's text, and what is not readable
reset_calls()
txt = srv.read_tail({"machine": h["machine"]}, "w3:p7", 40)
check("it reads the last lines of that pane", txt.split("\n")[-1] == "line 300" and len(txt.split("\n")) == 40)
c = calls()[-1][-1]
check("the pane is named as tmux's own %N, and only after it was found in the allowed session", "capture-pane" in c and "%7" in c)
check("escapes in the text are stripped", srv.read_tail({"machine": h["machine"]}, "w4:p9", 10).split("\n")[0] == "old red")
for bad, why in (("w8:p12", "a pane of a session it was not told about"), ("w3:p99", "a pane that does not exist"), ("%7", "a tmux id that is not a Shepr pane id"), ("w3:p7; rm", "shell characters")):
    try: srv.read_tail({"machine": h["machine"]}, bad, 10); code = None
    except srv.HerdrError as e: code = e.code
    check(f"refused: {why}", code in ("no_pane", "bad_pane"))
pv = srv.pane_view(h, "w3:p7", 20)
check("the pane view gives the text, the agent, and no menu", pv["text"].split("\n")[-1] == "line 300" and pv["menu"] is None and pv["agent"]["readonly"] is True and pv["agent"]["status"] == "working")

# ---- the queue: a watched pane is not work anyone waits for
srv.FLEET.update(had_work=False, stopped_at=None, told=False)
srv.HOST_ORDER[:] = ["lab"]; srv.HOST_SNAP.clear()
srv._poll_and_publish(h)
check("it is in the list of everything", {a["pane"] for a in srv.SNAP["agents"]} == {"w3:p7", "w4:p9"} and all(a.get("readonly") for a in srv.SNAP["agents"]))
check("...but a working tmux pane does not start the 'everything stopped' clock", srv.FLEET["had_work"] is False)
q = srv.build_queue()
check("the queue keeps the flag for the phone", q["agents"] and all(a.get("readonly") for a in q["agents"]))
check("and it waits on nobody", not any(a["status"] in ("blocked", "done") for a in q["agents"]))
hv = srv.hosts_view()
row = next(x for x in hv["saved"] if x["id"] == "lab")
check("the Hosts tab lists it with its sessions and status, and no switch to flip", row["kind"] == "tmux" and row["sessions"] == ["work"] and row["status"] == "ok" and row["target"] == "labbox")
srv.HOST_ORDER[:] = []; srv.HOST_SNAP.clear(); srv.SNAP.update(hosts=[], agents=[])

# ---- ssh's shared connection: a private directory, or none
d = srv.tmux_ctl_dir()
check("the shared-connection directory is private", d and stat.S_IMODE(os.stat(d).st_mode) == 0o700)
reset_calls(); srv.tmux_panes(tt(), fresh=True)
c = calls()[-1]
check("ssh keeps one connection for a while, so a poll is not a login every 15 s", "ControlPersist=120" in c and any(x.startswith("ControlPath=" + d) for x in c))
os.chmod(d, 0o755)
check("a directory others can enter is not used", srv.tmux_ctl_dir() is None)
reset_calls(); srv.tmux_panes(tt(), fresh=True)
check("...and then every call is its own login, with no master", "ControlMaster=no" in calls()[-1])
os.chmod(d, 0o700)
real_dir = srv.STATE_DIR
srv.STATE_DIR = "/" + "x" * 80
long_dir = srv.tmux_ctl_dir()
srv.STATE_DIR = real_dir
check("a state folder too long for a unix socket falls back to a private /tmp folder", long_dir == f"/tmp/shepr-ssh-{os.getuid()}" and stat.S_IMODE(os.stat(long_dir).st_mode) == 0o700)
try: os.rmdir(long_dir)   # only if it is empty: it was made by this check
except (OSError, TypeError): pass

# ---- the CLI
def cli(*argv):
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd")
    tx = sub.add_parser("tmux"); ts = tx.add_subparsers(dest="tmux_cmd")
    t1 = ts.add_parser("add"); t1.add_argument("target"); t1.add_argument("--session", action="append"); t1.add_argument("--label")
    ts.add_parser("list"); t3 = ts.add_parser("remove"); t3.add_argument("id")
    return srv.cli_tmux(ap.parse_args(["tmux"] + list(argv)))
import contextlib, io
def run_cli(*argv, ns=None):
    """ns: call cli_tmux with a ready Namespace instead of parsing argv (argparse itself refuses a leading '-', so the guard behind it is tested directly)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = srv.cli_tmux(ns) if ns is not None else cli(*argv)
        except SystemExit as e:   # argparse refusing the line: say so, never end the whole test silently
            rc = ("argparse", e.code)
    return rc, out.getvalue(), err.getvalue()
srv.machine_profiles = lambda: [{"label": "real", "id": "x"}]
save([]); world()
rc, o, e = run_cli("add", "labbox", "--session", "work", "--label", "lab")
check("add proves the host can be read, then saves it, and says it is read only", rc == 0 and "added lab" in o and "2 panes" in o and "Read only" in o)
check("...in the file the poll reads", srv.tmux_hosts() == [{"id": "lab", "target": "labbox", "sessions": ["work"]}])
rc, o, e = run_cli("list")
check("list shows it", rc == 0 and "lab" in o and "sessions: work" in o)
rc, o, e = run_cli("add", "labbox", "--session", "work", "--label", "lab")
check("the same name twice is refused", rc == 1 and "already a tmux host" in e)
rc, o, e = run_cli("add", "otherbox", "--label", "real")
check("a name that is a saved herdr machine is refused", rc == 1 and "saved herdr machine" in e)
rc, o, e = run_cli(ns=argparse.Namespace(tmux_cmd="add", target="-Jevilhost", session=None, label="bad"))
check("a target ssh would read as an option is refused by the CLI itself", rc == 1 and "ssh name" in e and len(srv.tmux_hosts()) == 1)
rc, o, e = run_cli("add", "-Jevilhost", "--label", "bad")
check("and argparse refuses it before that, the same way", rc == ("argparse", 2))
rc, o, e = run_cli("add", "otherbox", "--session", "a;b")
check("a session name with shell characters is refused", rc == 1 and "not a tmux session name" in e)
rc, o, e = run_cli("add", "otherbox", "--label", "Bad Name")
check("a label the phone cannot use is refused", rc == 1 and "Use --label" in e)
world(fail="Host key verification failed.")
rc, o, e = run_cli("add", "newbox")
check("a host key that is not trusted yet is NOT trusted by Shepr: it says to ssh there once", rc == 1 and "ssh newbox" in e and "never trusts a new host key" in e and len(srv.tmux_hosts()) == 1)
world(sessions={})
rc, o, e = run_cli("add", "emptybox")
check("a host with no tmux panes is not added", rc == 1 and "no tmux panes" in e)
world()
rc, o, e = run_cli("remove", "nope")
check("removing a name that is not there says so", rc == 1)
rc, o, e = run_cli("remove", "lab")
check("remove drops it and says nothing was changed on that machine", rc == 0 and "Nothing was changed" in o and srv.tmux_hosts() == [])

# ---- the shell is not parsed twice: a session with a space is one word
world(sessions={"my work": {"windows": [{"id": 1, "name": "w", "activity": NOW, "panes": [{"id": 2, "path": "/", "cmd": "sh", "text": "x"}]}]}})
check("a session name with a space is read as one name", [a["pane_id"] for a in srv.tmux_panes(tt("labbox", ("my work",)), fresh=True)[0]] == ["w1:p2"])

sys.exit(0 if ok else 1)
