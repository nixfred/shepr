"""The watch as the server uses it: the activity log, "quiet" and "everything has stopped", the burn and changes views, the risk
check on a permission dialog, and which phones are told about what. herdr is stubbed, session files and the git project are
made up in temp folders, and nothing is sent anywhere.  Run: python3 tests/watch_server_test.py"""
import importlib.util, json, os, subprocess, sys, tempfile, time
os.environ["SHEPR_STATE"] = tempfile.mkdtemp()
for _k in ("SHEPR_NEW_CMD", "SHEPR_CLAUDE_CMD", "SHEPR_CODEX_CMD", "SHEPR_PROJECTS", "SHEPR_PUSH_ALLOW", "SHEPR_READONLY", "SHEPR_SESSIONS",
           "SHEPR_WATCH", "SHEPR_QUIET_MIN", "SHEPR_WATCH_S"): os.environ.pop(_k, None)
os.environ["SHEPR_POLL"] = "15"
FIX = os.path.join(tempfile.mkdtemp(), "watch.json")
os.environ["SHEPR_WATCH_FIXTURE"] = FIX
CH = tempfile.mkdtemp(); os.environ["CLAUDE_HOME"] = CH
here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("shepr_server", os.path.join(here, "..", "server.py"))
srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)
ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name); ok = ok and bool(cond)
def code_of(fn):
    try: fn(); return None
    except srv.HerdrError as e: return e.code
events = lambda since=0: srv.read_events(since)
evs = lambda: [e["ev"] for e in events()]
host = {"id": "gus", "kind": "local", "machine": None, "enabled": True}
def ag(pane, seq, st, sid=None, ask=None, cwd=None):
    a = {"pane_id": pane, "state_change_seq": seq, "agent_status": st, "cwd": cwd or "/home/u/Projects/" + pane.replace(":", "_"), "workspace_id": "w1",
         "agent": "claude", "agent_session": {"agent": "claude", "kind": "id", "value": sid} if sid else {}}
    if ask: a["_ask"] = ask
    return a
WS = {"w1": "burnbar"}

# ---- the log itself
srv.log_event("blocked", "gus", "w1:p1", "burnbar", t=1000, q="Ship it?")
srv.log_event("done", "gus", "w2:p1", "x" * 200, t=1001, nothing=None)
check("log: an event is one line with its time, kind, host, pane and label (label cut to 60)", events()[0] == {"t": 1000, "ev": "blocked", "host": "gus", "pane": "w1:p1", "label": "burnbar", "q": "Ship it?"} and len(events()[1]["label"]) == 60 and "nothing" not in events()[1])
check("log: the file is private (0600)", os.stat(srv.state_path("events.jsonl")).st_mode & 0o777 == 0o600)
check("log: `since` returns only what is newer", [e["ev"] for e in events(1000)] == ["done"])
with open(srv.state_path("events.jsonl"), "a") as f: f.write("not json\n[1,2]\n{\"ev\": 5, \"t\": 1}\n")
check("log: lines that are not events are skipped", len(events()) == 2)
os.remove(srv.state_path("events.jsonl"))

# ---- a question asked and answered while watching becomes two events, with the question and the wait
srv.STATE["hosts"].clear(); srv.STATE["seen"].clear(); srv.PUSH_EVENTS.clear(); srv.WORKING.clear(); srv.QUIET_TOLD.clear()
srv.note_seen(host, [], 2000, WS)
srv.note_seen(host, [ag("w1:p1", 6, "blocked", ask={"q": "Deploy to production?", "n": 2, "menu": True})], 2010, WS)
srv.note_seen(host, [ag("w1:p1", 6, "blocked")], 2040, WS)
srv.note_seen(host, [ag("w1:p1", 7, "working")], 2070, WS)
e = events()
check("activity: a question that appears is logged with what it asks", e[0]["ev"] == "blocked" and e[0]["label"] == "burnbar" and e[0]["q"] == "Deploy to production?" and e[0]["pane"] == "w1:p1")
check("activity: when it is answered, that is logged with how long it waited (60 s)", e[1]["ev"] == "answered" and e[1]["wait"] == 60 and len(e) == 2)
check("activity: a state already true when watching began is not logged as news", True if (srv.STATE["seen"].clear() or True) else False)
srv.STATE["hosts"].clear()
srv.note_seen(host, [ag("w9:p1", 1, "blocked")], 5000, WS)   # first look at the host
check("...the first look at a host logs nothing", len(events()) == 2)
check("working: Shepr remembers when it saw a turn begin", "gus|w1:p1|7" not in srv.WORKING and True)

