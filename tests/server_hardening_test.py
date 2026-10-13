"""Regression tests for the 2026-10-09 bug hunt (Codex, 19 findings). Pure Python, nothing talks to herdr, the
network or the real state directory.  Run: python3 tests/server_hardening_test.py"""
import hashlib, importlib.util, json, os, shutil, subprocess, sys, tempfile, threading, time
here = os.path.dirname(os.path.abspath(__file__))
os.environ["SHEPR_STATE"] = tempfile.mkdtemp()
for _k in ("SHEPR_NEW_CMD", "SHEPR_CLAUDE_CMD", "SHEPR_CODEX_CMD", "SHEPR_PROJECTS", "SHEPR_PUSH_ALLOW", "SHEPR_READONLY"): os.environ.pop(_k, None)   # a test pins its own settings, not the shell's
os.environ["SHEPR_POLL"] = "15"
spec = importlib.util.spec_from_file_location("shepr_server", os.path.join(here, "..", "server.py"))
srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)
REAL_RUN_STEP = srv.run_step

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name); ok = ok and bool(cond)

def pub(k):
    x, y = srv._mul(k, srv._G)
    return {"kty": "EC", "crv": "P-256", "x": srv.b64u(x.to_bytes(32, "big")), "y": srv.b64u(y.to_bytes(32, "big"))}

# ---- one pairing code, two phones racing: exactly one wins
code = "ABCD-EFGH-JKLM"
srv.write_pairing({"hash": hashlib.sha256(srv.norm_code(code).encode()).hexdigest(), "expires": time.time() + 60, "used_by": None})
res = []
def redeem(k):
    try: res.append(("ok", srv.do_pair({"code": code, "jwk": pub(k), "name": f"p{k}"}, "t")))
    except srv.HerdrError as e: res.append(("err", e.code))
ts = [threading.Thread(target=redeem, args=(k,)) for k in (7, 9, 11, 13)]
[t.start() for t in ts]; [t.join() for t in ts]
check("pairing: four phones, one code, exactly one succeeds", sum(1 for r in res if r[0] == "ok") == 1)
check("pairing: exactly one device registered", len(json.load(open(srv.state_path("devices.json")))) == 1)

# ---- a revoke done by another process is never undone by this one's stale copy
did = next(iter(srv.devices()))
srv.devices()[did]["last_seen"] = 0
with open(srv.state_path("devices.json"), "w") as f: json.dump({}, f)       # `shepr revoke` from another process
def touch(devs):
    if did not in devs: raise srv.HerdrError("unknown_device", "revoked")
    devs[did]["last_seen"] = 1
try: srv.mutate_devices(touch); raised = False
except srv.HerdrError as e: raised = e.code == "unknown_device"
check("revoke: the touch sees the revocation and refuses", raised)
check("revoke: the device file still has no devices", json.load(open(srv.state_path("devices.json"))) == {})

# ---- two previews of the same blocked dialog: only the first commit is dispatched
sent = []
srv.need_host = lambda h: {"id": "gus", "machine": None, "kind": "local", "enabled": True}
srv.get_agent = lambda host, pane: {"agent_status": "blocked", "state_change_seq": 7, "agent_session": {"value": "s1"}}
srv.run_step = lambda host, pane, step: (time.sleep(0.2), sent.append(step["keys"]))
def mkplan(n):
    p = {"nonce": f"n{n}", "host": "gus", "pane": "w1:p1", "kind": "keys", "steps": [{"op": "keys", "keys": ["enter"]}],
         "snap": {"status": "blocked", "seq": 7, "session": "s1"}, "expires": time.time() + 60, "made": time.time()}
    srv.PLANS[p["nonce"]] = p
for n in (1, 2): mkplan(n)
out = []
def commit(n):
    try: out.append(srv.commit_plan(f"n{n}")["ok"])
    except srv.HerdrError as e: out.append(e.code)
ts = [threading.Thread(target=commit, args=(n,)) for n in (1, 2)]
[t.start() for t in ts]; [t.join() for t in ts]
check("commit: two previews of one dialog dispatch exactly once", len(sent) == 1)
check("commit: the other one is refused as moved_on", sorted(map(str, out)) == ["True", "moved_on"])

# ---- a multi-step dialog is ONE blocked state: ticks, then Review, then Submit all share herdr's counter (seq 7).
# A preview made AFTER the previous answer must work; only a preview made BEFORE it is dead. (Regression: Fred's phone could
# not press "Submit answers" on the Review screen because the whole state counted as "already answered".)
sent.clear()
time.sleep(0.05); mkplan(3)                      # made after the answer above, same seq 7
out3 = []
try: out3.append(srv.commit_plan("n3")["ok"])
except srv.HerdrError as e: out3.append(e.code)
check("multi-step: a fresh preview after an answer is accepted although the state counter has not moved", out3 == [True] and len(sent) == 1)
time.sleep(0.05); mkplan(4); mkplan(5)           # two phones look at the Review screen together
out45 = []
def c45(n):
    try: out45.append(srv.commit_plan(f"n{n}")["ok"])
    except srv.HerdrError as e: out45.append(e.code)
ts = [threading.Thread(target=c45, args=(n,)) for n in (4, 5)]
[t.start() for t in ts]; [t.join() for t in ts]
check("multi-step: two phones on the same Review screen still answer it only once", sorted(map(str, out45)) == ["True", "moved_on"])

# ---- a runaway child is cut off at the byte cap
srv.MAX_OUT = 1 << 20
try: srv.bounded_run([sys.executable, "-c", "print('x' * 20_000_000)"], 20); cut = False
except srv.HerdrError as e: cut = e.code == "too_big"
check("subprocess output: a 20 MB answer is cut off at the cap", cut)

# ---- one host's malformed answer does not break the poll
srv.run_herdr = lambda machine, args, timeout=10, want_json=True: {"result": {"agents": None}}
r = srv.poll_host({"id": "h", "machine": "h", "kind": "remote", "enabled": True})
check("poll: a host answering agents=null becomes that host's error", r[1] is None and "bad_response" in r[3])
srv.run_herdr = lambda machine, args, timeout=10, want_json=True: (_ for _ in ()).throw(ValueError("boom"))
r = srv.poll_host({"id": "h", "machine": "h", "kind": "remote", "enabled": True})
check("poll: any exception becomes that host's error", r[1] is None and "bad_response" in r[3])

# ---- static files: launch images are served, traversal is not
check("static: iOS launch image is allowed", bool(srv.STATIC_OK.match("/icons/launch/1170x2532.png")))
check("static: traversal is not", not srv.STATIC_OK.match("/icons/launch/../../server.py") and not srv.STATIC_OK.match("/icons/../server.py"))

# ---- multi-select preview discloses a checked row the phone cannot change
SCREEN = """Pick any

❯ 1. [ ] Alpha
  2. [✔] Bravo
  3. [ ] Charlie
  4. [✔] Type something
     Submit
─────────
  5. Chat about this
"""
srv.read_tail = lambda host, pane, lines: SCREEN
srv.READONLY = False
plan = srv.build_plan({"host": "gus", "pane": "w1:p1", "kind": "multi", "picks": [], "advance": True})
kept = next(l["v"] for l in plan["lines"] if l["k"] == "Leaves ticked")
check("multi: a checked 'Type something' row is disclosed, not hidden", "Type something" in kept and "cannot be changed" in kept)

