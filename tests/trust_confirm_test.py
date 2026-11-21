"""2026-11-21: (1) Claude Code's folder-trust prompt reads as idle to herdr, so it never reached the Queue and could not be answered;
Shepr now reads an idle Claude's screen, shows the trust dialog as a blocked question, and (SHEPR_AUTOTRUST, on by default, Fred's word)
presses the trust option itself, once, and nothing else. (2) A tap on a blocked dialog (a hook confirmation that sat 13 min) is no longer
refused as "moved on" when only herdr's state counter moved and the same dialog is still on screen.
Pure Python: no herdr, no network.   Run: python3 tests/trust_confirm_test.py"""
import importlib.util, os, sys, tempfile
here = os.path.dirname(os.path.abspath(__file__))
os.environ["SHEPR_STATE"] = tempfile.mkdtemp()
for _k in ("SHEPR_READONLY", "SHEPR_AUTOTRUST"): os.environ.pop(_k, None)
spec = importlib.util.spec_from_file_location("shepr_server", os.path.join(here, "..", "server.py"))
srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)
ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name); ok = ok and bool(cond)

TRUST = "\n".join(["", " Quick safety check: Is this a project you created or one you trust?", "", " /home/pi/nas/Projects/newthing", "",
                   " Claude Code'll be able to read, edit, and execute files here.", "",
                   " ❯ 1. Yes, I trust this folder", "   2. No, exit", "", " Enter to confirm · Esc to cancel"])
TRUST_OLD = TRUST.replace("Quick safety check: Is this a project you created or one you trust?", "Do you trust the files in this folder?")
PERM = "Allow Claude to run rm -rf ~/Projects?\n❯ 1. Yes\n  2. No\n"            # an ordinary permission dialog: never auto-answered
HOOK = ("Hook PreToolUse:Bash requires confirmation for this command:\n[PAI SECURITY] ⚠ Table destruction\n\n"
        " Do you want to proceed?\n ❯ 1. Yes\n   2. No\n\n Esc to cancel · Tab to amend\n")

screen = {"t": TRUST}
host = {"id": "gus", "machine": "m", "kind": "local", "enabled": True}
pressed = []
srv.read_tail = lambda h, pane, n, **k: screen["t"]
def fake_run(machine, args, **k):
    pressed.append(list(args)); return {}
srv.run_herdr = fake_run
srv.log_event = lambda *a, **k: None
def idle(agent="claude", status="idle", seq=5):
    return {"pane_id": "w1:p1", "agent": agent, "agent_status": status, "state_change_seq": seq, "cwd": "/home/pi/nas/Projects/newthing"}
def reset():
    srv.TRUST_CACHE.clear(); srv.TRUST_SENT.clear(); pressed.clear()

# ---- the dialog is seen, in both of Claude Code's wordings, and shown as a blocked question
for name, txt in (("new wording", TRUST), ("old wording", TRUST_OLD)):
    reset(); screen["t"] = txt; a = idle()
    m = srv.apply_trust(host, a)
    check(f"trust ({name}): an idle Claude on the dialog is shown as blocked with its menu", bool(m) and a["agent_status"] == "blocked" and a["_ask"]["menu"] and a["_ask"]["n"] == 2)
reset(); screen["t"] = TRUST; a = idle(status="unknown"); srv.apply_trust(host, a)
check("trust: an agent herdr calls unknown is read the same way", a["agent_status"] == "blocked")

# ---- only that dialog, only a Claude, only an agent that is not already working or blocked
reset(); screen["t"] = PERM; a = idle(); m = srv.apply_trust(host, a)
check("trust: an ordinary permission dialog on an idle pane is NOT taken for the trust prompt", m is None and a["agent_status"] == "idle")
reset(); screen["t"] = TRUST; a = idle(agent="codex"); check("trust: only Claude Code's prompt, not another agent's", srv.apply_trust(host, a) is None)
reset(); a = idle(status="working"); check("trust: a working agent's screen is never read for it", srv.apply_trust(host, a) is None and pressed == [])
reset(); screen["t"] = TRUST.replace("1. Yes, I trust this folder", "1. Yes, delete everything"); a = idle()
check("trust: trust wording without a trusting option is not the trust prompt", srv.apply_trust(host, a) is None)
reset(); screen["t"] = "ls\nrepo\n$ "; a = idle(); check("trust: a plain idle screen stays idle", srv.apply_trust(host, a) is None and a["agent_status"] == "idle")

reset(); screen["t"] = TRUST + "\n".join(["more"] * 30) + "\n\u276f 1. Yes, I trust this folder\n  2. No\n$ "
a = idle(); check("trust: the wording in old scrollback (not at the bottom, no key hint) is not the dialog", srv.apply_trust(host, a) is None)
reset(); screen["t"] = TRUST.replace(" Enter to confirm \u00b7 Esc to cancel", "")
a = idle(); check("trust: without its key-hint footer it is not taken for the dialog", srv.apply_trust(host, a) is None)

