#!/usr/bin/env python3
"""The pairing QR carries a one-time code, so it is handled like a secret (a marketplace reviewer asked for this on
2026-10-12): the code reaches qrencode over stdin and never in its argument list, and the picture is born owner-only
in a private directory the CLI has verified (never /tmp), with or without $XDG_RUNTIME_DIR. A fake qrencode records
exactly what it was given. Needs no server and no phone. Run: python3 tests/qr_secret_test.py"""
import json, os, stat, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.realpath(os.path.join(HERE, "..", "server.py"))
bad = 0


def check(name, ok, extra=""):
    global bad
    bad += 0 if ok else 1
    print(("PASS " if ok else "FAIL ") + name + ((" :: " + str(extra)[:300]) if (extra and not ok) else ""))


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def world():
    """A throwaway home with a state directory and a fake qrencode that logs argv and stdin."""
    root = tempfile.mkdtemp(prefix="qrsecret-")
    home = os.path.join(root, "home"); state = os.path.join(root, "state"); fake = os.path.join(root, "bin"); log = os.path.join(root, "log")
    for d in (home, fake, log):
        os.makedirs(d)
    os.makedirs(state, mode=0o700)
    with open(os.path.join(state, "server.json"), "w") as f:
        json.dump({"pid": os.getpid(), "port": 1, "url": "http://127.0.0.1:1", "started": int(time.time())}, f)
    qr = os.path.join(fake, "qrencode")
    with open(qr, "w") as f:
        f.write("#!/usr/bin/env python3\nimport json, os, sys\nL = os.environ['QR_LOG']\n"
                "n = len([x for x in os.listdir(L) if x.startswith('argv')])\n"
                "open(os.path.join(L, f'argv{n}'), 'w').write(json.dumps(sys.argv[1:]))\n"
                "A = sys.argv\n"
                "i, text = 1, None\n"
                "while i < len(A):   # like the real one: the string is the one argument that is not an option or an option's value\n"
                "    if A[i] in ('-o', '-s', '-m', '-l', '-t'): i += 2\n"
                "    elif A[i].startswith('-') and A[i] != '-': i += 1\n"
                "    else: text = A[i]; i += 1\n"
                "open(os.path.join(L, f'stdin{n}'), 'w').write(sys.stdin.read() if text is None else '')\n"
                "if '-t' in A: print('[ascii qr]')\n"
                "else:\n"
                "    out = A[A.index('-o') + 1]\n"
                "    if out == '-': sys.stdout.buffer.write(b'\\x89PNG\\r\\n\\x1a\\nFAKE')\n"
                "    else: open(out, 'wb').write(b'\\x89PNG\\r\\n\\x1a\\nFAKE')   # like the real one: a plain file, default permissions\n")
    os.chmod(qr, 0o755)
    return {"root": root, "home": home, "state": state, "fake": fake, "log": log}


def pair(w, *args, xdg="unset", umask=None):
    env = {"PATH": w["fake"] + ":/usr/bin:/bin", "HOME": w["home"], "SHEPR_STATE": w["state"], "SHEPR_LOCAL_ONLY": "1",
           "SHEPR_URL": "http://127.0.0.1:1", "QR_LOG": w["log"]}
    if xdg != "unset":
        env["XDG_RUNTIME_DIR"] = xdg
    pre = (lambda: os.umask(umask)) if umask is not None else None
    return subprocess.run([sys.executable, "-I", SERVER, "pair", "--no-wait", "--json", *args], env=env, capture_output=True,
                          text=True, timeout=60, preexec_fn=pre)


def logged(w, n=0):
    """What the fake qrencode was given: (argv, stdin). Empty when it was never run."""
    try:
        with open(os.path.join(w["log"], f"argv{n}")) as f:
            argv = json.load(f)
        with open(os.path.join(w["log"], f"stdin{n}")) as f:
            return argv, f.read()
    except OSError:
        return ["-o", "(qrencode never ran)"], ""


def result(r):
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}