# ---- multi-select: "This approves it" follows what the send leaves ticked, not only what it changes
YES_ON = "Allow these?\n\n❯ 1. [✔] Yes, allow it\n  2. [ ] No\n  3. [ ] Maybe later\n     Submit\n─────────\n  4. Chat about this\n"
YES_OFF = YES_ON.replace("[✔] Yes", "[ ] Yes")
def _multi_approves(screen, picks, advance=True):
    srv.read_tail = lambda host, pane, lines: screen
    return srv.build_plan({"host": "gus", "pane": "w1:p1", "kind": "multi", "picks": picks, "advance": advance})["approves"]
check("multi approves: ticking Yes warns", _multi_approves(YES_OFF, [1]) is True)
check("multi approves: submitting with Yes already ticked and kept still warns", _multi_approves(YES_ON, [1]) is True)
check("multi approves: submitting with Yes already ticked while ticking another box still warns", _multi_approves(YES_ON, [1, 3]) is True)
check("multi approves: unticking Yes does not warn", _multi_approves(YES_ON, [3]) is False and _multi_approves(YES_ON, []) is False)
check("multi approves: a tick that is not sent (advance off) keeps the old rule, Yes already ticked and left alone does not warn", _multi_approves(YES_ON, [1, 3], advance=False) is False)
check("multi approves: ticking a non-approving box on a screen with no approval does not warn", _multi_approves(YES_OFF, [2]) is False)

# ---- uploads from the phone: what a file really is, where it is stored, and how it is cleaned up
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 50
check("upload: a PNG, JPEG, GIF, WEBP and PDF are recognised by their first bytes, whatever the phone calls them",
      [srv.sniff_upload(b, "exe") for b in (PNG, b"\xff\xd8\xff\xe0" + b"x" * 9, b"GIF89a" + b"x" * 9, b"RIFF\0\0\0\0WEBPx", b"%PDF-1.7 x")] == ["png", "jpg", "gif", "webp", "pdf"])
check("upload: an executable, a script and unknown bytes are refused", all(srv.sniff_upload(b, e) is None for b, e in ((b"MZ\x90\x00" + b"x" * 20, "exe"), (b"#!/bin/sh\nrm -rf x", "sh"), (b"\x7fELF" + b"x" * 9, "bin"))))
check("upload: plain text is accepted only with a text extension, and not with a NUL or bad UTF-8",
      srv.sniff_upload(b"hello", "txt") == "txt" and srv.sniff_upload(b"hello", "sh") is None and srv.sniff_upload(b"a\x00b", "txt") is None and srv.sniff_upload(b"\xff\xfe\xfa", "txt") is None)
r = srv.save_upload(PNG, "png", "dev1")
st = os.stat(r["path"]); dst = os.stat(os.path.dirname(r["path"]))
check("upload: stored under the state dir, mode 0600 in a 0700 folder, with a random name", r["path"].startswith(srv.upload_dir()) and st.st_mode & 0o777 == 0o600 and dst.st_mode & 0o777 == 0o700 and r["kind"] == "png" and open(r["path"], "rb").read() == PNG)
for bad, why in ((b"", "empty"), (b"x" * (srv.UPLOAD_MAX + 1), "too big")):
    try: srv.save_upload(bad, "txt", "dev1"); refused = False
    except srv.HerdrError as e: refused = e.code in ("too_big", "bad_type")
    check(f"upload: a {why} file is refused", refused)
os.utime(r["path"], (time.time() - 8 * 86400,) * 2)
r2 = srv.save_upload(PNG, "png", "dev1")
check("upload: files older than a week are deleted on the next upload", not os.path.exists(r["path"]) and os.path.exists(r2["path"]))
srv.UPLOAD_DIR_CAP = 120
paths = [srv.save_upload(PNG + bytes([i]), "png", "dev1")["path"] for i in range(5)]
check("upload: the folder is kept under its size cap by dropping the oldest first", sum(os.path.getsize(p) for p in paths if os.path.exists(p)) <= 120 and os.path.exists(paths[-1]))

# ---- Start new: a first message is DATA for the shell, quoted as one word
import subprocess
nasty = ["it's \"quoted\" $HOME `whoami` ; touch /tmp/shepr-pwned #x", "$(id)", "a\\b", "-n", "'", "~ && echo hacked", "é ü 日本"]
check("first message: quoted, every nasty string reaches the program as exactly ONE argument, unchanged",
      all(subprocess.run(["bash", "-c", "printf '%s' " + srv.shlex.quote(m)], capture_output=True, text=True).stdout == m for m in nasty) and not os.path.exists("/tmp/shepr-pwned"))
srv.need_host = lambda h: {"id": "gus", "machine": None, "kind": "local", "enabled": True}
srv.run_herdr = lambda machine, args, timeout=10, want_json=True: {"result": {"workspaces": []}}
p1 = srv.plan_new_project({"host": "gus", "name": "N", "dir": "d", "message": "it's fine"})
p2 = srv.plan_new_project({"host": "gus", "name": "N", "dir": "d"})
c1 = next(l["v"] for l in p1["lines"] if l["k"] == "Then types"); c2 = next(l["v"] for l in p2["lines"] if l["k"] == "Then types")
check("first message: it is appended to the command, quoted; with none, the command is unchanged", c1.endswith("claude 'it'\"'\"'s fine'") and c2.endswith("&& claude") and any(l["k"] == "First message" for l in p1["lines"]) and not any(l["k"] == "First message" for l in p2["lines"]))
for bad in ("two\nlines", "x" * 501, "bell\x07", 5):
    try: srv.plan_new_project({"host": "gus", "name": "N", "dir": "d", "message": bad}); refused = False
    except srv.HerdrError as e: refused = e.code == "bad_message"
    if not refused: check(f"first message {bad!r:.20} refused", False)
check("first message: newline, control character, non-string and over 500 characters are refused", True)

# ---- "Type something": your own words. Enter with nothing typed DECLINES the question, so it needs text, and the cursor is
# checked before a single character is typed.
captured = []
real_run_herdr = srv.run_herdr
srv.run_herdr = lambda machine, args, timeout=10, want_json=True: captured.append(list(args)) or ""
REAL_RUN_STEP({"machine": None}, "w1:p1", {"op": "text", "text": "-n hello -- world"})
check("typing into a dialog sends the text WITHOUT a '--' (herdr would type the dashes), even if the text starts with a dash", captured[-1] == ["pane", "send-text", "w1:p1", "-n hello -- world"])
srv.run_herdr = real_run_herdr
SINGLE = """Pick one

%s 1. Alpha
     Choose Alpha.
%s 2. Bravo
     Choose Bravo.
%s 3. Type something.
─────────────────────────────
  4. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel"""
on = lambda row: SINGLE % tuple("❯" if i == row else " " for i in (1, 2, 3))
srv.READONLY = False
srv.need_host = lambda h: {"id": "gus", "machine": None, "kind": "local", "enabled": True}
srv.get_agent = lambda host, pane: {"agent_status": "blocked", "state_change_seq": 7, "agent_session": {"value": "s1"}}
srv.read_tail = lambda host, pane, lines, max_bytes=None: on(1)
def refused(body):
    try: srv.build_plan({"host": "gus", "pane": "w1:p1", **body}); return None
    except srv.HerdrError as e: return e.code
