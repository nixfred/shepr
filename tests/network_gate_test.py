"""Handler.network_gate, layer 1 of every /api call: Host allow-list, Funnel refusal, tailnet-owner check, cross-origin
and content-type rules for writes.  Driven with a fake request object: no socket, no herdr.
Run: python3 tests/network_gate_test.py"""
import importlib.util, os, sys, tempfile
os.environ["SHEPR_STATE"] = tempfile.mkdtemp()
for _k in ("SHEPR_NEW_CMD", "SHEPR_CLAUDE_CMD", "SHEPR_CODEX_CMD", "SHEPR_PROJECTS", "SHEPR_PUSH_ALLOW", "SHEPR_READONLY"): os.environ.pop(_k, None)
here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("shepr_server", os.path.join(here, "..", "server.py"))
srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)
ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name); ok = ok and bool(cond)

class Hdrs(dict):
    """Case-insensitive get, like http.client.HTTPMessage."""
    def get(self, k, d=None):
        for key, v in self.items():
            if key.lower() == k.lower(): return v
        return d
class Fake:
    def __init__(self, headers, path="/api/queue"):
        self.headers, self.path, self.out = Hdrs(headers), path, None
    def json_out(self, code, obj): self.out = (code, obj.get("error"))
def gate(headers, post=False, path="/api/queue"):
    f = Fake(headers, path)
    r = srv.Handler.network_gate(f, post)
    return r, f.out

srv.OWNER["login"] = "fred@example.com"
srv.OWNER["names"] = {"localhost", "127.0.0.1", "[::1]", "gus.tailnet.ts.net"}
JSON = {"X-Shepr": "1", "Content-Type": "application/json"}

check("GET from a known Host and no login header passes as 'local'", gate({"Host": "127.0.0.1:8743"}) == ("local", None))
check("the port is ignored when matching the Host", gate({"Host": "localhost:9999"})[0] == "local")
check("IPv6 loopback Host in brackets passes", gate({"Host": "[::1]:8743"})[0] == "local")
check("Host matching is case-insensitive", gate({"Host": "GUS.tailnet.ts.net"})[0] == "local")
check("an unknown Host is refused 421 (DNS-rebinding guard)", gate({"Host": "evil.example.com"}) == (None, (421, "bad_host")))
check("a missing Host is refused 421", gate({}) == (None, (421, "bad_host")))
check("Funnel (public internet) traffic is refused 403", gate({"Host": "127.0.0.1", "Tailscale-Funnel-Request": "?1"}) == (None, (403, "funnel")))
check("the node owner's tailnet login passes and is returned", gate({"Host": "127.0.0.1", "Tailscale-User-Login": "Fred@Example.com"})[0] == "fred@example.com")
check("another tailnet user is refused 403", gate({"Host": "127.0.0.1", "Tailscale-User-Login": "mallory@example.com"}) == (None, (403, "not_owner")))

h = dict(JSON, Host="127.0.0.1:8743")
check("a well-formed POST passes", gate(h, True)[0] == "local")
check("a POST with a same-origin Origin passes", gate(dict(h, Origin="http://127.0.0.1:8743"), True)[0] == "local")
check("a cross-origin POST is refused 403", gate(dict(h, Origin="https://evil.example.com"), True) == (None, (403, "origin")))
check("an Origin with a different port is refused", gate(dict(h, Origin="http://127.0.0.1:1"), True)[1] == (403, "origin"))
check("a POST without X-Shepr: 1 is refused 400", gate({"Host": "127.0.0.1", "Content-Type": "application/json"}, True) == (None, (400, "bad_request")))
check("X-Shepr must be exactly '1'", gate(dict(h, **{"X-Shepr": "0"}), True)[1] == (400, "bad_request"))
check("a text/plain POST (CORS-simple) is refused 400", gate({"Host": "127.0.0.1", "X-Shepr": "1", "Content-Type": "text/plain"}, True)[1] == (400, "bad_request"))
check("a POST with no Content-Type is refused 400", gate({"Host": "127.0.0.1", "X-Shepr": "1"}, True)[1] == (400, "bad_request"))
check("JSON content type with a charset suffix passes", gate(dict(h, **{"Content-Type": "application/json; charset=utf-8"}), True)[0] == "local")
up = {"Host": "127.0.0.1", "X-Shepr": "1", "Content-Type": "application/octet-stream"}
check("/api/upload requires application/octet-stream and accepts it", gate(up, True, "/api/upload")[0] == "local")
check("/api/upload refuses a JSON content type", gate(h, True, "/api/upload")[1] == (400, "bad_request"))
check("octet-stream is refused on every other write path", gate(up, True, "/api/send/prepare")[1] == (400, "bad_request"))
check("GET is not subject to the X-Shepr / content-type rule", gate({"Host": "127.0.0.1"}, False)[0] == "local")
print("OK" if ok else "FAILED"); sys.exit(0 if ok else 1)