# 1. XDG_RUNTIME_DIR present and private: the picture goes in <it>/shepr, owner-only, and the code never touches argv
w = world(); xdg = os.path.join(w["root"], "run"); os.makedirs(xdg, mode=0o700)
r = pair(w, "--png", xdg=xdg); out = result(r)
argv, stdin = logged(w)
code = out.get("code", "?"); png = out.get("png") or ""
check("--png: exits 0 and says where the picture is", r.returncode == 0 and png.startswith(os.path.join(os.path.realpath(xdg), "shepr") + os.sep), (r.returncode, r.stderr, out))
check("--png: the code and the URL are NOT in qrencode's argument list", all("shepr-pair" not in a and code not in a and "127.0.0.1" not in a for a in argv), argv)
check("--png: the URL with the one-time code arrives on qrencode's stdin", "#shepr-pair=" + code in stdin, stdin)
check("--png: nothing asked qrencode to write a file (-o is -)", "-o" in argv and argv[argv.index("-o") + 1] == "-", argv)
check("--png: the picture is owner-only (0600) and the directory is 0700", png and mode(png) == 0o600 and mode(os.path.dirname(png)) == 0o700, (png and oct(mode(png))))
check("--png: the picture holds what qrencode drew", png and open(png, "rb").read().startswith(b"\x89PNG"))

# 2. umask 0 cannot widen it
w = world(); xdg = os.path.join(w["root"], "run"); os.makedirs(xdg, mode=0o700)
out = result(pair(w, "--png", xdg=xdg, umask=0)); png = out.get("png") or ""
check("umask 0: picture still 0600, folder still 0700", png and mode(png) == 0o600 and mode(os.path.dirname(png)) == 0o700, png and oct(mode(png)))

# 3. no XDG_RUNTIME_DIR at all: the state directory's own run folder, never /tmp
w = world()
r = pair(w, "--png"); out = result(r); png = out.get("png") or ""
want = os.path.join(os.path.realpath(w["state"]), "run")
check("no XDG_RUNTIME_DIR: the picture lands in <state>/run, not /tmp", r.returncode == 0 and os.path.dirname(png) == want and not png.startswith("/tmp/shepr"), (r.returncode, r.stderr, out))
check("no XDG_RUNTIME_DIR: the picture is 0600 and <state>/run is 0700", png and mode(png) == 0o600 and mode(want) == 0o700)
argv, stdin = logged(w)
check("no XDG_RUNTIME_DIR: the code is still only on stdin", "#shepr-pair=" + out.get("code", "?") in stdin and not any("shepr-pair" in a for a in argv), argv)

# 4. XDG_RUNTIME_DIR that other users can enter is not trusted: fall back, and leave it exactly as it was
w = world(); loose = os.path.join(w["root"], "loose"); os.makedirs(loose); os.chmod(loose, 0o755)
r = pair(w, "--png", xdg=loose); out = result(r); png = out.get("png") or ""
check("open XDG_RUNTIME_DIR (0755): not used, falls back to <state>/run", r.returncode == 0 and os.path.dirname(png) == os.path.join(os.path.realpath(w["state"]), "run"), (r.stderr, out))
check("open XDG_RUNTIME_DIR: left untouched (no shepr folder, still 0755)", not os.path.exists(os.path.join(loose, "shepr")) and mode(loose) == 0o755)

# 5. a relative or missing XDG_RUNTIME_DIR is ignored
w = world()
out = result(pair(w, "--png", xdg="relative/dir")); png = out.get("png") or ""
check("relative XDG_RUNTIME_DIR: ignored", png.startswith(os.path.join(os.path.realpath(w["state"]), "run")), out)
w = world()
out = result(pair(w, "--png", xdg=os.path.join(w["root"], "gone"))); png = out.get("png") or ""
check("XDG_RUNTIME_DIR that does not exist: ignored", png.startswith(os.path.join(os.path.realpath(w["state"]), "run")), out)