check("type-something: choosing it with no words is refused (the plain pick would decline the question)", refused({"kind": "choose", "option": 3}) == "needs_text" and refused({"kind": "choose", "option": 3, "text": "   "}) == "needs_text")
check("type-something: words with a newline, a tab, a control character, or over 1000 characters are refused",
      all(refused({"kind": "choose", "option": 3, "text": x}) == "bad_text" for x in ("a\nb", "a\tb", "a\x07b", "x" * 1001, 5)))
check("type-something: a normal option does not take words", refused({"kind": "choose", "option": 1, "text": "hi"}) == "bad_text")
p = srv.build_plan({"host": "gus", "pane": "w1:p1", "kind": "choose", "option": 3, "text": "What about 42, it's \"ok\" $HOME?"})
st = srv.PLANS[p["nonce"]]["steps"]
check("type-something: the plan is arrow down twice, type the words, then Enter, and says so",
      [s["op"] for s in st] == ["keys", "text", "keys"] and st[0]["keys"] == ["down", "down"] and st[1]["text"].startswith("What about 42") and st[2]["keys"] == ["enter"]
      and any(l["k"] == "Types" for l in p["lines"]) and srv.PLANS[p["nonce"]]["snap"]["text_at"] == 2)
# commit: the screen is read before the words go in
seq = {"screens": []}
sent = []
srv.run_step = lambda host, pane, step: (sent.append(step["op"] + (":" + step["text"] if step["op"] == "text" else ":" + ",".join(step["keys"]))), None)[1]
def go(screens):
    sent.clear(); it = iter(screens); last = [screens[0]]
    srv.read_tail = lambda host, pane, lines, max_bytes=None: next(it, last[0])
    p = srv.build_plan({"host": "gus", "pane": "w1:p1", "kind": "choose", "option": 3, "text": "mine"}); srv.PLANS[p["nonce"]]["made"] = time.time(); srv.LAST_SENT.clear()
    try: srv.commit_plan(p["nonce"]); return None
    except srv.HerdrError as e: return e.code
srv.PLANS.clear()
res_ok = go([on(1), on(1), on(3)])                      # plan, commit check, then (after the arrows) the cursor IS on row 3
check("type-something: when the cursor lands on the row, the arrows, the words and Enter are sent in that order", res_ok is None and sent == ["keys:down,down", "text:mine", "keys:enter"])
res_bad = go([on(1), on(1), on(1)])                     # the arrows did nothing: the cursor is still on row 1
check("type-something: if the cursor did NOT land on the row, NOTHING is typed and Enter is not pressed", res_bad == "moved_on" and "text:mine" not in sent and "keys:enter" not in sent)

# multi-select with your own words: tick, arrow to the row, type (that ticks it), arrow to Submit
MULTI = """Pick any

%s 1. [ ] Alpha
         First
%s 2. [ ] Bravo
         Second
%s 3. [%s] %s
     Submit
─────────────────────────────
  4. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel"""
m_on = lambda row, free="Type something", tick=" ": MULTI % tuple(["❯" if r == row else " " for r in (1, 2, 3)] + [tick, free])
srv.read_tail = lambda host, pane, lines, max_bytes=None: m_on(1)
p = srv.build_plan({"host": "gus", "pane": "w1:p1", "kind": "multi", "picks": [1], "text": "my own", "advance": True})
st = srv.PLANS[p["nonce"]]["steps"]
check("multi + words: tick 1, arrow down to the typing row, type, then arrow to Submit and Enter (never Enter on the typing row)",
      [(s["op"], s.get("keys") or s.get("text")) for s in st] == [("keys", ["1"]), ("keys", ["down", "down"]), ("text", "my own"), ("keys", ["down", "enter"])] and any(l["k"] == "Types" and l["v"] == "my own" for l in p["lines"]))
srv.read_tail = lambda host, pane, lines, max_bytes=None: m_on(3)
p = srv.build_plan({"host": "gus", "pane": "w1:p1", "kind": "multi", "picks": [1], "text": "my own", "advance": True})
st = srv.PLANS[p["nonce"]]["steps"]
check("multi + words, cursor already on the typing row: step OFF it first so the digit is not typed into the text",
      [(s["op"], s.get("keys") or s.get("text")) for s in st][:3] == [("keys", ["up"]), ("keys", ["1"]), ("keys", ["down"])])
srv.read_tail = lambda host, pane, lines, max_bytes=None: m_on(1, free="Charlie")
check("multi + words: a question with no 'Type something' row refuses words", refused({"kind": "multi", "picks": [], "text": "x", "advance": True}) == "bad_text")
# the mid-plan recheck must accept that the typed-in row no longer says "Type something"
screens = [m_on(1), m_on(1), m_on(3, free="my own", tick="✔")]
it = iter(screens); lastm = [screens[-1]]
srv.read_tail = lambda host, pane, lines, max_bytes=None: next(it, lastm[0])
srv.PLANS.clear(); srv.LAST_SENT.clear(); sent.clear()
p = srv.build_plan({"host": "gus", "pane": "w1:p1", "kind": "multi", "picks": [1], "text": "my own", "advance": True}); srv.PLANS[p["nonce"]]["made"] = time.time()
try: srv.commit_plan(p["nonce"]); r = None
except srv.HerdrError as e: r = e.code
check("multi + words: the whole sequence runs, and the row you typed in (now showing your words) does not trip the safety recheck", r is None and sent[-1] == "keys:down,enter" and "text:my own" in sent)

# ---- the poller reads each blocked agent's screen, so the phone can say WHAT it is waiting on (or that nothing is on screen)
SCREENS = {"w1:p1": "Which approach?\n\n❯ 1. One\n  2. Two\n  3. Type something.\n──────\n  4. Chat about this", "w2:p1": "I am thinking about something.\nNo question here."}
srv.read_tail = lambda host, pane, lines, max_bytes=None: SCREENS[pane]
raw = lambda pane, st: {"pane_id": pane, "agent_status": st, "state_change_seq": 1, "cwd": "/x", "agent_session": {}, "workspace_id": "w"}
srv.run_herdr = lambda machine, args, timeout=10, want_json=True: ({"result": {"agents": [raw("w1:p1", "blocked"), raw("w2:p1", "blocked"), raw("w3:p1", "working")]}} if args[:2] == ["agent", "list"] else {"result": {"workspaces": []}})
hh, ags, _ws, err = srv.poll_host({"id": "gus", "machine": None, "kind": "local", "enabled": True})
sh = {a["pane_id"]: srv.shape_agent({"id": "gus"}, a, {}) for a in ags}
check("poll: a blocked agent with a menu carries its question and option count", sh["w1:p1"]["ask"] == {"q": "Which approach?", "n": 4, "menu": True})
check("poll: a blocked agent with nothing to answer on screen is marked, not hidden", sh["w2:p1"]["ask"] == {"q": None, "n": 0, "menu": False})
check("poll: an agent that is not blocked is never read", sh["w3:p1"]["ask"] is None)
# ---- a dead host must not make every refresh wait, nor keep the live hosts' data stale
srv.HOST_SNAP.clear(); srv.HOST_ORDER.clear(); srv._inflight.clear(); srv.SNAP.update(ts=0, hosts=[], agents=[], polls=0)
calls = {"slow": 0}
H_FAST = {"id": "gus", "machine": None, "kind": "local", "enabled": True}
H_SLOW = {"id": "vic", "machine": "vic", "kind": "remote", "enabled": True}
srv.discover_hosts = lambda: [H_FAST, H_SLOW]
def fake_poll_host(h):
    if h["id"] == "vic":
        calls["slow"] += 1; time.sleep(2.5); return h, None, {}, "timeout: ssh took longer than 9s"
    return h, [{"pane_id": "w1:p1", "agent_status": "blocked", "state_change_seq": 1, "cwd": "/x", "agent_session": {}, "workspace_id": "w", "_ask": None}], {"w": "burnbar"}, None
