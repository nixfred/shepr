"""Regression tests for the 2026-10-09 bug hunt (Codex, 19 findings). Pure Python, nothing talks to herdr, the
network or the real state directory.  Run: python3 tests/server_hardening_test.py"""
import hashlib, importlib.util, json, os, sys, tempfile, threading, time
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
check("first message: it is appended to the command, quoted; with none, the command is unchanged", c1.endswith("ccc 'it'\"'\"'s fine'") and c2.endswith("&& ccc") and any(l["k"] == "First message" for l in p1["lines"]) and not any(l["k"] == "First message" for l in p2["lines"]))
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

check("gone session: herdr agent_not_found and pane_not_found are 404, so the phone can leave",
      srv.http_status("agent_not_found") == 404 and srv.http_status("pane_not_found") == 404)
check("gone session: a bad request stays 400, a dead herdr stays 502, an unknown host stays 404",
      srv.http_status("bad_pane") == 400 and srv.http_status("timeout") == 502 and srv.http_status("bad_host") == 404
      and srv.http_status("unpaired") == 401)

sys.exit(0 if ok else 1)
