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
sys.exit(0 if ok else 1)