srv.poll_host = fake_poll_host
t0 = time.time(); srv.poll_all(wait=0.6); took = time.time() - t0
check("dead host: a refresh returns in well under the dead host's 2.5 s timeout", took < 1.5)
check("dead host: the live host's agents are already in the snapshot when it returns", [a["wlabel"] for a in srv.SNAP["agents"]] == ["burnbar"] and [h["id"] for h in srv.SNAP["hosts"]] == ["gus"])
t0 = time.time(); srv.poll_all(wait=0.3); srv.poll_all(wait=0.3)
check("dead host: asking again while its poll is still running does not start a second poll for it", calls["slow"] == 1)
time.sleep(2.6)
check("dead host: when it finally answers it appears, as down, next to the live host (order kept)", [(h["id"], h["status"]) for h in srv.SNAP["hosts"]] == [("gus", "ok"), ("vic", "down")] and len(srv.SNAP["agents"]) == 1)

# ---- subscription budget: Shepr measures it itself (usage.py); the endpoint just hands over the meter's view
fixture = os.path.join(tempfile.mkdtemp(), "usage.json")
srv.USAGE_FIXTURE = fixture
srv.USAGE_FIXTURE = os.path.join(tempfile.mkdtemp(), "missing.json")
v = srv.budget_view()
check("budget: a missing fixture says so instead of failing", v["available"] is False and v["agents"] == [])
srv.USAGE_FIXTURE = fixture
open(fixture, "w").write("{not json")
check("budget: a corrupt fixture is unavailable, not a crash", srv.budget_view()["available"] is False)
open(fixture, "w").write("[1, 2, 3]")
check("budget: a fixture that is not an object is unavailable", srv.budget_view()["available"] is False)
open(fixture, "w").write(json.dumps({"available": True, "agents": [{"id": "claude", "name": "Claude", "rows": []}], "now": 1}))
v = srv.budget_view(now=5.0)
check("budget: a fixture is returned as it is, with the current time put on it", v["available"] is True and v["agents"][0]["id"] == "claude" and v["now"] == 5000)
open(fixture, "w").write(" " * (srv.USAGE_FIXTURE_MAX + 10))
check("budget: an oversized fixture is refused", srv.budget_view()["available"] is False)
srv.USAGE_FIXTURE = None
v = srv.budget_view()
check("budget: with no fixture the real meter answers at once, without waiting on any network (nothing has been measured yet)", isinstance(v, dict) and "agents" in v and "available" in v and v.get("measuring") in (True, False))
src = open(os.path.join(here, "..", "server.py")).read() + open(os.path.join(here, "..", "usage.py")).read()
check("budget: Shepr reads no Burn Bar or Omarchy file: neither the path nor the variable is in the code", ".local/state/omarchy" not in src and "SHEPR_BURN" not in src and "burnbar/history" not in src)


# ---- model and effort: /model <name> and /effort <level>, only to a Claude session waiting for its next prompt
srv.need_host = lambda h: {"id": "gus", "machine": None, "kind": "local", "enabled": True}
def agent_is(kind, status):
    srv.get_agent = lambda host, pane: {"agent": kind, "agent_status": status, "state_change_seq": 9, "agent_session": {"value": "s9"}}
def refused(body):
    try:
        srv.build_plan(dict(body, host="gus", pane="w1:p1")); return None
    except srv.HerdrError as e:
        return e.code
agent_is("claude", "idle")
p = srv.build_plan({"kind": "model", "value": "opus", "host": "gus", "pane": "w1:p1"})
check("model: an idle Claude gets exactly '/model opus' as a prompt, and the sheet says it is saved as the default",
      srv.PLANS[p["nonce"]]["steps"] == [{"op": "prompt", "text": "/model opus"}] and any("default for new sessions" in l["v"] for l in p["lines"]))
p = srv.build_plan({"kind": "effort", "value": "xhigh", "host": "gus", "pane": "w1:p1"})
check("effort: '/effort xhigh'", srv.PLANS[p["nonce"]]["steps"] == [{"op": "prompt", "text": "/effort xhigh"}])
agent_is("claude", "done")
check("model: a finished Claude is allowed too", refused({"kind": "model", "value": "haiku"}) is None)
check("model: values outside the offered lists are refused, including ones that would smuggle in more text",
      all(refused({"kind": k, "value": v}) == "bad_value" for k, v in [("model", "opus\n/exit"), ("model", "gpt-5"), ("effort", "extreme"), ("effort", None), ("model", ["opus"]), ("effort", "high; rm -rf")]))
agent_is("claude", "working")
check("model: not while the agent is working", refused({"kind": "model", "value": "opus"}) == "not_idle")
agent_is("claude", "blocked")
check("model: not while it is waiting on a question (that dialog would eat the text)", refused({"kind": "effort", "value": "low"}) == "not_idle")
agent_is("codex", "idle")
check("model: only Claude Code sessions", refused({"kind": "model", "value": "opus"}) == "not_claude")

# ---- typed words at a numbered question, queued messages, and the hints that do nothing from a phone
HERE = os.path.dirname(os.path.abspath(__file__))
fx = lambda n: open(os.path.join(HERE, n)).read()
srv.need_host = lambda h: {"id": "gus", "machine": None, "kind": "local", "enabled": True}
srv.get_agent = lambda host, pane: {"agent": "claude", "agent_status": "blocked", "state_change_seq": 7, "agent_session": {"value": "s1"}}
PLAIN = "Do you want to proceed?\n\u276f 1. Yes\n  2. Yes, and do not ask again\n  3. No\n\nEsc to cancel\n"
srv.read_tail = lambda host, pane, n, **k: PLAIN
try:
    srv.build_plan({"kind": "text", "text": "I should be able to multi select here. I cant do 1 2 3", "host": "gus", "pane": "w1:p1"}); code = None
except srv.HerdrError as e:
    code = e.code
check("typing: words at a numbered question with no typing row are refused, never typed (a digit would pick an option)", code == "menu_text")
srv.read_tail = lambda host, pane, n, **k: fx("sample-free.txt")
p = srv.build_plan({"kind": "text", "text": "my own answer", "host": "gus", "pane": "w1:p1"})
st = srv.PLANS[p["nonce"]]["steps"]
check("typing: with a 'Type something' row the words go into that row (arrow to it, the words, Enter), not at the cursor",
      [x["op"] for x in st] == ["keys", "text", "keys"] and st[0]["keys"] == ["down", "down"] and st[1]["text"] == "my own answer" and st[2]["keys"] == ["enter"])
srv.read_tail = lambda host, pane, n, **k: "Some prompt that is not a numbered menu\n> "
p = srv.build_plan({"kind": "text", "text": "yes", "host": "gus", "pane": "w1:p1"})
check("typing: a blocked agent that is NOT at a numbered menu still gets the words typed and Enter", [x["op"] for x in srv.PLANS[p["nonce"]]["steps"]] == ["text", "keys"])
srv.get_agent = lambda host, pane: {"agent": "claude", "agent_status": "working", "state_change_seq": 8, "agent_session": {"value": "s1"}}
srv.read_tail = lambda host, pane, n, **k: fx("sample-queued.txt")
p = srv.build_plan({"kind": "sendnow", "host": "gus", "pane": "w1:p1"})
check("send now: with a message queued it plans exactly Ctrl+Enter", srv.PLANS[p["nonce"]]["steps"] == [{"op": "keys", "keys": ["ctrl+enter"]}])
srv.read_tail = lambda host, pane, n, **k: "just working\n"
try:
    srv.build_plan({"kind": "sendnow", "host": "gus", "pane": "w1:p1"}); code = None
