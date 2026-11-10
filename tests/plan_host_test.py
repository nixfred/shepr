"""plan_host / commit_host: enabling, disabling and adding herdr machines and revoking a phone. Revoke signs a phone out, so
it must really remove the key and its push subscription; a failed machine write must say it may still have landed.
herdr is stubbed; nothing leaves the temp state dir.  Run: python3 tests/plan_host_test.py"""
import importlib.util, json, os, sys, tempfile
os.environ["SHEPR_STATE"] = tempfile.mkdtemp()
for _k in ("SHEPR_NEW_CMD", "SHEPR_CLAUDE_CMD", "SHEPR_CODEX_CMD", "SHEPR_PROJECTS", "SHEPR_PUSH_ALLOW", "SHEPR_READONLY"): os.environ.pop(_k, None)
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

calls, polls = [], []
fail = {"on": None}
def fake_run(machine, args, timeout=10, want_json=True):
    calls.append((machine, list(args)))
    if args[:2] == ["machine", "list"]: return [{"id": "m-1", "label": "vic", "target": "vic"}, {"id": "m-2", "label": "mind", "target": "mind"}]
    if fail["on"]: raise srv.HerdrError("herdr_failed", fail["on"])
    return {}
srv.run_herdr = fake_run
srv.poll_all = lambda *a, **k: polls.append(1)
def audits():
    try: return [json.loads(l) for l in open(srv.state_path("sends.jsonl"))]
    except OSError: return []

# ---- enable / disable
p = srv.plan_host({"kind": "host_disable", "id": "vic"})
check("disable: step is `machine disable <id>` with the saved machine's id, nothing sent yet", p["kind"] == "host_disable" and srv.PLANS[p["nonce"]]["steps"] == [{"op": "machine", "args": ["disable", "m-1"]}] and not [c for c in calls if c[1][:1] == ["machine"] and c[1][1] != "list"])
check("a plan never approves an agent", p["approves"] is False)
check("enable uses `enable`", srv.plan_host({"kind": "host_enable", "id": "mind"}) and srv.PLANS[list(srv.PLANS)[-1]]["steps"][0]["args"] == ["enable", "m-2"])
check("an unknown machine label is refused", code_of(lambda: srv.plan_host({"kind": "host_enable", "id": "nope"})) == "bad_host")
check("a non-string id cannot smuggle itself in", code_of(lambda: srv.plan_host({"kind": "host_enable", "id": ["vic"]})) == "bad_host")
check("an unknown kind is refused", code_of(lambda: srv.plan_host({"kind": "host_delete"})) == "bad_kind")

# ---- add: only from the last scan, only ready peers, label must be plain
srv.SCAN["peers"] = [{"name": "box", "state": "ready", "target": "box", "ip": "100.1.1.1"}, {"name": "cold", "state": "no_herdr", "target": "cold", "ip": "100.1.1.2"}]
p = srv.plan_host({"kind": "host_add", "name": "box"})
check("add: label defaults to the lower-cased peer name and the step is `machine add <target> --label`", srv.PLANS[p["nonce"]]["steps"][0]["args"] == ["add", "box", "--label", "box"])
check("add: a peer that is not ready is refused", code_of(lambda: srv.plan_host({"kind": "host_add", "name": "cold"})) == "bad_host")
check("add: a name the scan never found is refused", code_of(lambda: srv.plan_host({"kind": "host_add", "name": "ghost"})) == "bad_host")
for bad in ("-rf", "a b", "x;y", "A" * 40, "1abc"):
    check(f"add: label {bad!r} is refused", code_of(lambda b=bad: srv.plan_host({"kind": "host_add", "name": "box", "label": b})) == "bad_label")
check("add: an upper-case label is lower-cased, then accepted", srv.PLANS[srv.plan_host({"kind": "host_add", "name": "box", "label": "Box2"})["nonce"]]["steps"][0]["args"][-1] == "box2")

# ---- revoke
JWK = {"x": srv.b64u(b"\x01" * 32), "y": srv.b64u(b"\x02" * 32), "name": "Fred's iPhone", "added": 1, "last_seen": 1}
srv.mutate_devices(lambda d: d.update({"dev1": dict(JWK), "dev2": dict(JWK, name="iPad")}))
srv.PUSH["dev1"] = {"sub": {"endpoint": "https://x"}, "prefs": {}}; srv.PUSH["dev2"] = {"sub": {"endpoint": "https://y"}, "prefs": {}}
p = srv.plan_host({"kind": "device_revoke", "id": "dev1"})
check("revoke: the preview names the phone", any("iPhone" in l["v"] for l in p["lines"]) and "dev1" in srv.devices())
check("revoke: an unknown device is refused", code_of(lambda: srv.plan_host({"kind": "device_revoke", "id": "ghost"})) == "bad_device")
r = srv.commit_host(srv.PLANS[p["nonce"]])
check("revoke: commit returns ok", r == {"ok": True, "sent": ["revoke"]})
check("revoke: the key is gone from memory AND from disk", "dev1" not in srv.devices() and "dev1" not in json.load(open(srv.state_path("devices.json"))))
check("revoke: only that phone is gone", "dev2" in srv.devices())
check("revoke: its push subscription is dropped too, the other phone's is kept", "dev1" not in srv.PUSH and "dev2" in srv.PUSH)
check("revoke: it is audited", any(a["kind"] == "device_revoke" and a["ok"] for a in audits()))
check("revoke: devices.json stays private (0600)", os.stat(srv.state_path("devices.json")).st_mode & 0o777 == 0o600)
check("revoking an already-revoked phone does not raise", srv.commit_host({"kind": "device_revoke", "host": None, "pane": None, "steps": [{"op": "revoke", "id": "dev1"}]})["ok"])

# ---- commit of a machine write
calls.clear()
r = srv.commit_host(srv.PLANS[srv.plan_host({"kind": "host_disable", "id": "vic"})["nonce"]])
check("machine commit: runs `herdr machine disable m-1` locally, once, then refreshes the poll", [c for c in calls if c[1][0] == "machine" and c[1][1] != "list"] == [(None, ["machine", "disable", "m-1"])] and polls == [1] and r["ok"])
fail["on"] = "ssh refused"; polls.clear()
e = None
try: srv.commit_host(srv.PLANS[srv.plan_host({"kind": "host_enable", "id": "vic"})["nonce"]])
except srv.HerdrError as x: e = x
check("machine commit failure: herdr's code is kept and the message warns the write may have landed", e and e.code == "herdr_failed" and "ssh refused" in e.message and "may still have landed" in e.message)
check("machine commit failure: audited as failed, and no poll refresh", audits()[-1]["ok"] is False and "ssh refused" in audits()[-1]["err"] and polls == [])
print("OK" if ok else "FAILED"); sys.exit(0 if ok else 1)
