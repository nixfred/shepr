"""plan_open_folder / folders_view: the plan types `cd <folder> && <agent>` into a terminal, so the folder name, the agent and
the mode are validated hard, a folder is never created, and a projects path with a space or quote stays ONE shell word.
herdr is stubbed.  Run: python3 tests/open_folder_test.py"""
import importlib.util, os, shlex, sys, tempfile, time
tmp = tempfile.mkdtemp()
os.environ["SHEPR_STATE"] = os.path.join(tmp, "state")
os.environ["CLAUDE_HOME"] = os.path.join(tmp, "claude")
os.environ["CODEX_HOME"] = os.path.join(tmp, "codex")
for _k in ("SHEPR_NEW_CMD", "SHEPR_CLAUDE_CMD", "SHEPR_CODEX_CMD", "SHEPR_PUSH_ALLOW", "SHEPR_READONLY"): os.environ.pop(_k, None)
proj = os.path.join(tmp, "my proj's")            # a space and an apostrophe in the projects folder
os.makedirs(proj)
os.environ["SHEPR_PROJECTS"] = proj
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

HOST = {"id": "gus", "kind": "local", "machine": None, "enabled": True}
REMOTE = {"id": "vic", "kind": "remote", "machine": "vic", "enabled": True}
srv.need_host = lambda hid: {"gus": HOST, "vic": REMOTE}.get(hid) or (_ for _ in ()).throw(srv.HerdrError("bad_host", hid))
panes = {"panes": []}; wss = {"workspaces": []}
def fake_run(machine, args, timeout=10, want_json=True):
    if args[:2] == ["pane", "list"]: return {"result": panes}
    if args[:2] == ["workspace", "list"]: return {"result": wss}
    raise srv.HerdrError("herdr_failed", "unexpected " + " ".join(args))
srv.run_herdr = fake_run

os.makedirs(os.path.join(proj, "alpha")); os.makedirs(os.path.join(proj, "beta.d")); os.makedirs(os.path.join(proj, ".hidden")); os.makedirs(os.path.join(proj, "trail."))
open(os.path.join(proj, "afile"), "w").close()
before = sorted(os.listdir(proj))
body = lambda **k: dict({"host": "gus", "dir": "alpha", "agent": "claude", "mode": "fresh"}, **k)

p = srv.plan_open_folder(body())
plan = srv.PLANS[p["nonce"]]
step = plan["steps"][0]
check("a valid fresh claude plan registers one new_project step with the real folder as cwd", plan["kind"] == "open_folder" and step["op"] == "new_project" and step["cwd"] == os.path.join(proj, "alpha") and step["label"] == "alpha" and p["approves"] is False)
words = shlex.split(step["cmd"])
check("the typed command is `cd <folder> && claude` and the folder survives as ONE word despite space+apostrophe in the path", words[0] == "cd" and words[1] == os.path.join(proj, "alpha") and words[2:] == ["&&", "claude"])
check("planning never created, removed or renamed anything in the projects folder", sorted(os.listdir(proj)) == before)

for bad in ("../etc", "a/b", ".hidden", "-rf", "a b", "x;rm", "a$(id)", "trail.", "", "A" * 81, "zz\nid"):
    check(f"folder {bad!r} is refused as bad_dir", code_of(lambda b=bad: srv.plan_open_folder(body(dir=b))) == "bad_dir")
check("a non-string folder is refused", code_of(lambda: srv.plan_open_folder(body(dir=["alpha"]))) == "bad_dir")
check("a missing folder is refused and not created", code_of(lambda: srv.plan_open_folder(body(dir="ghost"))) == "no_folder" and not os.path.exists(os.path.join(proj, "ghost")))
check("a plain FILE with a valid name is not a folder", code_of(lambda: srv.plan_open_folder(body(dir="afile"))) == "no_folder")
check("unknown agent is refused", code_of(lambda: srv.plan_open_folder(body(agent="bash"))) == "bad_agent")
check("unknown mode is refused", code_of(lambda: srv.plan_open_folder(body(mode="yolo"))) == "bad_mode")
check("a missing mode is refused", code_of(lambda: srv.plan_open_folder({k: v for k, v in body().items() if k != "mode"})) == "bad_mode")
check("a remote host is refused (local only)", code_of(lambda: srv.plan_open_folder(body(host="vic"))) == "local_only")
check("an unknown host is refused", code_of(lambda: srv.plan_open_folder(body(host="zzz"))) == "bad_host")
for bad in ("two\nlines", "bell\x07", "", "x" * 61, 5, ["a"]):
    check(f"project name {bad!r} is refused", code_of(lambda b=bad: srv.plan_open_folder(body(name=b))) == "bad_name")
check("a name is whitespace-normalised", srv.PLANS[srv.plan_open_folder(body(name="  my   name "))["nonce"]]["steps"][0]["label"] == "my name")

# continue needs a recorded conversation
check("continue with no recorded conversation is refused", code_of(lambda: srv.plan_open_folder(body(mode="continue"))) == "nothing_to_resume")
conv = os.path.join(srv.CLAUDE_HOME, "projects", __import__("re").sub(r"[^A-Za-z0-9]", "-", os.path.realpath(os.path.join(proj, "alpha"))))
os.makedirs(conv); open(os.path.join(conv, "s.jsonl"), "w").write("{}\n")
p = srv.plan_open_folder(body(mode="continue"))
check("continue with a conversation types `claude --continue`", shlex.split(srv.PLANS[p["nonce"]]["steps"][0]["cmd"])[-2:] == ["claude", "--continue"])
check("codex fresh types the codex command, codex continue with no session is refused", shlex.split(srv.PLANS[srv.plan_open_folder(body(agent="codex"))["nonce"]]["steps"][0]["cmd"])[-1] == "codex" and code_of(lambda: srv.plan_open_folder(body(agent="codex", mode="continue"))) == "nothing_to_resume")

# heads-up lines
wss["workspaces"] = [{"label": "alpha"}]; panes["panes"] = [{"cwd": os.path.join(proj, "alpha", "sub")}]
vals = [l["v"] for l in srv.plan_open_folder(body())["lines"] if l["k"] == "Heads up"]
check("a same-named workspace and an already-open terminal each produce a heads-up", len(vals) == 2)
def boom(*a, **k): raise srv.HerdrError("herdr_failed", "down")
srv.run_herdr = boom
check("if herdr cannot be read the plan is still made (heads-ups are best effort)", srv.plan_open_folder(body())["kind"] == "open_folder")

# folders_view
srv.run_herdr = fake_run; panes["panes"] = [{"cwd": os.path.join(proj, "beta.d")}]
v = srv.folders_view(HOST)
names = [i["dir"] for i in v["items"]]
check("folders_view lists plain folders only, hides dot/trailing-dot names and files", names == ["alpha"] and v["open"] == 1 and v["total"] == 1)
check("folders_view refuses a remote host", code_of(lambda: srv.folders_view(REMOTE)) == "local_only")
srv.run_herdr = boom
check("folders_view offers every folder when herdr cannot be read", sorted(i["dir"] for i in srv.folders_view(HOST)["items"]) == ["alpha", "beta.d"])
print("OK" if ok else "FAILED"); sys.exit(0 if ok else 1)