except srv.HerdrError as e:
    code = e.code
check("send now: refused when nothing is queued on screen, so Ctrl+Enter can never land as a stray key", code == "nothing_queued")
srv.get_agent = lambda host, pane: {"agent": "codex", "agent_status": "working", "state_change_seq": 8, "agent_session": {"value": "s1"}}
srv.read_tail = lambda host, pane, n, **k: fx("sample-queued.txt")
try:
    srv.build_plan({"kind": "sendnow", "host": "gus", "pane": "w1:p1"}); code = None
except srv.HerdrError as e:
    code = e.code
check("send now: only for Claude Code", code == "not_claude")
for k in ("ctrl+c", "ctrl+enter", "ctrl+d"):
    try:
        srv.build_plan({"kind": "keys", "keys": [k], "host": "gus", "pane": "w1:p1"}); code = None
    except srv.HerdrError as e:
        code = e.code
    check(f"keys: {k} is still not a key the phone may send on its own", code in ("bad_keys",))
text, queued = srv.clean_tail(fx("sample-queued.txt"))
check("tail: the queued hint lines and the (ctrl+o to expand) fragment are removed, the queued message itself stays, and queued is reported",
      queued and "Press up to edit" not in text and "send now" not in text and "ctrl+o" not in text and "QUEUED-SECOND" in text)
check("tail: ordinary text that merely mentions the shortcut is left alone", srv.clean_tail("press ctrl+enter to send now, he said") == ("press ctrl+enter to send now, he said", False))

# ---- hosts: a down machine reports when it was last answering
srv.machine_profiles = lambda: [{"label": "vic", "id": "p1", "enabled": True, "target": "x"}]
srv.hub_pubkey = lambda: None
with srv.LOCK:
    srv.SNAP["hosts"] = [{"id": "gus", "status": "ok", "counts": {}}, {"id": "vic", "status": "down", "error": "timeout: ssh took longer than 9s", "last_ok": 1791600000.0, "counts": {}}]
hv = srv.hosts_view()
vic = next(h for h in hv["saved"] if h["id"] == "vic")
check("hosts: a down machine reports its error and when it last answered, for the Hosts page", vic["status"] == "down" and vic["error"].startswith("timeout") and vic["last_ok"] == 1791600000.0)

def bash_enroll(pk):
    """Run the Copy-key command in a throwaway home. Returns (rc, authorized_keys text, whether INJECTED appeared)."""
    srv.hub_pubkey = lambda: pk
    cmd = srv.hosts_view()["enroll"]
    home = tempfile.mkdtemp()
    env = dict(os.environ); env["HOME"] = home
    r = subprocess.run(["bash", "-c", cmd], cwd=home, env=env, capture_output=True, text=True, timeout=5)
    keyp = os.path.join(home, ".ssh", "authorized_keys")
    wrote = open(keyp).read() if os.path.exists(keyp) else ""
    side = os.path.exists(os.path.join(home, "INJECTED"))
    shutil.rmtree(home, ignore_errors=True)
    return r.returncode, wrote, side

plain_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyMaterial user@host"
rc, wrote, side = bash_enroll(plain_key)
check("enroll: a normal public key is appended as that one line", rc == 0 and wrote == plain_key + "\n" and not side)
apos_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyMaterial fred's laptop"
rc, wrote, side = bash_enroll(apos_key)
check("enroll: an apostrophe in the key comment still installs that exact key", rc == 0 and wrote == apos_key + "\n" and not side)
bad_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyMaterial x'; touch INJECTED; echo '"
rc, wrote, side = bash_enroll(bad_key)
check("enroll: a quote in the key comment cannot run a command beside the echo", rc == 0 and wrote == bad_key + "\n" and not side)

check("gone session: herdr agent_not_found and pane_not_found are 404, so the phone can leave",
      srv.http_status("agent_not_found") == 404 and srv.http_status("pane_not_found") == 404)
check("gone session: a bad request stays 400, a dead herdr stays 502, an unknown host stays 404",
      srv.http_status("bad_pane") == 400 and srv.http_status("timeout") == 502 and srv.http_status("bad_host") == 404
      and srv.http_status("unpaired") == 401)

# ---- a question that cannot take words: the person may skip it. Esc closes it WITHOUT answering, the words follow as a prompt.
NUMQ = """Do you want to proceed?

❯ 1. Yes
  2. Yes, and do not ask again
  3. No

Esc to cancel"""
srv.READONLY = False
srv.need_host = lambda h: {"id": "gus", "machine": None, "kind": "local", "enabled": True}
state = {"status": "blocked"}
STUB = lambda host, pane: {"agent_status": state["status"], "state_change_seq": 9, "agent_session": {"value": "s9"}, "cwd": "/tmp/x"}
srv.get_agent = STUB
srv.read_tail = lambda host, pane, lines, max_bytes=None: NUMQ
check("skip: words at a numbered question are still refused as they are, with their own code", refused({"kind": "text", "text": "hello"}) == "menu_text")
check("skip: several lines at a dialog are refused with their own code", refused({"kind": "text", "text": "a\nb"}) == "blocked_text")
ps = srv.build_plan({"host": "gus", "pane": "w1:p1", "kind": "text", "text": "line one\nline two", "skip": True})
sk = srv.PLANS[ps["nonce"]]["steps"]
check("skip: the plan is Esc, wait for the question to close, then the words as a prompt (several lines are fine), and approves nothing",
      [s["op"] for s in sk] == ["keys", "await_clear", "prompt"] and sk[0]["keys"] == ["esc"] and sk[2]["text"] == "line one\nline two"
      and ps["approves"] is False and any(l["k"] == "Closes the question" and "Nothing is answered or approved" in l["v"] for l in ps["lines"]))
state["status"] = "working"
pw = srv.build_plan({"host": "gus", "pane": "w1:p1", "kind": "text", "text": "hello", "skip": True})
check("skip: on an agent that is not blocked the flag does nothing, so Esc can never interrupt its work", [s["op"] for s in srv.PLANS[pw["nonce"]]["steps"]] == ["prompt"])
state["status"] = "blocked"
srv.AWAIT_CLEAR_S = 0.8
flip = {"n": 0}
def closing(host, pane):
    flip["n"] += 1
    return {"agent_status": "blocked" if flip["n"] < 3 else "idle"}
srv.get_agent = closing
check("skip: the wait ends the moment the question has closed", REAL_RUN_STEP({"machine": None}, "w1:p1", {"op": "await_clear"}) is None and flip["n"] == 3)
srv.get_agent = lambda host, pane: {"agent_status": "blocked"}
try: REAL_RUN_STEP({"machine": None}, "w1:p1", {"op": "await_clear"}); stuck = None
except srv.HerdrError as e: stuck = e.code
check("skip: if the question never closes it says so", stuck == "still_blocked")
srv.get_agent = STUB
order, never_closes = [], [False]
def rec(host, pane, step):
    order.append(step["op"])
    if step["op"] == "await_clear" and never_closes[0]:
        raise srv.HerdrError("still_blocked", "the question did not close, so your words were not sent")
