"""The waiting ledger: only waits the host WATCHED from first moment to last are recorded, and today / the last 7 days add up.
Pure Python.  Run: python3 tests/ledger_test.py"""
import importlib.util, json, os, sys, tempfile, time
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
host = {"id": "gus", "kind": "local", "machine": None, "enabled": True}
ag = lambda pane, seq, st, ws="w1": {"pane_id": pane, "state_change_seq": seq, "agent_status": st, "cwd": "/home/pi/Projects/" + pane.replace(":", "_"), "workspace_id": ws, "agent_session": {}}
waits = lambda: srv.read_waits(0)
srv.STATE["hosts"].clear(); srv.STATE["seen"].clear(); srv.PUSH_EVENTS.clear()
WS = {"w1": "burnbar"}

# a wait already true when the host started, then ending: NOT recorded (we never saw it begin)
srv.note_seen(host, [ag("w1:p1", 5, "blocked")], 1000, WS)
srv.note_seen(host, [ag("w1:p1", 5, "blocked")], 1010, WS)
srv.note_seen(host, [], 1020, WS)
check("a wait that was already going when the host started is not counted", waits() == [])
# a wait watched from first moment to last
srv.note_seen(host, [], 2000, WS)
srv.note_seen(host, [ag("w1:p1", 6, "blocked")], 2010, WS)
srv.note_seen(host, [ag("w1:p1", 6, "blocked")], 2040, WS)
srv.note_seen(host, [], 2070, WS)
w = waits()
check("a watched wait is recorded once, named by its workspace, with the right length (60 s)", len(w) == 1 and w[0]["label"] == "burnbar" and w[0]["t1"] - w[0]["t0"] == 60 and w[0]["host"] == "gus")
# finished states are not waiting
srv.note_seen(host, [ag("w2:p1", 1, "done", "w2")], 2100, WS); srv.note_seen(host, [], 2130, WS)
check("a finished agent is not a wait", len(waits()) == 1)
# the host was away between two polls: we cannot know when the wait ended
srv.note_seen(host, [ag("w3:p1", 1, "blocked", "w3")], 3000, WS)
srv.note_seen(host, [], 3300, WS)            # 300 s later: far beyond 3 polls
check("a wait that ended while the host was not watching is not counted", len(waits()) == 1)
# a multi-step dialog is ONE blocked state (same seq): one wait, not one per step
srv.note_seen(host, [], 4000, WS)
for t in (4010, 4040, 4070): srv.note_seen(host, [ag("w4:p1", 9, "blocked", "w4")], t, WS)
srv.note_seen(host, [], 4100, WS)
check("a multi-step dialog (one blocked state) is one wait", len(waits()) == 2 and waits()[-1]["t1"] - waits()[-1]["t0"] == 90)
check("the ledger file is private (0600)", os.stat(srv.state_path("waits.jsonl")).st_mode & 0o777 == 0o600)
srv.record_wait("gus", "x", 100, 100); srv.record_wait("gus", "x", 0, 100 + 30 * 3600)
check("a zero-length wait and a wait over 24 hours are ignored", len(waits()) == 2)

# aggregation over local calendar days
os.remove(srv.state_path("waits.jsonl"))
now = time.mktime((2026, 10, 10, 15, 0, 0, 0, 0, -1))
mid = time.mktime((2026, 10, 10, 0, 0, 0, 0, 0, -1))
def add(days_ago, hour, minutes, label):
    t1 = mid - days_ago * 86400 + hour * 3600
    srv.record_wait("gus", label, t1 - minutes * 60, t1)
add(0, 9, 38, "burnbar"); add(0, 11, 5, "clarity"); add(0, 13, 12, "blip")      # today: 38 + 5 + 12
add(1, 10, 20, "burnbar"); add(1, 16, 7, "clarity")                              # yesterday
add(3, 12, 45, "blip")                                                           # three days ago
add(10, 12, 99, "ancient")                                                       # outside the 7 days
v = srv.ledger_view(now, agents=[{"status": "blocked", "for_s": 600}, {"status": "blocked", "for_s": 120}, {"status": "working"}])
check("today: 3 waits, 55 minutes in all, the longest was burnbar for 38 minutes", v["today"]["n"] == 3 and v["today"]["secs"] == 55 * 60 and v["today"]["longest"] == {"label": "burnbar", "secs": 38 * 60})
check("today: the top list is ordered longest first", [t["label"] for t in v["today"]["top"]] == ["burnbar", "blip", "clarity"])
check("the last 7 days: seven entries, oldest first, ending today, each with its own count and total",
      len(v["days"]) == 7 and v["days"][-1]["d"] == "2026-10-10" and v["days"][-1]["n"] == 3 and v["days"][-2]["n"] == 2 and v["days"][-2]["secs"] == 27 * 60 and v["days"][-4]["n"] == 1 and v["days"][-4]["secs"] == 45 * 60 and sum(d["n"] for d in v["days"]) == 6)
check("a wait from 10 days ago is not in the week", all(t["label"] != "ancient" for t in v["today"]["top"]) and sum(d["n"] for d in v["days"]) == 6)
check("waiting right now: how many agents are blocked and for how long in all", v["now"] == {"n": 2, "secs": 720})
# pruning
# pruning works on the real clock (the fixture above is pinned to 2026-10-10), so these two are placed relative to now
real = time.time()
srv.record_wait("gus", "old", real - 40 * 86400 - 600, real - 40 * 86400)
srv.record_wait("gus", "recent", real - 3 * 86400 - 600, real - 3 * 86400)
srv.prune_waits()
check("waits older than 30 days are dropped when the host starts, newer ones stay",
      all(w["label"] != "old" for w in srv.read_waits(0)) and any(w["label"] == "recent" for w in srv.read_waits(0)))