# 6. a looser folder of ours is tightened before anything secret is written
w = world(); run = os.path.join(w["state"], "run"); os.makedirs(run); os.chmod(run, 0o755); os.chmod(w["state"], 0o755)
out = result(pair(w, "--png")); png = out.get("png") or ""
check("a 0755 run folder and a 0755 state folder of ours are tightened to 0700", png and mode(run) == 0o700 and mode(w["state"]) == 0o700, (oct(mode(run)), oct(mode(w["state"]))))

# 7. a symlink planted where the picture goes is replaced, never followed
w = world(); run = os.path.join(w["state"], "run"); os.makedirs(run, mode=0o700)
victim = os.path.join(w["root"], "victim.txt"); open(victim, "w").write("keep me")
os.symlink(victim, os.path.join(run, "pair-qr.png"))
r = pair(w, "--png"); out = result(r); png = out.get("png") or ""
check("planted symlink: the victim file is untouched", open(victim).read() == "keep me", open(victim).read())
check("planted symlink: the picture is a real 0600 file now", png and os.path.isfile(png) and not os.path.islink(png) and mode(png) == 0o600, (r.stderr, out))

# 8. a run folder that is itself a symlink is not trusted: fail closed, write nothing there
w = world(); target = os.path.join(w["root"], "elsewhere"); os.makedirs(target, mode=0o700)
os.symlink(target, os.path.join(w["state"], "run"))
r = pair(w, "--png")
check("run folder is a symlink: refuses, says why, exit != 0", r.returncode != 0 and "no private directory" in r.stderr and "Traceback" not in r.stderr, (r.returncode, r.stderr))
check("run folder is a symlink: nothing was written through it", os.listdir(target) == [], os.listdir(target))

# 9. --png FILE: owner-only whatever the umask, replaces a symlink rather than following it
w = world(); dest = os.path.join(w["root"], "mine.png"); victim = os.path.join(w["root"], "other.txt"); open(victim, "w").write("keep me")
os.symlink(victim, dest)
r = pair(w, "--png", dest, umask=0); out = result(r)
check("--png FILE: written at that path, 0600 under umask 0, symlink replaced", r.returncode == 0 and out.get("png") == dest and os.path.isfile(dest) and not os.path.islink(dest) and mode(dest) == 0o600 and open(victim).read() == "keep me", (r.stderr, out))
argv, stdin = logged(w)
check("--png FILE: the code is on stdin here too", "#shepr-pair=" in stdin and not any("shepr-pair" in a for a in argv), argv)

# 10. a directory where the picture should go: a clean error, not a traceback
w = world(); d = os.path.join(w["root"], "adir"); os.makedirs(d)
r = pair(w, "--png", d)
check("--png at a directory: clean error, exit != 0", r.returncode != 0 and "Traceback" not in r.stderr and "shepr:" in r.stderr, (r.returncode, r.stderr))

# 11. the terminal QR (no --png) also keeps the code out of argv
w = world()
r = subprocess.run([sys.executable, "-I", SERVER, "pair", "--no-wait"],
                   env={"PATH": w["fake"] + ":/usr/bin:/bin", "HOME": w["home"], "SHEPR_STATE": w["state"], "SHEPR_LOCAL_ONLY": "1",
                        "SHEPR_URL": "http://127.0.0.1:1", "QR_LOG": w["log"]}, capture_output=True, text=True, timeout=60)
argv, stdin = logged(w)
check("terminal QR: draws it, code on stdin only", r.returncode == 0 and "#shepr-pair=" in stdin and not any("shepr-pair" in a or "127.0.0.1" in a for a in argv), (r.returncode, r.stderr, argv))