# ---- the one answer Shepr gives alone
reset(); screen["t"] = TRUST; srv.AUTOTRUST = True; a = idle(); m = srv.apply_trust(host, a)
srv.auto_trust(host, a, m, "newthing")
check("auto: presses Enter on the trusting option, nothing else", pressed == [["pane", "send-keys", "w1:p1", "enter"]])
srv.auto_trust(host, a, m, "newthing")
check("auto: the same prompt is never answered twice (a write is not retried)", len(pressed) == 1)
reset(); screen["t"] = TRUST.replace("❯ 1. Yes, I trust this folder", "  1. Yes, I trust this folder").replace("   2. No, exit", " ❯ 2. No, exit")
a = idle(); m = srv.apply_trust(host, a); srv.auto_trust(host, a, m, "x")
check("auto: with the cursor on 'No, exit' it moves UP to the trusting option first", pressed == [["pane", "send-keys", "w1:p1", "up", "enter"]])
reset(); screen["t"] = TRUST; srv.AUTOTRUST = False; a = idle(); m = srv.apply_trust(host, a); srv.auto_trust(host, a, m, "x")
check("auto: SHEPR_AUTOTRUST=0 still shows the prompt but presses nothing", m and a["agent_status"] == "blocked" and pressed == [])
srv.AUTOTRUST = True
reset(); screen["t"] = PERM; a = idle(); srv.apply_trust(host, a)
check("auto: a permission dialog is never answered by Shepr", pressed == [])
reset(); screen["t"] = HOOK; a = idle(); srv.apply_trust(host, a)
check("auto: a hook confirmation (destructive command) is never answered by Shepr", pressed == [] and a["agent_status"] == "idle")

# ---- get_agent (what every phone answer goes through) sees the dialog too, so the tap is accepted
reset(); screen["t"] = TRUST
srv.run_herdr = lambda machine, args, **k: {"result": {"agent": idle()}}
g = srv.get_agent(host, "w1:p1")
check("get_agent: a trust prompt reads as blocked, so choosing '1. Yes' is not refused as not_blocked", g["agent_status"] == "blocked")
srv.run_herdr = fake_run

# ---- a tap on a blocked dialog survives herdr moving its counter while the same dialog is up
srv.READONLY = False
srv.need_host = lambda h: host
state = {"seq": 7, "status": "blocked", "sess": "s1"}
srv.get_agent = lambda h, pane: {"agent": "claude", "agent_status": state["status"], "state_change_seq": state["seq"], "agent_session": {"value": state["sess"]}, "cwd": "/tmp/x"}
sent = []
srv.run_step = lambda h, pane, step: sent.append(dict(step))
def commit(before, after, body, **after_state):
    sent.clear(); srv.LAST_SENT.clear(); state.update(seq=7, status="blocked", sess="s1"); screen["t"] = before
    p = srv.build_plan(dict(body, host="gus", pane="w1:p1"))
    screen["t"] = after; state.update(after_state)
    try: srv.commit_plan(p["nonce"]); return None
    except srv.HerdrError as e: return e.code
Q = HOOK
r = commit(Q, Q, {"kind": "choose", "option": 1}, seq=9)
check("hook dialog: tapping 1 after the counter moved (same dialog) is sent", r is None and sent == [{"op": "keys", "keys": ["enter"]}])
r = commit(Q, Q, {"kind": "choose", "option": 2}, seq=9)
check("hook dialog: tapping 2 after the counter moved is sent", r is None and sent == [{"op": "keys", "keys": ["down", "enter"]}])
r = commit(Q, Q, {"kind": "keys", "keys": ["1"]}, seq=9)
check("hook dialog: a key press (1) after the counter moved is sent", r is None and sent == [{"op": "keys", "keys": ["1"]}])
r = commit(Q, Q.replace("Do you want to proceed?", "Allow this other thing?"), {"kind": "choose", "option": 1}, seq=9)
check("moved counter + a DIFFERENT question: still refused, nothing sent", r == "moved_on" and sent == [])
r = commit(Q, Q, {"kind": "choose", "option": 1}, seq=9, status="working")
check("moved counter + the agent no longer blocked: still refused", r == "moved_on" and sent == [])
r = commit(Q, Q, {"kind": "choose", "option": 1}, seq=9, sess="s2")
check("moved counter + another session in the pane: still refused", r == "moved_on" and sent == [])
r = commit(Q, Q, {"kind": "choose", "option": 1})
check("unchanged counter: sent as before", r is None and sent == [{"op": "keys", "keys": ["enter"]}])
r = commit("ready\n> ", "ready\n> ", {"kind": "text", "text": "hi"}, seq=9, status="idle")
check("a message to an idle agent whose counter moved is still refused", r == "moved_on" and sent == [])

print("ALL OK" if ok else "SOMETHING FAILED"); sys.exit(0 if ok else 1)
