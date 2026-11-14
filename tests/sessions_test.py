"""Named herdr sessions on this machine (issue #4): every RUNNING named session is one more host called "<this host>/<name>",
routed with `herdr --session <name>`, so its agents reach the queue, the ledger, push and prepare/commit like any host.
herdr is stubbed (no real agent is read); nothing leaves the temp state dir.  Run: python3 tests/sessions_test.py"""
import importlib.util, io, contextlib, json, os, sys, tempfile, types
os.environ["SHEPR_STATE"] = tempfile.mkdtemp()
for _k in ("SHEPR_NEW_CMD", "SHEPR_CLAUDE_CMD", "SHEPR_CODEX_CMD", "SHEPR_PROJECTS", "SHEPR_PUSH_ALLOW", "SHEPR_READONLY",
           "SHEPR_SESSIONS", "SHEPR_LOCAL_ONLY", "HERDR_SOCKET_PATH"): os.environ.pop(_k, None)   # a test pins its own settings, not the shell's
here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("shepr_server", os.path.join(here, "..", "server.py"))
srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)
real_run_herdr, real_bounded = srv.run_herdr, srv.bounded_run   # the real ones, for the routing check; everything else uses a stub
ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name); ok = ok and bool(cond)

HOME = "/home/pi/.config/herdr"
def row(name, running=True, default=False):
    d = HOME if default else f"{HOME}/sessions/{name}"
    return {"default": default, "name": name, "running": running, "session_dir": d, "socket_path": d + "/herdr.sock"}
SESSIONS = [row("default", default=True), row("work"), row("hrtest"), row("quake", running=False)]
calls = []
state = {"sessions": SESSIONS, "machines": [], "fail_sessions": False}
def fake_run(machine, args, timeout=10, want_json=True):
    calls.append((machine, list(args)))
    if args[:2] == ["session", "list"]:
        if state["fail_sessions"]: raise srv.HerdrError("error", "unknown subcommand")
        return {"sessions": state["sessions"]}
    if args[:2] == ["machine", "list"]: return state["machines"]
    raise srv.HerdrError("error", "not stubbed: " + " ".join(args))
srv.run_herdr = fake_run
def fresh():   # forget the cached session list, as the passing of SESSIONS_TTL_S would
    srv._sessions.update(at=0.0, found=[], read=False); calls.clear()
def hosts(): fresh(); return srv.discover_hosts()
ids = lambda hs: [h["id"] for h in hs]
me = srv.LOCAL

# ---- discovery
hs = hosts()
check("the default session stays the one plain host, and each RUNNING named session is one more", ids(hs) == [me, f"{me}/work", f"{me}/hrtest"])
check("a stopped session (quake) is not a host", f"{me}/quake" not in ids(hs))
w = hs[1]
check("a session host is on this machine (kind local) and carries its name", w["kind"] == "local" and w["session"] == "work" and w["enabled"] is True)
check("its machine is a Session, which is still a str with the session's name", isinstance(w["machine"], srv.Session) and w["machine"] == "work")
check("the plain host is untouched: no machine, so no --session", hs[0]["machine"] is None and not hasattr(hs[0]["machine"], "upper"))
check("both lists were really read, so the caller may trust the host list (nothing vanished by accident)", srv._tls.listed is True and srv._tls.sessions_listed is True)

# ---- the duplicate trap: a Shepr started with the socket of a named session (the old workaround)
os.environ["HERDR_SOCKET_PATH"] = f"{HOME}/sessions/work/herdr.sock"
hs = hosts()
check("a Shepr whose plain host already IS session 'work' does not list it again, but now shows the default session", ids(hs) == [me, f"{me}/default", f"{me}/hrtest"])
os.environ["HERDR_SOCKET_PATH"] = f"{HOME}/herdr.sock"
check("HERDR_SOCKET_PATH naming the default session changes nothing", ids(hosts()) == [me, f"{me}/work", f"{me}/hrtest"])
os.environ.pop("HERDR_SOCKET_PATH")

# ---- names are data: only plain names become a --session argument
state["sessions"] = SESSIONS + [row("-evil"), row("two words"), row(""), {"name": 7, "running": True}, "junk", row("a.b_c-1")]
check("a session name that could be read as a flag, or is not a plain name, is skipped", ids(hosts()) == [me, f"{me}/work", f"{me}/hrtest", f"{me}/a.b_c-1"])
state["sessions"] = [dict(row("work"), running=None, status="running")]
check("an older herdr that reports status: running instead of running: true still counts", ids(hosts()) == [me, f"{me}/work"])
state["sessions"] = SESSIONS