# 12. cancel removes the code AND the picture, from either place
w = world(); xdg = os.path.join(w["root"], "run"); os.makedirs(xdg, mode=0o700)
out = result(pair(w, "--png", xdg=xdg)); png = out.get("png") or ""
env = {"PATH": "/usr/bin:/bin", "HOME": w["home"], "SHEPR_STATE": w["state"], "XDG_RUNTIME_DIR": xdg}
r = subprocess.run([sys.executable, "-I", SERVER, "pair", "--cancel"], env=env, capture_output=True, text=True, timeout=60)
check("pair --cancel: the code and the XDG picture are gone", r.returncode == 0 and png and not os.path.exists(png) and not os.path.exists(os.path.join(w["state"], "pairing.json")), (r.stdout, r.stderr))
w = world()
out = result(pair(w, "--png")); png = out.get("png") or ""
env = {"PATH": "/usr/bin:/bin", "HOME": w["home"], "SHEPR_STATE": w["state"]}
r = subprocess.run([sys.executable, "-I", SERVER, "pair", "--cancel"], env=env, capture_output=True, text=True, timeout=60)
check("pair --cancel: the fallback picture is gone too", r.returncode == 0 and png and not os.path.exists(png), (r.stdout, r.stderr))
r = subprocess.run([sys.executable, "-I", SERVER, "pair", "--cancel"], env=env, capture_output=True, text=True, timeout=60)
check("pair --cancel with nothing waiting still succeeds", r.returncode == 0 and "no pairing code" in r.stdout, (r.returncode, r.stdout, r.stderr))

# 12b. wait mode (no --no-wait): the picture of a dead code does not outlive it. The plugin runs --no-wait, a person at a
# terminal may not. Paired, Ctrl-C and a picture the user named themselves are the three ways it ends.
import signal


def wait_pair(w, *args):
    env = {"PATH": w["fake"] + ":/usr/bin:/bin", "HOME": w["home"], "SHEPR_STATE": w["state"], "SHEPR_LOCAL_ONLY": "1",
           "SHEPR_URL": "http://127.0.0.1:1", "QR_LOG": w["log"]}
    p = subprocess.Popen([sys.executable, "-I", SERVER, "pair", "--json", "--ttl", "30", *args], env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    try:
        return p, json.loads(p.stdout.readline())   # the --json line is printed before it starts waiting
    except ValueError:
        p.kill(); p.wait()
        return p, {}


def finish(p, how):
    if how == "sigint":
        p.send_signal(signal.SIGINT)
    try:
        p.wait(timeout=20)
    except subprocess.TimeoutExpired:
        p.kill(); p.wait()
        return None
    return p.returncode


w = world()
p, out = wait_pair(w, "--png"); png = out.get("png") or ""
alive = bool(png) and os.path.isfile(png)
pf = os.path.join(w["state"], "pairing.json"); d = json.load(open(pf)); d["used_by"] = "phone-1"; json.dump(d, open(pf, "w"))
rc = finish(p, "paired")
check("wait mode, phone pairs: the private picture existed while waiting", alive, out)
check("wait mode, phone pairs: exit 0 and the picture is gone", rc == 0 and not os.path.exists(png), (rc, png))

w = world()
p, out = wait_pair(w, "--png"); png = out.get("png") or ""
alive = bool(png) and os.path.isfile(png)
rc = finish(p, "sigint")
check("wait mode, Ctrl-C: the private picture existed while waiting", alive, out)
check("wait mode, Ctrl-C: exit 1, the picture and the code are gone", rc == 1 and not os.path.exists(png) and not os.path.exists(os.path.join(w["state"], "pairing.json")), (rc, png))

w = world(); mine = os.path.join(w["root"], "mine.png")
p, out = wait_pair(w, "--png", mine)
alive = os.path.isfile(mine)
rc = finish(p, "sigint")
check("wait mode with --png FILE: a picture the user named stays (it is theirs)", alive and rc == 1 and os.path.isfile(mine), (rc, out))

# 13. the plugin itself: no /tmp fallback, no secret in an argument, no rm
qml = open(os.path.join(HERE, "..", "Service.qml")).read()
check("Service.qml: no /tmp fallback and no hand-built picture path", '"/tmp"' not in qml and "pairPngPath" not in qml and "runtimeDir" not in qml)
check("Service.qml: asks for the private picture (--png with no file name)", '"pair", "--png", "--json"' in qml)
check("Service.qml: the QR cleanup is the CLI's cancel, not an rm of a guessed path", '"rm"' not in qml and '"pair", "--cancel"' in qml)

print(f"\n{'ALL PASS' if not bad else str(bad) + ' FAILED'}")
sys.exit(1 if bad else 0)