srv.run_step = rec
def commit_skip():
    order.clear(); pl = srv.build_plan({"host": "gus", "pane": "w1:p1", "kind": "text", "text": "mine", "skip": True})
    srv.PLANS[pl["nonce"]]["made"] = time.time(); srv.LAST_SENT.clear()
    try: srv.commit_plan(pl["nonce"]); return None
    except srv.HerdrError as e: return e.code
err_ok = commit_skip(); order_ok = list(order)
never_closes[0] = True; err_stuck = commit_skip(); order_stuck = list(order)
check("skip: committed in order, Esc then the wait then the words", err_ok is None and order_ok == ["keys", "await_clear", "prompt"])
check("skip: when the question will not close, the words are never sent", err_stuck == "still_blocked" and order_stuck == ["keys", "await_clear"])

# ---- an EXISTING folder opened as a herdr project: listed, planned and committed without creating or changing anything in it
import tempfile, shutil
froot = tempfile.mkdtemp()
for d in ("plain", "with-git/.git", "has-history", "busy", ".hidden", "_archive", "bad name"):
    os.makedirs(os.path.join(froot, d))
open(os.path.join(froot, "a-file.txt"), "w").write("x")
os.symlink(os.path.join(froot, "plain"), os.path.join(froot, "linked"))
srv.PROJECTS_DIR = froot
srv.READONLY = False
LOCALH = {"id": "gus", "machine": None, "kind": "local", "enabled": True}
srv.need_host = lambda h: LOCALH
srv.run_herdr = lambda machine, args, timeout=10, want_json=True: ({"result": {"panes": [{"pane_id": "w1:p1", "foreground_cwd": os.path.join(froot, "busy", "sub"), "cwd": "/x"}]}}
                                                                   if args[:2] == ["pane", "list"] else {"result": {"workspaces": [{"label": "dup"}]}})
srv.project_last_active = lambda cwd: 2000.0 if cwd.endswith("has-history") else None
srv.codex_last_active = lambda cwd: 1000.0 if cwd.endswith("with-git") else None
before = sorted(os.listdir(froot))
fv = srv.folders_view(LOCALH)
fnames = [i["dir"] for i in fv["items"]]
check("folders: only plain folders directly inside the projects folder are listed: no file, no dot or underscore folder, no name with a space", set(fnames) == {"plain", "with-git", "has-history", "linked"})
check("folders: one herdr already has a terminal in is left out and counted", "busy" not in fnames and fv["open"] == 1)
check("folders: newest conversation first, then the rest by name", fnames == ["has-history", "with-git", "linked", "plain"])
fi = {i["dir"]: i for i in fv["items"]}
check("folders: each says whether it is a git repo and when Claude or Codex last ran there", fi["with-git"]["git"] is True and fi["plain"]["git"] is False and fi["has-history"]["claude"] == 2000.0 and fi["with-git"]["codex"] == 1000.0 and fi["plain"]["last"] is None)
try: srv.folders_view({"id": "vic", "machine": "vic", "kind": "remote"}); remote_code = None
except srv.HerdrError as e: remote_code = e.code
check("folders: another machine cannot be listed from here", remote_code == "local_only")
def opf(**kw):
    try: return srv.build_plan(dict({"kind": "open_folder", "host": "gus"}, **kw))
    except srv.HerdrError as e: return e.code
check("open folder: path tricks and odd names are refused", all(opf(dir=x, mode="fresh") == "bad_dir" for x in ("../x", "a/b", ".x", "x.", "", 5, "a;b")))
check("open folder: a folder that is not there is never created, it is refused", opf(dir="nope", mode="fresh") == "no_folder" and not os.path.exists(os.path.join(froot, "nope")))
check("open folder: a bad mode, agent or name is refused", opf(dir="plain", mode="x") == "bad_mode" and opf(dir="plain", mode="fresh", agent="gpt") == "bad_agent"
      and opf(dir="plain", mode="fresh", name="a\nb") == "bad_name" and opf(dir="plain", mode="fresh", name="x" * 61) == "bad_name")
check("open folder: resuming where nothing is recorded is refused, for Claude and for Codex", opf(dir="plain", mode="continue") == "nothing_to_resume" and opf(dir="plain", mode="continue", agent="codex") == "nothing_to_resume")
check("start command: the default is plain claude, so a stranger with no ccc alias works (ccc is the author's own alias)", srv.NEW_CMD == "claude" and srv.CODEX_CMD == "codex")
_h = tempfile.mkdtemp(); _old_home = os.environ.get("HOME"); os.environ["HOME"] = _h
try:
    open(os.path.join(_h, ".bashrc"), "w").write("alias zz='claude --some-flag'\nalias other='ls'\n")
    check("start command: a shell alias of your own is explained on the confirm sheet, a plain command is not", srv.alias_expansion("zz") == "claude --some-flag" and srv.alias_expansion("claude") is None)
finally:
    os.environ["HOME"] = _old_home if _old_home is not None else ""; shutil.rmtree(_h, ignore_errors=True)
srv.NEW_CMD, srv.CODEX_CMD = "ccc", "codex"
cases = {("has-history", "continue", "claude"): f"cd {froot}/has-history && ccc --continue", ("plain", "fresh", "claude"): f"cd {froot}/plain && ccc",
         ("with-git", "continue", "codex"): f"cd {froot}/with-git && codex resume --last", ("plain", "fresh", "codex"): f"cd {froot}/plain && codex"}
good = True
for (d, mode, agent), want in cases.items():
    pl = srv.build_plan({"kind": "open_folder", "host": "gus", "dir": d, "mode": mode, "agent": agent}); st = srv.PLANS[pl["nonce"]]["steps"][0]
    good = good and st["cmd"] == want and "mkdir" not in st["cmd"] and st["op"] == "new_project" and st["cwd"] == os.path.join(froot, d) and pl["approves"] is False and pl["kind"] == "open_folder"
check("open folder: the command is cd then the agent, resuming or fresh, never mkdir, and the plan approves nothing", good)
# the projects folder is data for the shell too: a space or an apostrophe in SHEPR_PROJECTS must neither split the command
# nor swallow the && after it. Run the real command in a throwaway home and look at where the folder and the agent land.
_save = (srv.PROJECTS_DIR, srv.NEW_CMD, os.environ.get("HOME"))
srv.NEW_CMD = "touch STARTED"
def _lands(top, spelled, kind):
    """True when the command made/entered exactly <home>/<top>/proj, started there, and wrote nothing anywhere else."""
    qroot = tempfile.mkdtemp(); home = os.path.join(qroot, "home"); start = os.path.join(qroot, "start"); os.makedirs(home); os.makedirs(start)
    os.environ["HOME"] = home
    srv.PROJECTS_DIR = spelled.replace("HOME", home)
    if kind == "open": os.makedirs(os.path.join(home, top, "proj"))
    pl = srv.build_plan({"kind": "open_folder", "host": "gus", "dir": "proj", "mode": "fresh"} if kind == "open"
                        else {"kind": "new_project", "host": "gus", "name": "N", "dir": "proj"})
    cmd = srv.PLANS[pl["nonce"]]["steps"][0]["cmd"]
    r = subprocess.run(["bash", "-c", cmd], cwd=start, env=dict(os.environ, HOME=home), capture_output=True, text=True, timeout=10)
    good = os.path.isfile(os.path.join(home, top, "proj", "STARTED")) and os.listdir(start) == [] and os.listdir(home) == [top]
    shutil.rmtree(qroot, ignore_errors=True)
    return good and r.returncode == 0