# ---- SHEPR_SESSIONS limits it
for val, want, why in (("default", [me], "only the plain session"), ("default,work", [me, f"{me}/work"], "a named list adds only those"),
                       ("work", [me, f"{me}/work"], "a name without default still keeps the plain host"), ("all", [me, f"{me}/work", f"{me}/hrtest"], "'all' is the same as unset"),
                       ("nope", [me], "a name that is not running adds nothing")):
    os.environ["SHEPR_SESSIONS"] = val
    check(f"SHEPR_SESSIONS={val}: {why}", ids(hosts()) == want)
os.environ["SHEPR_SESSIONS"] = "default"
fresh(); srv.discover_hosts()
check("SHEPR_SESSIONS=default does not even ask herdr for the session list", not [c for c in calls if c[1][:2] == ["session", "list"]])
os.environ.pop("SHEPR_SESSIONS")

# ---- failures change nothing but the host list, and are never taken for "the sessions are gone"
state["fail_sessions"] = True
hs = hosts()
check("an older herdr without `session list` leaves just the plain host", ids(hs) == [me])
check("...and that unread session list is not trusted to say a session is gone, while the machine list still is (a herdr with no `session list` must not stop a vanished MACHINE being forgotten)",
      srv._tls.listed is True and srv._tls.sessions_listed is False)
state["fail_sessions"] = False
state["machines"] = [{"label": "vic", "enabled": True}, {"label": f"{me}/work", "enabled": True}]
hs = hosts()
check("a saved machine that happens to be called gus/work keeps the name; the session is not added on top", ids(hs) == [me, "vic", f"{me}/work", f"{me}/hrtest"] and hs[2]["kind"] == "remote")
state["machines"] = []
os.environ["SHEPR_LOCAL_ONLY"] = "1"
check("SHEPR_LOCAL_ONLY (tests) still means this host alone, with no session read", ids(hosts()) == [me] and not [c for c in calls if c[1][:2] == ["session", "list"]])
os.environ.pop("SHEPR_LOCAL_ONLY")

# ---- the list is cached, because discover_hosts runs on every request
fresh(); srv.discover_hosts(); srv.discover_hosts(); srv.discover_hosts()
check("three discoveries inside the cache window ask herdr for the session list once", len([c for c in calls if c[1][:2] == ["session", "list"]]) == 1)

# ---- routing: the command line each kind of host produces
seen_cmd = []
def fake_bounded(cmd, timeout): seen_cmd.append(cmd); return 0, "{}", ""
srv.bounded_run = fake_bounded
rh = real_run_herdr
rh(None, ["agent", "list"]); rh("vic", ["agent", "list"]); rh(srv.Session("work"), ["agent", "list"])
H = srv.HERDR
check("the plain host sends no routing flag", seen_cmd[0] == [H, "agent", "list"])
check("a saved machine still goes through --machine", seen_cmd[1] == [H, "--machine", "vic", "agent", "list"])
check("a session goes through --session NAME, in front of the subcommand", seen_cmd[2] == [H, "--session", "work", "agent", "list"])
srv.bounded_run = real_bounded

# ---- a whole poll: two sessions that both have a pane called w1:p1 stay two different agents
def agent(pane, st, seq): return {"pane_id": pane, "agent_status": st, "state_change_seq": seq, "workspace_id": "w1", "agent": "claude", "cwd": "/home/pi/Projects/" + pane.replace(":", "_"),
                                  "terminal_title_stripped": "t", "agent_session": {}}
per = {None: [agent("w1:p1", "blocked", 3)], "work": [agent("w1:p1", "blocked", 3), agent("w1:p2", "working", 1)], "hrtest": [], "vic": [agent("w1:p1", "blocked", 8)]}
def fake_run2(machine, args, timeout=10, want_json=True):
    key = str(machine) if isinstance(machine, srv.Session) else machine
    if args[:2] == ["session", "list"]: return {"sessions": SESSIONS}
    if args[:2] == ["session", "list"] and state["fail_sessions"]: raise srv.HerdrError("error", "unknown subcommand")
    if args[:2] == ["machine", "list"]: return state["machines"]
    if args[:2] == ["agent", "list"]: return {"result": {"agents": per[key]}}
    if args[:2] == ["workspace", "list"]: return {"result": {"workspaces": [{"workspace_id": "w1", "label": "ws-" + (key or "default"), "agent_status": "unknown"}]}}
    if args[:2] == ["agent", "get"]: return {"result": {"agent": next(a for a in per[key] if a["pane_id"] == args[2])}}
    if args[:2] == ["agent", "read"]: return "" if not want_json else {"result": {}}
    raise srv.HerdrError("error", "not stubbed: " + " ".join(args))