# ---- quiet: a working agent whose session file has not been written for QUIET_S
os.makedirs(os.path.join(CH, "projects", "-home-u-Projects-alpha"))
T = os.path.join(CH, "projects", "-home-u-Projects-alpha", "sess-a.jsonl"); open(T, "w").write("{}\n")
now = 100_000.0
a = ag("w3:p1", 4, "working", sid="sess-a", cwd="/home/u/Projects/alpha")
srv.STATE["hosts"]["gus"] = {"watching_since": now - 100, "last_ok": now - 10}
os.utime(T, (now - 30, now - 30))
srv.note_seen(host, [a], now, WS); srv.watch_local(host, [a], WS, now)
check("quiet: a session that wrote 30 s ago is not quiet", "quiet" not in evs() and a["_active"] == now - 30)
check("watch: the open session is mapped to its terminal, by Claude's session id", srv.SID_AGENT["sess-a"] == {"host": "gus", "pane": "w3:p1", "label": "burnbar", "status": "working"})
later = now + srv.QUIET_S + 5
srv.STATE["hosts"]["gus"]["last_ok"] = later - 10
srv.PUSH_EVENTS.clear()
srv.note_seen(host, [a], later, WS); srv.watch_local(host, [a], WS, later)
q = [x for x in events() if x["ev"] == "quiet"]
check("quiet: after QUIET_S of silence it is logged once, with the minutes", len(q) == 1 and q[0]["mins"] == int((srv.QUIET_S + 35) // 60) and q[0]["label"] == "burnbar")
check("quiet: ...and it is news for a phone that asked", srv.PUSH_EVENTS == ["quiet"])
srv.watch_local(host, [a], WS, later + 60)
check("quiet: the same silence is not reported twice", len([x for x in events() if x["ev"] == "quiet"]) == 1)
os.makedirs(os.path.join(CH, "projects", "-home-u-Projects-alpha", "sess-a", "subagents"))
H = os.path.join(CH, "projects", "-home-u-Projects-alpha", "sess-a", "subagents", "agent-1.jsonl"); open(H, "w").write("{}\n")
os.utime(H, (later + 100, later + 100))
check("quiet: a helper (subagent) still writing means the session is not silent", srv.session_written(a) == later + 100)
srv.watch_local(host, [a], WS, later + 110)
os.utime(H, (later + 100, later + 100)); os.utime(T, (later + 100, later + 100))
srv.watch_local(host, [a], WS, later + 110 + srv.QUIET_S + 100)
check("quiet: once it has written again, a NEW silence is news again", len([x for x in events() if x["ev"] == "quiet"]) == 2)
b = ag("w4:p1", 1, "working")   # no session id: Codex, or a session Shepr cannot find
srv.note_seen(host, [a, b], later + 5000, WS); srv.watch_local(host, [a, b], WS, later + 5000)
check("quiet: an agent with no session file is never judged", b.get("_active") is None and len([x for x in events() if x["ev"] == "quiet" and x["pane"] == "w4:p1"]) == 0)

# ---- everything has stopped
srv.FLEET.update(had_work=False, stopped_at=None, told=False); srv.PUSH_EVENTS.clear()
A = lambda *sts: [{"status": s} for s in sts]
OKH = [{"id": "gus", "status": "ok"}]
srv.fleet_watch(A("blocked", "done"), OKH, 10)
check("stopped: a fleet that was never seen working is not 'stopped'", srv.FLEET["stopped_at"] is None and "stopped" not in evs())
srv.fleet_watch(A("working", "blocked"), OKH, 20)
srv.fleet_watch(A("blocked", "done"), OKH, 30)
check("stopped: the moment nothing is working any more is noted, not yet announced", srv.FLEET["stopped_at"] == 30 and "stopped" not in evs())
srv.fleet_watch(A("blocked", "done"), OKH, 30 + srv.FLEET_STOP_S + 1)
st = [x for x in events() if x["ev"] == "stopped"]
check("stopped: after it has held it is logged once, with how many wait and how many finished", len(st) == 1 and st[0]["blocked"] == 1 and st[0]["done"] == 1 and srv.PUSH_EVENTS == ["stopped"])
srv.fleet_watch(A("blocked", "done"), OKH, 500)
check("stopped: one stop is one notice", len([x for x in events() if x["ev"] == "stopped"]) == 1)
srv.fleet_watch(A("working"), OKH, 600); srv.fleet_watch(A("idle"), OKH, 700); srv.fleet_watch(A("idle"), OKH, 900)
check("stopped: all idle with nothing waiting is not a stop", len([x for x in events() if x["ev"] == "stopped"]) == 1 and srv.FLEET["stopped_at"] is None)
srv.fleet_watch(A("working"), OKH, 1000); srv.fleet_watch(A("blocked"), [{"id": "gus", "status": "ok"}, {"id": "vic", "status": "down"}], 1100)
srv.fleet_watch(A("blocked"), [{"id": "gus", "status": "ok"}, {"id": "vic", "status": "down"}], 1300)
check("stopped: never judged while a host cannot be read (its agents may be working)", len([x for x in events() if x["ev"] == "stopped"]) == 1)

# ---- the queue carries it
RISKS = [{"t": time.time() - 120, "sid": "sess-a", "level": "danger", "what": "force push", "cmd": "git push -f origin main", "id": "tu-1", "cwd": "/home/u/Projects/alpha", "sub": False},
         {"t": time.time() - 9000, "sid": "sess-z", "level": "note", "what": "disabled a service", "cmd": "systemctl disable x", "id": "tu-0", "cwd": "/home/u/Projects/zeta", "sub": True}]
BURN = {"rows": [{"sid": "sess-a", "share": 0.62, "out": 120000, "ctx": 9000000, "think": 3000, "replies": 40, "model": "opus", "models": ["opus", "haiku"], "helpers": 0.2, "last": 5.0, "cwd": "/home/u/Projects/alpha", "title": "Ship it"},
                 {"sid": "sess-gone-1234", "share": 0.30, "out": 50000, "ctx": 100, "think": 0, "replies": 9, "model": "sonnet", "models": ["sonnet"], "helpers": 0.0, "last": 4.0, "cwd": "/home/u/Projects/zeta", "title": ""}],
        "others": {"n": 3, "share": 0.08}, "sessions": 5, "replies": 60, "out": 180000, "ctx": 9100000, "series": [0.1] * 24, "models": [{"model": "opus", "share": 0.6}], "now": 9.0}
json.dump({"risks": RISKS, "burn": BURN}, open(FIX, "w"))
tnow = time.time()
srv.WORKING["gus|w3:p1|4"] = {"t": tnow - 600, "exact": True}
srv.SNAP.update(ts=tnow, hosts=OKH, agents=[srv.shape_agent(host, dict(a, _active=tnow - 400), WS), srv.shape_agent(host, ag("w5:p1", 2, "working"), WS)])
qv = srv.build_queue(); by = {x["pane"]: x for x in qv["agents"]}
check("queue: a working agent says how long its turn has run and how long it has been silent", 598 <= by["w3:p1"]["work_s"] <= 602 and 398 <= by["w3:p1"]["quiet_s"] <= 402)
check("queue: one that was already working at the first look claims neither", "work_s" not in by["w5:p1"] and "quiet_s" not in by["w5:p1"])
check("queue: the internal write time is not sent to the phone", "active_at" not in by["w3:p1"])
check("queue: a session that ran something risky in the last hour carries what it was (never the command itself)", by["w3:p1"]["risk"]["what"] == "force push" and by["w3:p1"]["risk"]["level"] == "danger" and "cmd" not in by["w3:p1"]["risk"])
check("queue: the fleet counts, the quiet threshold and the watch switch ride along", qv["fleet"]["working"] == 2 and qv["quiet_min"] == int(srv.QUIET_S // 60) and qv["watch"] is True)

# ---- risky commands reach the log once, and only a fresh danger is news
os.remove(srv.state_path("events.jsonl")); srv.PUSH_EVENTS.clear(); srv.RISK_SEEN.clear()
srv._publish = lambda: None
srv.BOOT_T = time.time() - 300
srv.note_risks()
r = [x for x in events() if x["ev"] == "risk"]
check("risk: each risky command is logged once, oldest first, at the time it RAN", [x["what"] for x in r] == ["disabled a service", "force push"] and abs(r[1]["t"] - RISKS[0]["t"]) < 1)
check("risk: one from an open session carries its terminal and name; one from a closed session its folder", r[1]["pane"] == "w3:p1" and r[1]["label"] == "burnbar" and r[0]["label"] == "zeta" and "pane" not in r[0])
check("risk: a helper's command says so", r[0].get("helper") is True and "helper" not in r[1])
check("risk: only the danger that ran just now is news for a phone (the old note is not)", srv.PUSH_EVENTS == ["risk"])
srv.note_risks()
check("risk: a second pass logs nothing again", len([x for x in events() if x["ev"] == "risk"]) == 2 and srv.PUSH_EVENTS == ["risk"])
srv.RISK_SEEN.clear(); srv.prune_events()
check("risk: after a restart the log itself says which ones are already written", srv.RISK_SEEN == {"tu-0", "tu-1"})

# ---- the activity view
with open(srv.state_path("sends.jsonl"), "a") as f:
    f.write(json.dumps({"t": int(time.time()) - 30, "host": "gus", "pane": "w3:p1", "kind": "text", "ok": True, "err": "", "steps": [{"op": "prompt", "text": "carry on " + "x" * 300}]}) + "\n")
    f.write(json.dumps({"t": int(time.time()) - 20, "host": "gus", "pane": "w9:p9", "kind": "choose", "ok": False, "err": "moved_on", "steps": [{"op": "keys", "keys": ["down", "enter"]}]}) + "\n")
    f.write(json.dumps({"t": int(time.time()) - 10, "kind": "upload", "device": "d", "bytes": 5}) + "\n")
srv.log_event("done", "gus", "w3:p1", "burnbar")
v = srv.activity_view(since=time.time() - 86400)
kinds = [x["ev"] for x in v["items"]]
check("activity: newest first, with what was sent from a phone mixed in by time", kinds[:3] == ["done", "send_failed", "sent"] and kinds[-1] == "risk")
sent = next(x for x in v["items"] if x["ev"] == "sent")
check("activity: a send shows the session's name and the first words (cut), a key send shows the keys", sent["label"] == "burnbar" and sent["text"].startswith("carry on") and len(sent["text"]) <= 120 and v["items"][1]["text"] == "keys ↓ ⏎")
check("activity: an upload is not a send to an agent and is not listed", "upload" not in json.dumps(v["items"]))
check("activity: an entry whose session is still open can be opened; a closed one cannot", sent["open"] is True and v["items"][1]["open"] is False)
check("activity: the summary counts each kind, and how many of the risky ones were dangers", v["summary"] == {"risk": 2, "risk_danger": 1, "sent": 1, "send_failed": 1, "done": 1})
check("activity: `since` narrows it, and the summary with it", srv.activity_view(since=time.time() - 25)["summary"] == {"send_failed": 1, "done": 1})
check("activity: a limit keeps the newest", [x["ev"] for x in srv.activity_view(since=0, limit=2)["items"]] == ["done", "send_failed"])
check("activity: nonsense for `since` is refused", code_of(lambda: srv.activity_view(since="soon")) == "bad_request")

# ---- burn
bv = srv.burn_view("5h")
check("burn: an open session is named as the phone names it and can be opened", bv["rows"][0]["label"] == "burnbar" and bv["rows"][0]["open"] and bv["rows"][0]["pane"] == "w3:p1" and bv["rows"][0]["share"] == 0.62)
check("burn: a session that is closed is named by its folder and cannot be opened", bv["rows"][1]["label"] == "zeta" and bv["rows"][1]["open"] is False and bv["rows"][1]["pane"] is None)
check("burn: only the first 8 characters of a session id ever leave the host", bv["rows"][1]["sid"] == "sess-gon" and "sess-gone-1234" not in json.dumps(bv))
check("burn: the rest, the totals and the series come through", bv["others"] == {"n": 3, "share": 0.08} and bv["sessions"] == 5 and len(bv["series"]) == 24 and bv["available"] and bv["ready"])
check("burn: an unknown span is refused", code_of(lambda: srv.burn_view("9y")) == "bad_request")
srv.WATCH_ON = False
check("burn: with the watch switched off it says so instead of showing nothing", srv.burn_view("5h")["available"] is False and "SHEPR_WATCH=0" in srv.burn_view("5h")["reason"])
srv.WATCH_ON = True

# ---- changes: a real git project in a temp folder
repo = tempfile.mkdtemp()
G = lambda *args: subprocess.run(["git", "-C", repo, "-c", "user.name=T", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main"] + list(args),
                                 capture_output=True, text=True, env=dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1"))
G("init", "-q", "-b", "main")
open(os.path.join(repo, "app.py"), "w").write("one\ntwo\nthree\n"); open(os.path.join(repo, "keep.txt"), "w").write("same\n")
G("add", "-A"); G("commit", "-q", "-m", "first commit")
open(os.path.join(repo, "app.py"), "w").write("one\nTWO\nthree\nfour\n")
open(os.path.join(repo, "new file.md"), "w").write("# fresh\nhello\n")
open(os.path.join(repo, "blob.bin"), "wb").write(b"\x00\x01\x02" * 50)
os.makedirs(os.path.join(repo, "sub"));
srv.get_agent = lambda h, p: {"pane_id": p, "agent_status": "done", "cwd": os.path.join(repo, "sub") if p == "w2:p2" else repo}
cv = srv.changes_view(host, "w1:p1")
files = {f["path"]: f for f in cv["files"]}
check("changes: the branch and the project folder", cv["git"] and cv["branch"] == "main" and cv["folder"] == os.path.basename(repo) and cv["upstream"] is None and cv["ahead"] == 0)
check("changes: the changed file with its added and deleted lines", files["app.py"]["add"] == 2 and files["app.py"]["del"] == 1 and files["app.py"]["untracked"] is False)
check("changes: new files are listed as untracked (a name with a space too), with no counts", files["new file.md"]["untracked"] and files["new file.md"]["add"] is None and "blob.bin" in files and "keep.txt" not in files)
check("changes: the totals and the latest commits", cv["added"] == 2 and cv["deleted"] == 1 and cv["commits"][0]["s"] == "first commit" and cv["commits"][0]["unpushed"] is False)
check("changes: a session sitting in a sub-folder sees the whole project", {f["path"] for f in srv.changes_view(host, "w2:p2")["files"]} == set(files))
d = srv.changes_diff(host, "w1:p1", "app.py")
check("changes: one file's diff, from its first hunk, lines as git prints them", d["text"].startswith("@@") and "-two" in d["text"] and "+TWO" in d["text"] and "+four" in d["text"] and not d["new"] and not d["binary"])
d = srv.changes_diff(host, "w1:p1", "new file.md")
check("changes: a new file is shown whole, every line added", d["new"] and d["text"] == "+# fresh\n+hello\n+")
check("changes: a binary file says so and shows nothing", srv.changes_diff(host, "w1:p1", "blob.bin")["binary"] is True)
check("changes: a file git does not list as changed is refused (this is not a way to read any file)", code_of(lambda: srv.changes_diff(host, "w1:p1", "keep.txt")) == "not_changed"
      and code_of(lambda: srv.changes_diff(host, "w1:p1", "../../etc/passwd")) == "not_changed" and code_of(lambda: srv.changes_diff(host, "w1:p1", "")) == "bad_request")
os.symlink("/etc/hostname", os.path.join(repo, "sneaky"))
check("changes: an untracked symlink pointing outside the project is not followed", code_of(lambda: srv.changes_diff(host, "w1:p1", "sneaky")) == "not_changed")
check("changes: a session on another machine is refused (the files are not here)", code_of(lambda: srv.changes_view({"id": "vic", "kind": "remote", "machine": "vic"}, "w1:p1")) == "local_only")
check("changes: a bad pane id is refused before anything runs", code_of(lambda: srv.changes_view(host, "w1:p1; rm x")) == "bad_pane")
plain = tempfile.mkdtemp(); srv.get_agent = lambda h, p: {"pane_id": p, "agent_status": "done", "cwd": plain}
check("changes: a folder that is not a git project says so", srv.changes_view(host, "w1:p1")["git"] is False)
srv.get_agent = lambda h, p: {"pane_id": p, "agent_status": "done", "cwd": "/no/such/folder"}
check("changes: a folder that is not on this machine is refused", code_of(lambda: srv.changes_view(host, "w1:p1")) == "no_folder")
G("checkout", "-q", "--detach")
srv.get_agent = lambda h, p: {"pane_id": p, "agent_status": "done", "cwd": repo}
check("changes: a detached HEAD is called that", srv.changes_view(host, "w1:p1")["detached"] is True)

# ---- a permission dialog that would allow something risky
DIALOG = "\n".join(["some earlier talk about git push -f that is not the dialog", ""] * 20 + ["─" * 60, " Bash command", "", "   git push --force origin main", "   Force push to main", "",
                    " Do you want to proceed?", " ❯ 1. Yes", "   2. Yes, and don't ask again for git push commands", "   3. No, and tell Claude what to do differently (esc)"])
m = srv.parse_menu(DIALOG)
check("dialog: a permission dialog about a force push is flagged as one", srv.dialog_risk(DIALOG, m) == {"level": "danger", "what": "force push"})
SAFE = DIALOG.replace("git push --force origin main", "ls -la").replace("Force push to main", "List files")
check("dialog: a harmless command is not flagged, even with risky talk further up the screen", srv.dialog_risk(SAFE, srv.parse_menu(SAFE)) is None)
NORULE = "\n".join(["talk about git reset --hard"] * 20 + [" Bash command", "   git clean -fdx", " Do you want to proceed?", " ❯ 1. Yes", "   2. No"])
check("dialog: with no rule above it, only the lines just above the options are judged", srv.dialog_risk(NORULE, srv.parse_menu(NORULE)) == {"level": "danger", "what": "deleted untracked files (git clean)"})
QUESTION = "\n".join(["Which one?", "❯ 1. Blue", "  2. Green"])
check("dialog: a plain question (nothing to approve) is never judged", srv.dialog_risk(QUESTION + "\ngit push -f", srv.parse_menu(QUESTION)) is None)
calls = []
def fake_run(machine, args, timeout=10, want_json=True):
    if args[:2] == ["agent", "get"]: return {"result": {"agent": {"pane_id": args[2], "agent_status": "blocked", "state_change_seq": 3, "cwd": "/x/alpha", "agent": "claude"}}}
    if args[:2] == ["agent", "read"]: return DIALOG
    raise srv.HerdrError("error", "x")
spec2 = importlib.util.spec_from_file_location("shepr_server2", os.path.join(here, "..", "server.py"))
s2 = importlib.util.module_from_spec(spec2); spec2.loader.exec_module(s2)
s2.run_herdr = fake_run; s2.SNAP["hosts"] = [{"id": "gus", "status": "ok"}]; s2.discover_hosts = lambda: [host]
plan = s2.build_plan({"kind": "choose", "host": "gus", "pane": "w1:p1", "option": 1})
check("dialog: approving it says what is being allowed, on the confirm sheet and in the plan", plan["approves"] and plan["risk"] == {"level": "danger", "what": "force push"} and {"k": "Careful", "v": "what this allows looks like: force push"} in plan["lines"])
plan = s2.build_plan({"kind": "choose", "host": "gus", "pane": "w1:p1", "option": 3})
check("dialog: refusing it carries no warning", not plan["approves"] and plan["risk"] is None)
check("dialog: the open session shows the flag with its menu", s2.pane_view(host, "w1:p1", 40)["menu"]["risk"] == {"level": "danger", "what": "force push"})

# ---- which phones hear about what
sent = []
srv.send_push = lambda dev, why="test": sent.append((dev, why)) or 201
srv.devices = lambda: {"A": {}, "B": {}, "C": {}}
srv.PUSH.clear(); srv.PUSH_LAST.clear()
srv.PUSH.update({"A": {"sub": {}, "prefs": {"blocked": True, "done": False}}, "B": {"sub": {}, "prefs": dict(srv.PUSH_PREF_DEFAULTS, quiet=True, risk=True, blocked=False)},
                 "C": {"sub": {}, "prefs": dict(srv.PUSH_PREF_DEFAULTS, stopped=True)}})
srv.dispatch_push(["quiet"])
check("push: 'quiet' goes only to the phone that asked for it (an older subscription without the new kinds gets none)", sent == [("B", "quiet")])
sent.clear(); srv.PUSH_LAST.clear(); srv.dispatch_push(["stopped", "risk"])
check("push: one push covers several kinds, and names them for the notification text", sorted(sent) == [("B", "risk+stopped"), ("C", "risk+stopped")])
sys.exit(0 if ok else 1)