try:
    for kind in ("new", "open"):
        check(f"projects path ({kind}): a space in the projects folder, spelled ~/ or absolute, lands in that folder and starts there",
              _lands("My Projects", "~/My Projects", kind) and _lands("My Projects", "HOME/My Projects", kind))
        check(f"projects path ({kind}): an apostrophe cannot swallow the command or rename the folder",
              _lands("Fred's Projects", "~/Fred's Projects", kind) and _lands("Fred's Projects", "HOME/Fred's Projects", kind))
    srv.PROJECTS_DIR = "~/Projects"
    check("projects path: a plain ~/Projects/name is typed exactly as before", srv.shell_path("~/Projects/alpha") == "~/Projects/alpha" and srv.shell_path("/srv/p/alpha") == "/srv/p/alpha"
          and srv.PLANS[srv.build_plan({"kind": "new_project", "host": "gus", "name": "N", "dir": "alpha"})["nonce"]]["steps"][0]["cmd"] == "mkdir -p ~/Projects/alpha && cd ~/Projects/alpha && touch STARTED")
finally:
    srv.PROJECTS_DIR, srv.NEW_CMD = _save[0], _save[1]
    if _save[2] is None: os.environ.pop("HOME", None)
    else: os.environ["HOME"] = _save[2]
pl = srv.build_plan({"kind": "open_folder", "host": "gus", "dir": "plain", "mode": "fresh", "name": "dup"})
lk = {l["k"]: l["v"] for l in pl["lines"]}
check("open folder: the sheet says the folder exists and nothing will be created, and warns that a herdr project has that name", "exists" in lk["Folder"] and "nothing will be created" in lk["Folder"] and any(l["k"] == "Heads up" for l in pl["lines"]))
pb = srv.build_plan({"kind": "open_folder", "host": "gus", "dir": "busy", "mode": "fresh"})
check("open folder: a folder herdr already has a terminal in is allowed, with a warning", any(l["k"] == "Heads up" and "terminal in this folder" in l["v"] for l in pb["lines"]))
srv.READONLY = True
check("open folder: a read-only server refuses it", opf(dir="plain", mode="fresh") == "read_only")
srv.READONLY = False
calls = []
srv.run_herdr = lambda machine, args, timeout=10, want_json=True: (calls.append(list(args)) or ({"result": {"root_pane": {"pane_id": "w9:p1"}}} if args[:2] == ["workspace", "create"] else ""))
pl = srv.build_plan({"kind": "open_folder", "host": "gus", "dir": "plain", "mode": "fresh"})
calls.clear()   # planning looks at herdr too; only what the COMMIT does is of interest here
srv.commit_plan(pl["nonce"])
check("open folder: committing makes the herdr project in that folder, then types the command, and the folder itself is untouched",
      calls[0][:4] == ["workspace", "create", "--label", "plain"] and calls[0][calls[0].index("--cwd") + 1] == os.path.join(froot, "plain") and calls[1][:3] == ["pane", "run", "w9:p1"]
      and calls[1][3] == f"cd {froot}/plain && ccc" and sorted(os.listdir(froot)) == before and os.listdir(os.path.join(froot, "plain")) == [])
shutil.rmtree(froot)

# ---- words for a blocked pane that is not a menu must not be typed if a numbered question appears first
bare_screen = {"t": "Some prompt that is not a numbered menu\n> "}
srv.READONLY = False
srv.need_host = lambda h: {"id": "gus", "machine": None, "kind": "local", "enabled": True}
srv.get_agent = lambda host, pane: {"agent": "claude", "agent_status": "blocked", "state_change_seq": 7, "agent_session": {"value": "s1"}, "cwd": "/tmp/x"}
srv.read_tail = lambda host, pane, n, **k: bare_screen["t"]
sent_bare = []
srv.run_step = lambda host, pane, step: sent_bare.append(dict(step))
srv.LAST_SENT.clear()
pb = srv.build_plan({"kind": "text", "text": "1", "host": "gus", "pane": "w1:p1"})
bare_screen["t"] = "Do you want to proceed?\n\u276f 1. Yes\n  2. No\n"
try:
    srv.commit_plan(pb["nonce"]); bare_err = None
except srv.HerdrError as e:
    bare_err = e.code
check("typing: if a numbered question appears before the commit, the words are not typed", bare_err == "moved_on" and sent_bare == [])
bare_screen["t"] = "Some prompt that is not a numbered menu\n> "
sent_bare.clear(); srv.LAST_SENT.clear()
pb = srv.build_plan({"kind": "text", "text": "hello", "host": "gus", "pane": "w1:p1"})
try:
    srv.commit_plan(pb["nonce"]); bare_ok = None
except srv.HerdrError as e:
    bare_ok = e.code
check("typing: a blocked pane that stays a non-menu still gets the words and Enter", bare_ok is None and [s["op"] for s in sent_bare] == ["text", "keys"] and sent_bare[0]["text"] == "hello")
# ---- keys planned for one blocked screen must not land on a later question that shares herdr's state counter
MENU_A = "Old question?\n\u276f 1. Keep going\n  2. Stop here\n"
MENU_B = "Do you want to proceed?\n\u276f 1. Yes\n  2. No\n"
NONMENU = "Some prompt that is not a numbered menu\n> "
kscreen = {"t": MENU_A}
srv.READONLY = False
srv.need_host = lambda h: {"id": "gus", "machine": None, "kind": "local", "enabled": True}
srv.get_agent = lambda host, pane: {"agent": "claude", "agent_status": "blocked", "state_change_seq": 7, "agent_session": {"value": "s1"}, "cwd": "/tmp/x"}
srv.read_tail = lambda host, pane, n, **k: kscreen["t"]
sent_k = []
srv.run_step = lambda host, pane, step: sent_k.append(dict(step))

def commit_keys(before, after, keys):
    sent_k.clear(); srv.LAST_SENT.clear(); kscreen["t"] = before
    p = srv.build_plan({"kind": "keys", "keys": keys, "host": "gus", "pane": "w1:p1"})
    kscreen["t"] = after
    try:
        srv.commit_plan(p["nonce"]); return None
    except srv.HerdrError as e:
        return e.code

err = commit_keys(MENU_A, MENU_B, ["1", "enter"])
check("keys: a different question with the same state counter does not receive the keys", err == "moved_on" and sent_k == [])
err = commit_keys(NONMENU, MENU_B, ["enter"])
check("keys: Enter planned against a non-menu is not sent once a numbered question is up", err == "moved_on" and sent_k == [])
err = commit_keys(MENU_A, MENU_A, ["1", "enter"])
check("keys: the same question still receives the keys", err is None and sent_k == [{"op": "keys", "keys": ["1", "enter"]}])
err = commit_keys(NONMENU, NONMENU, ["enter"])
check("keys: a blocked pane that stays a non-menu still receives Enter", err is None and sent_k == [{"op": "keys", "keys": ["enter"]}])
# ---- Send now is Ctrl+Enter only while the queue line is still on screen. The state counter need not move when it goes.
qscreen = {"t": fx("sample-queued.txt")}
srv.READONLY = False
srv.need_host = lambda h: {"id": "gus", "machine": None, "kind": "local", "enabled": True}
srv.get_agent = lambda host, pane: {"agent": "claude", "agent_status": "working", "state_change_seq": 8, "agent_session": {"value": "s1"}, "cwd": "/tmp/x"}
srv.read_tail = lambda host, pane, n, **k: qscreen["t"]
sent_q = []
srv.run_step = lambda host, pane, step: sent_q.append(dict(step))
srv.LAST_SENT.clear()
pq = srv.build_plan({"kind": "sendnow", "host": "gus", "pane": "w1:p1"})
qscreen["t"] = "still working, the queue line is gone\n"
try:
    srv.commit_plan(pq["nonce"]); qerr = None