srv.run_herdr = fake_run2
state["machines"] = []
fresh(); srv.STATE["hosts"].clear(); srv.STATE["seen"].clear(); srv._absent.clear(); srv.HOST_SNAP.clear(); srv._inflight.clear()
srv.poll_all(wait=10)
q = srv.build_queue()
by = {(a["host"], a["pane"]): a for a in q["agents"]}
check("the queue holds the default session's agent and both of 'work's, each under its own host", set(by) == {(me, "w1:p1"), (f"{me}/work", "w1:p1"), (f"{me}/work", "w1:p2")})
check("a session's agents are named after that session's own workspaces", by[(f"{me}/work", "w1:p1")]["workspace"] == "ws-work" and by[(me, "w1:p1")]["workspace"] == "ws-default")
check("the host list shows the session next to the machine, all ok", [h["id"] for h in q["hosts"]] == [me, f"{me}/work", f"{me}/hrtest"] and all(h["status"] == "ok" for h in q["hosts"]))
seen = srv.STATE["seen"]
check("the 'blocked for' ledger keeps the same pane id in two sessions apart", f"{me}|w1:p1|3|blocked" in seen and f"{me}/work|w1:p1|3|blocked" in seen)
h = srv.need_host(f"{me}/work")
check("need_host finds a session host, and what is sent to it goes to that session", isinstance(h["machine"], srv.Session))
got = []
srv.run_herdr = lambda machine, args, **k: (got.append((machine, args)), fake_run2(machine, args, **k))[1]
srv.get_agent(h, "w1:p2")
check("reading a pane of a session host reaches that session's herdr", got and got[0][0] == "work" and isinstance(got[0][0], srv.Session))

# ---- the two lists vouch for different hosts
state["machines"] = [{"label": "vic", "enabled": True}]
fresh(); srv.poll_all(wait=10)
check("a saved machine and a session are both watched, each with its own ledger rows", srv.STATE["seen"].keys() >= {f"vic|w1:p1|8|blocked", f"{me}/work|w1:p1|3|blocked"})
state["machines"] = []; state["fail_sessions"] = True
for _ in range(3): fresh(); srv.poll_all(wait=10)
check("with the session list unreadable, a session's ledger is kept however many polls pass (it is not known to be gone)",
      f"{me}/work" in srv.STATE["hosts"] and any(k.startswith(f"{me}/work|") for k in srv.STATE["seen"]))
check("...while a saved machine that left herdr's own list is still forgotten (the machine list was read)",
      "vic" not in srv.STATE["hosts"] and not [k for k in srv.STATE["seen"] if k.startswith("vic|")])
state["fail_sessions"] = False

# ---- a session that stops drops out; its ledger goes after two real lists
state["sessions"] = [row("default", default=True)]
SESSIONS[:] = state["sessions"]
per_stop = srv.STATE["seen"]
fresh(); srv.poll_all(wait=10); fresh(); srv.poll_all(wait=10)
check("a session that stopped is gone from the host list", [h["id"] for h in srv.build_queue()["hosts"]] == [me])
check("...and so are its ledger rows, after two real lists in a row", not [k for k in srv.STATE["seen"] if k.startswith(f"{me}/work|")] and any(k.startswith(me + "|") for k in srv.STATE["seen"]))

# ---- `shepr status` (the bar badge) counts the other sessions too
SESSIONS[:] = [row("default", default=True), row("work")]
per["work"] = [agent("w1:p1", "blocked", 4), agent("w1:p2", "working", 1)]
fresh(); srv.run_herdr = fake_run2
args = types.SimpleNamespace(json=True)
buf = io.StringIO()
with contextlib.redirect_stdout(buf): srv.cli_status(args)
st = json.loads(buf.getvalue())
check("status counts the default session and 'work' together: 2 blocked, 1 working", st["counts"] == {"blocked": 2, "working": 1})
check("a waiting agent from a named session says which session, the plain one does not", [w.get("session") for w in st["waiting"]] == [None, "work"])
os.environ["SHEPR_SESSIONS"] = "default"
fresh(); buf = io.StringIO()
with contextlib.redirect_stdout(buf): srv.cli_status(args)
check("SHEPR_SESSIONS=default keeps status to the default session as before", json.loads(buf.getvalue())["counts"] == {"blocked": 1})

sys.exit(0 if ok else 1)