# machines that come and go (a throwaway container): their ledger rows must not outlive them, or a recycled name inherits old "blocked for" times
srv.STATE["hosts"].clear(); srv.STATE["seen"].clear(); srv._absent.clear()
mk = lambda hid: {"id": hid, "kind": "remote", "machine": hid, "enabled": True}
here_h, job7, job70 = host, mk("job-demo-7"), mk("job-demo-70")
for h_, t_ in ((here_h, 100), (job7, 100), (job70, 100)):
    srv.note_seen(h_, [ag("w1:p1", 3, "done")], t_, WS)
keys = lambda hid: [k for k in srv.STATE["seen"] if k.startswith(hid + "|")]
check("setup: three hosts each hold a ledger row", all(len(keys(h_["id"])) == 1 for h_ in (here_h, job7, job70)))
srv.forget_gone_hosts([here_h, job70], False)
srv.forget_gone_hosts([here_h, job70], False)
check("a machine list that could not be read drops nothing, however often it fails", len(keys("job-demo-7")) == 1 and "job-demo-7" in srv.STATE["hosts"])
srv.forget_gone_hosts([here_h, job70], True)
check("one real list without a machine is not enough", len(keys("job-demo-7")) == 1 and "job-demo-7" in srv.STATE["hosts"])
srv.forget_gone_hosts([here_h, job7, job70], True)
srv.forget_gone_hosts([here_h, job70], True)
check("a machine that came back resets the count: one more absence still keeps it", len(keys("job-demo-7")) == 1)
srv.forget_gone_hosts([here_h, job70], True)
check("two real lists in a row without it drop its host entry and its ledger row",
      "job-demo-7" not in srv.STATE["hosts"] and keys("job-demo-7") == [])
check("the other hosts keep theirs, including a name that only starts the same (job-demo-70 vs job-demo-7)",
      len(keys("job-demo-70")) == 1 and len(keys("gus")) == 1 and "job-demo-70" in srv.STATE["hosts"])
_disk = json.load(open(srv.state_path("seen.json")))
check("the drop was written to disk (host entry and ledger row both gone from seen.json)",
      "job-demo-7" not in _disk["hosts"] and not any(k.startswith("job-demo-7|") for k in _disk["seen"]) and "job-demo-70" in _disk["hosts"])
srv.note_seen(job7, [ag("w1:p1", 3, "done")], 5000, WS)
check("a new machine that takes the same name starts with a clean ledger (first look, not the old time)",
      srv.STATE["seen"]["job-demo-7|w1:p1|3|done"]["t"] == 5000)

# the same, end to end through poll_all: herdr's own saved-machine list decides
srv.STATE["hosts"].clear(); srv.STATE["seen"].clear(); srv._absent.clear(); srv.HOST_SNAP.clear(); srv._inflight.clear()
machines, mode = [{"label": "job-a-700", "enabled": True}, {"label": "vic", "enabled": True}], {"fail": False}
def fake_herdr(machine, args, timeout=10, want_json=True):
    if args[:2] == ["machine", "list"]:
        if mode["fail"]: raise srv.HerdrError("timeout", "herdr took longer than 6s")
        return list(machines)
    if args[:2] == ["agent", "list"]:
        return {"result": {"agents": [{"pane_id": "w1:p1", "agent_status": "done", "state_change_seq": 4, "cwd": "/work/a", "workspace_id": "w1", "agent_session": {}}]}}
    return {"result": {"workspaces": [{"workspace_id": "w1", "label": "a"}]}}
real_herdr, real_discover = srv.run_herdr, srv.discover_hosts
srv.run_herdr = fake_herdr
srv.poll_all(wait=10)
check("poll_all: every listed machine is watched and holds a ledger row", {"job-a-700", "vic"} <= set(srv.STATE["hosts"]) and keys("job-a-700") and keys("vic"))
mode["fail"] = True
for _ in range(3): srv.poll_all(wait=10)
check("poll_all: herdr's list failing three times drops no machine's ledger", "job-a-700" in srv.STATE["hosts"] and keys("job-a-700") and keys("vic"))
mode["fail"] = False; machines.pop(0)
srv.poll_all(wait=10)
check("poll_all: one real list without the container keeps it", "job-a-700" in srv.STATE["hosts"])
srv.poll_all(wait=10)
check("poll_all: the second real list without it drops its ledger and leaves vic's alone",
      "job-a-700" not in srv.STATE["hosts"] and keys("job-a-700") == [] and "vic" in srv.STATE["hosts"] and keys("vic"))
srv.STATE["hosts"]["ghost"] = {"watching_since": 1, "last_ok": 1}; srv.STATE["seen"]["ghost|w1:p1|1|done"] = {"t": 1, "exact": False, "basis": "first-look", "label": "g"}
srv.discover_hosts = lambda: [{"id": srv.LOCAL, "machine": None, "kind": "local", "enabled": True}]   # what server_hardening_test does
for _ in range(4): srv.poll_all(wait=10)
check("poll_all: a replaced discover_hosts is never taken for a real machine list", "ghost" in srv.STATE["hosts"] and keys("ghost"))
srv.run_herdr, srv.discover_hosts = real_herdr, real_discover
sys.exit(0 if ok else 1)
