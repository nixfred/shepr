#!/usr/bin/env python3
"""`shepr pair` in WAIT mode: shows a code, waits, and reports the device that redeems it.
Needs a running test server (see tests/ui.mjs header). Generates a real P-256 key with openssl."""
import json, os, subprocess, sys, tempfile, time, urllib.request, base64
HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "server.py")
ENV = dict(os.environ, SHEPR_STATE=os.environ.get("SHEPR_STATE", "/tmp/claude-1000/shepr-state"), SHEPR_URL="http://localhost:8743")
# Regression (2026-10-08): the QR must carry the tailnet name, not 127.0.0.1. Run WITHOUT SHEPR_URL, which is what masked it.
import importlib.util
os.environ.pop("SHEPR_URL", None)
spec = importlib.util.spec_from_file_location("shepr_server", SERVER); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.learn_tailnet()
url = m.public_url(8742)
good = url.startswith("https://") and "127.0.0.1" not in url and ".ts.net:8742" in url
print(("PASS" if good else "FAIL"), "pairing URL uses the tailnet name:", url)

# The hint names the user unit that runs THIS server.py. It must not invent shepr, or one nixweb session name.
def pair_err(home):
    r = subprocess.run([sys.executable, SERVER, "pair", "--no-wait"],
                       env=dict(os.environ, SHEPR_STATE=tempfile.mkdtemp(), HOME=home),
                       capture_output=True, text=True)
    return r.returncode != 0, (r.stderr or "").strip()

def write_unit(home, unit, body):
    d = os.path.join(home, ".config", "systemd", "user")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, unit), "w") as f:
        f.write(body)

me = os.path.realpath(SERVER)
stopped, err = pair_err(tempfile.mkdtemp())
bare = stopped and "shepr.service" in err and "nixweb-<session>" in err and "systemctl --user start" not in err
print(("PASS" if bare else "FAIL"), "pair: with no unit installed, both install styles are named and neither is ordered:", err)

home = tempfile.mkdtemp()
write_unit(home, "shepr.service", f"ExecStart=/usr/bin/python3 {me} 8742\n")
write_unit(home, "other.service", "ExecStart=/usr/bin/python3 /tmp/elsewhere/server.py 1\n")
stopped, err = pair_err(home)
readme = stopped and err.endswith("systemctl --user start shepr") and "elsewhere" not in err
print(("PASS" if readme else "FAIL"), "pair: a README unit that runs this file is the one named:", err)

home = tempfile.mkdtemp()
link = os.path.join(home, "shepr-link")
os.symlink(me, link)
write_unit(home, "nixweb-herder.remote.iphone.service", f"ExecStart=/usr/bin/python3 {link} 8742\n")
stopped, err = pair_err(home)
nixweb = stopped and err.endswith("systemctl --user start nixweb-herder.remote.iphone") and "shepr.service" not in err
print(("PASS" if nixweb else "FAIL"), "pair: a nixweb unit is named, including when its path is a symlink:", err)
hint = bare and readme and nixweb

# CLI wait-mode needs a running test server on :8743 (see tests/ui.mjs header); skip cleanly without one.
try:
    urllib.request.urlopen("http://localhost:8743/api/hello", timeout=2)
    have_server = True
except Exception:
    have_server = False
if not have_server:
    print("SKIP pair CLI wait-mode test: no test server on :8743")
    sys.exit(0 if good and hint else 1)

d = tempfile.mkdtemp()
subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", d + "/k.pem"], check=True, capture_output=True)
raw = subprocess.run(["openssl", "ec", "-in", d + "/k.pem", "-pubout", "-outform", "DER"], check=True, capture_output=True).stdout[-64:]
b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
jwk = {"kty": "EC", "crv": "P-256", "x": b64u(raw[:32]), "y": b64u(raw[32:])}

p = subprocess.Popen([sys.executable, SERVER, "pair", "--json", "--ttl", "60"], env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
info = json.loads(p.stdout.readline())
req = urllib.request.Request("http://localhost:8743/api/pair", method="POST", headers={"Content-Type": "application/json", "X-Shepr": "1"},
                             data=json.dumps({"code": info["code"], "jwk": jwk, "name": "cli-test"}).encode())
res = json.load(urllib.request.urlopen(req))
out, err = p.communicate(timeout=10)
ok = p.returncode == 0 and "paired" in out and "cli-test" in out and res["fp"] in out
print(("PASS" if ok else "FAIL"), "pair CLI waits, then reports the paired device:", out.strip().splitlines()[-1] if out.strip() else err)
sys.exit(0 if good and hint and (not have_server or ok) else 1)