except srv.HerdrError as e:
    qerr = e.code
check("send now: if the queue line disappears before the commit, Ctrl+Enter is not sent", qerr == "moved_on" and sent_q == [])
qscreen["t"] = fx("sample-queued.txt")
sent_q.clear(); srv.LAST_SENT.clear()
pq = srv.build_plan({"kind": "sendnow", "host": "gus", "pane": "w1:p1"})
try:
    srv.commit_plan(pq["nonce"]); qok = None
except srv.HerdrError as e:
    qok = e.code
check("send now: while the queue line is still on screen, Ctrl+Enter is sent", qok is None and sent_q == [{"op": "keys", "keys": ["ctrl+enter"]}])
# ---- skip waits for the question to close, then must not prompt a different session that now owns the pane
calls_s = []
srv.run_herdr = lambda machine, args, timeout=10, want_json=True: calls_s.append(list(args)) or ""
srv.run_step = REAL_RUN_STEP
srv.need_host = lambda h: {"id": "gus", "machine": None, "kind": "local", "enabled": True}
srv.READONLY = False
srv.read_tail = lambda host, pane, n, **k: "Do you want to proceed?\n\u276f 1. Yes\n  2. No\n"
n_s = {"i": 0}
def ga_changed(host, pane):
    n_s["i"] += 1
    if n_s["i"] <= 2:
        return {"agent": "claude", "agent_status": "blocked", "state_change_seq": 9, "agent_session": {"value": "sess-A"}, "cwd": "/tmp/proj"}
    return {"agent": "claude", "agent_status": "idle", "state_change_seq": 1, "agent_session": {"value": "sess-B"}, "cwd": "/tmp/other"}
srv.get_agent = ga_changed
srv.LAST_SENT.clear()
ps = srv.build_plan({"kind": "text", "text": "the words", "skip": True, "host": "gus", "pane": "w1:p1"})
try:
    srv.commit_plan(ps["nonce"]); serr = None
except srv.HerdrError as e:
    serr = e.code
prompts = [c for c in calls_s if len(c) >= 2 and c[:2] == ["agent", "prompt"]]
check("skip: a session that changes after the question closes does not get the words", serr == "moved_on" and prompts == [])
n_s["i"] = 0
calls_s.clear()
def ga_same(host, pane):
    n_s["i"] += 1
    if n_s["i"] <= 2:
        return {"agent": "claude", "agent_status": "blocked", "state_change_seq": 9, "agent_session": {"value": "sess-A"}, "cwd": "/tmp/proj"}
    return {"agent": "claude", "agent_status": "idle", "state_change_seq": 10, "agent_session": {"value": "sess-A"}, "cwd": "/tmp/proj"}
srv.get_agent = ga_same
srv.LAST_SENT.clear()
ps = srv.build_plan({"kind": "text", "text": "the words", "skip": True, "host": "gus", "pane": "w1:p1"})
try:
    srv.commit_plan(ps["nonce"]); serr = None
except srv.HerdrError as e:
    serr = e.code
prompts = [c for c in calls_s if len(c) >= 2 and c[:2] == ["agent", "prompt"]]
check("skip: the same session, once the question has closed, still gets the words", serr is None and prompts == [["agent", "prompt", "w1:p1", "the words"]])
# ---- a tapped option (choose) or a set of ticks (multi) must not answer the NEXT question of the same dialog, which keeps
# herdr's state counter and can have the very same options
Q_A = "Allow Claude to read notes.txt?\n❯ 1. Yes\n  2. No\n"
Q_B = "Allow Claude to run rm -rf ~/Projects?\n❯ 1. Yes\n  2. No\n"
T_A = "Which files may Claude read?\n\n❯ 1. [ ] notes.txt\n  2. [ ] todo.md\n     Submit\n"
T_B = T_A.replace("may Claude read?", "may Claude DELETE?")
qs = {"t": Q_A}
srv.READONLY = False
srv.need_host = lambda h: {"id": "gus", "machine": None, "kind": "local", "enabled": True}
srv.get_agent = lambda host, pane: {"agent": "claude", "agent_status": "blocked", "state_change_seq": 7, "agent_session": {"value": "s1"}, "cwd": "/tmp/x"}
srv.read_tail = lambda host, pane, n, **k: qs["t"]
sent_q2 = []
srv.run_step = lambda host, pane, step: sent_q2.append(dict(step))
def commit_on(before, after, body):
    sent_q2.clear(); srv.LAST_SENT.clear(); qs["t"] = before
    p = srv.build_plan(dict(body, host="gus", pane="w1:p1"))
    qs["t"] = after
    try:
        srv.commit_plan(p["nonce"]); return None
    except srv.HerdrError as e:
        return e.code
check("choose: 1. Yes picked for one question is not sent to a different question with the same options and counter",
      commit_on(Q_A, Q_B, {"kind": "choose", "option": 1}) == "moved_on" and sent_q2 == [])
check("choose: the same question still gets the pick", commit_on(Q_A, Q_A, {"kind": "choose", "option": 1}) is None and sent_q2 == [{"op": "keys", "keys": ["enter"]}])
check("multi: ticks made for one question are not sent to a different question with the same options and counter",
      commit_on(T_A, T_B, {"kind": "multi", "picks": [1, 2], "advance": True}) == "moved_on" and sent_q2 == [])
check("multi: the same question still gets the ticks and Submit",
      commit_on(T_A, T_A, {"kind": "multi", "picks": [1, 2], "advance": True}) is None and [s["keys"] for s in sent_q2] == [["1"], ["2"], ["down", "down", "enter"]])
# ---- malformed input gets an error reply, not an exception that drops the connection (/api/pair needs no signature)
srv.mutate_devices(lambda devs: devs.__setitem__("odd1", {"name": "odd", "x": "AA", "y": "AA"}))
def err_code(fn):
    try: fn(); return None
    except srv.HerdrError as e: return e.code
    except Exception as e: return type(e).__name__
srv.PAIR_FAILS.clear()
check("pair: a jwk that is a list or a string is a bad key, not a crash",
      err_code(lambda: srv.do_pair({"code": "x", "jwk": [1]}, "ua")) == "bad_key" and err_code(lambda: srv.do_pair({"code": "x", "jwk": "EC"}, "ua")) == "bad_key")
check("signature: a timestamp too large for a float is a bad signature, not a crash",
      err_code(lambda: srv.verify_request({"X-Shepr-Device": "odd1", "X-Shepr-Ts": "9" * 400, "X-Shepr-Nonce": "n" * 10, "X-Shepr-Sig": "AA"}, "GET", "/api/queue", b"")) == "bad_signature")
check("wake: a target that is not an object is refused, not a crash", err_code(lambda: srv.plan_wake({"text": "hi", "targets": ["w1:p1"]})) == "bad_targets")

sys.exit(0 if ok else 1)
