"""Shepr's own subscription meter (usage.py): every provider parsed and probed against stand-ins, never the network or the real login.
Pure Python. Run: python3 tests/usage_test.py"""
import json, os, stat, sys, tempfile, time
here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, ".."))
import usage

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name); ok = ok and bool(cond)

NOW = usage.iso_ms("2026-10-10T12:00:00Z")
SECRET = "sk-ant-oat01-SECRET-TOKEN-DO-NOT-LEAK"
H = 3600_000

def tmp():
    return tempfile.mkdtemp()

def claude_dir(expires_in_h=3.0, token=SECRET, creds=True):
    d = tmp()
    if creds:
        json.dump({"claudeAiOauth": {"accessToken": token, "refreshToken": "REFRESH-DO-NOT-LEAK", "expiresAt": NOW + expires_in_h * H, "rateLimitTier": "default_claude_max_20x", "subscriptionType": "max"}}, open(os.path.join(d, ".credentials.json"), "w"))
    return d

PAYLOAD = {"five_hour": {"utilization": 3.0, "resets_at": "2026-10-10T13:09:59.623991+00:00"}, "seven_day": {"utilization": 3.0, "resets_at": "2026-10-15T05:59:59.624008+00:00"},
           "limits": [{"kind": "weekly_scoped", "scope": {"model": {"display_name": "Fable"}}, "percent": 0.0, "resets_at": "2026-10-15T06:00:00+00:00"}]}

class Fake:
    """An http_get stand-in that records every call and can be told what to answer."""
    def __init__(self, status=200, payload=None, headers=None, boom=False, routes=None):
        self.calls, self.status, self.payload, self.headers, self.boom = [], status, PAYLOAD if payload is None else payload, headers or {}, boom
        self.routes = routes or {}   # a payload per provider, picked by what the URL contains
    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers)))
        if self.boom: raise OSError("no route to host")
        payload = next((p for k, p in self.routes.items() if k in url), self.payload)
        return self.status, json.dumps(payload).encode(), self.headers

# ---- Claude: what it asks, of whom, and how the answer is read
f = Fake()
r = usage.read_claude(claude_dir(), NOW, f)
check("claude: one GET, to Anthropic's usage endpoint and nowhere else, with the token only in the Authorization header",
      len(f.calls) == 1 and f.calls[0][0] == "https://api.anthropic.com/api/oauth/usage" and f.calls[0][1]["Authorization"] == "Bearer " + SECRET and f.calls[0][1]["anthropic-beta"] == "oauth-2025-04-20")
rows = {l["label"]: l for l in r["limits"]}
check("claude: the 5-hour session, the week and the Fable weekly window are read (percent scale: 3.0 is 3%)", set(rows) == {"Session (5-hour)", "Weekly (7-day)", "Fable Weekly"} and abs(rows["Weekly (7-day)"]["used"] - 0.03) < 1e-9)
check("claude: windows and resets are exact: 5 hours and 7 days, resets in milliseconds", rows["Session (5-hour)"]["window_ms"] == 5 * H and rows["Weekly (7-day)"]["window_ms"] == 168 * H and rows["Weekly (7-day)"]["resets_ms"] == usage.iso_ms("2026-10-15T05:59:59.624008+00:00"))
check("claude: the plan label comes from the login (Max 20x)", r["tier"] == "Max 20x")
check("claude: neither the access token nor the refresh token is anywhere in what is returned", SECRET not in json.dumps(r) and "REFRESH-DO-NOT-LEAK" not in json.dumps(r))
r = usage.read_claude(claude_dir(), NOW, Fake(payload={"five_hour": {"utilization": 0.37, "resets_at": "2026-10-10T13:00:00Z"}, "seven_day": {"utilization": 0.5, "resets_at": "2026-10-15T06:00:00Z"}}))
check("claude: a payload in fractions (no value at 1 or more) is read as fractions: 0.37 is 37%", [round(l["used"], 2) for l in r["limits"]] == [0.37, 0.5])
r = usage.read_claude(claude_dir(), NOW, Fake(payload={"five_hour": {"utilization": 1.0, "resets_at": "2026-10-10T13:00:00Z"}, "seven_day": {"utilization": 40.0, "resets_at": "2026-10-15T06:00:00Z"}}))
check("claude: one payload speaks one convention, so 1.0 beside 40.0 is 1% and not 100%", abs(r["limits"][0]["used"] - 0.01) < 1e-9)
f = Fake()
r = usage.read_claude(claude_dir(expires_in_h=-1), NOW, f)
check("claude: an expired sign-in makes NO request, says so, and says how to fix it", f.calls == [] and r["status"] == "Sign-in expired" and "claude auth login" in r["help"] and r["limits"] == [])
f = Fake(); r = usage.read_claude(claude_dir(creds=False), NOW, f)
check("claude: installed but not signed in says so and makes no request", f.calls == [] and "Not signed in" in r["status"] and r["limits"] == [])
check("claude: not installed at all means not shown", usage.read_claude(os.path.join(tmp(), "nope"), NOW, Fake()) is None)
r = usage.read_claude(claude_dir(), NOW, Fake(status=429, headers={"Retry-After": "1200"}))
check("claude: rate limited is reported as such and backs off for at least as long as Anthropic asked", r["limits"] == [] and r.get("error") and r["retry_after_s"] >= 1200 and "rate limiting" in r["help"])
r = usage.read_claude(claude_dir(), NOW, Fake(boom=True))
check("claude: no route to Anthropic is reported plainly, not raised", r["limits"] == [] and r.get("error") and "Could not reach" in r["help"])
r = usage.read_claude(claude_dir(), NOW, Fake(status=500)); check("claude: any other status is reported with the number", "500" in r["help"] and r["limits"] == [])
r = usage.read_claude(claude_dir(), NOW, Fake(payload={"surprise": 1})); check("claude: a payload with no limits is reported, not guessed", r["limits"] == [] and "no limits" in r["help"])
hostile = {"five_hour": {"utilization": "lots", "resets_at": "tomorrow"}, "seven_day": {"utilization": 9e99, "resets_at": 5}, "limits": [{"kind": "x" * 500, "scope": {"model": {"display_name": "N" * 500}}, "percent": float("nan")}, "junk", None]}
try:
    rr = usage.parse_claude_usage(hostile); check("claude: hostile numbers and text never crash, are clamped to 0..1, and labels are capped", all(0 <= l["used"] <= 1 and len(l["label"]) <= 40 for l in rr))
except Exception as e:
    check("claude: hostile payload crashed: %r" % e, False)
# A real HTTPS stand-in on 127.0.0.1 (a throwaway self-signed cert) that redirects to a second server: the redirect is not
# followed, so the second server never sees the token. (This line was `check(..., True)`, which tested nothing.)
import http.server, shutil, ssl, subprocess, threading
if shutil.which("openssl"):
    cd = tmp(); seen = []
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "1",
                    "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", cd + "/k.pem", "-out", cd + "/c.pem"],
                   check=True, capture_output=True)
    class Hop(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            seen.append((self.server.server_port, self.path, self.headers.get("Authorization")))
            if self.path == "/first":
                self.send_response(302); self.send_header("Location", "https://127.0.0.1:%d/second" % srv2.server_port)
            else:
                self.send_response(200)
            self.send_header("Content-Length", "0"); self.end_headers()
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); sctx.load_cert_chain(cd + "/c.pem", cd + "/k.pem")
    srv1, srv2 = (http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hop) for _ in range(2))
    for s_ in (srv1, srv2):
        s_.socket = sctx.wrap_socket(s_.socket, server_side=True); threading.Thread(target=s_.serve_forever, daemon=True).start()
    old_ca = os.environ.get("SSL_CERT_FILE"); os.environ["SSL_CERT_FILE"] = cd + "/c.pem"
    try:
        st_, _, _ = usage.https_get("https://127.0.0.1:%d/first" % srv1.server_port, {"Authorization": "Bearer " + SECRET}, 5)
    finally:
        os.environ.pop("SSL_CERT_FILE") if old_ca is None else os.environ.__setitem__("SSL_CERT_FILE", old_ca)
        srv1.shutdown(); srv2.shutdown()
    check("claude: the token never follows a redirect (the 302 comes back, the second server sees nothing)",
          st_ == 302 and [p_ for p_, _, _ in seen] == [srv1.server_port] and seen[0][2] == "Bearer " + SECRET)
else:
    print("SKIP claude: redirect check needs openssl for a throwaway certificate")
try:
    usage.https_get("http://api.anthropic.com/api/oauth/usage", {}, 1); check("https_get refuses plain http", False)
except ValueError:
    check("https_get refuses plain http", True)

# ---- Codex: read from its own session files
def codex_home(lines_by_file):
    h = tmp()
    for rel, (lines, mtime) in lines_by_file.items():
        p = os.path.join(h, "sessions", *rel.split("/")); os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write("\n".join(json.dumps(x) if not isinstance(x, str) else x for x in lines) + "\n"); os.utime(p, (mtime, mtime))
    return h
def tc(ts, used, resets_s, mins=10080, limit_id="codex", plan="pro", primary=True):
    w = {"used_percent": used, "window_minutes": mins, "resets_at": resets_s}
    return {"timestamp": ts, "type": "event_msg", "payload": {"type": "token_count", "info": None, "rate_limits": {"limit_id": limit_id, "primary": w if primary else None, "secondary": None, "plan_type": plan}}}
RESET = usage.iso_ms("2026-10-14T03:29:30Z") // 1000
h = codex_home({"2026/10/10/rollout-a.jsonl": ([tc("2026-10-10T08:00:00.000Z", 10.0, RESET), tc("2026-10-10T10:35:47.282Z", 17.0, RESET), "not json at all"], 2000),
                "2026/10/09/rollout-old.jsonl": ([tc("2026-10-09T08:00:00.000Z", 50.0, RESET)], 1000)})
r = usage.read_codex(h, NOW)
check("codex: the newest rate_limits event wins (17%), the plan and a 7-day window come from it, resets are exact", len(r["limits"]) == 1 and abs(r["limits"][0]["used"] - 0.17) < 1e-9 and r["limits"][0]["window_ms"] == 168 * H and r["limits"][0]["resets_ms"] == RESET * 1000 and r["tier"] == "pro")
check("codex: it was measured when the event was written, not now", r["measured_ms"] == usage.iso_ms("2026-10-10T10:35:47.282Z"))
h = codex_home({"2026/10/10/rollout-new.jsonl": ([tc("2026-10-10T09:00:00.000Z", 5.0, RESET)], 3000), "2026/10/10/rollout-long.jsonl": ([tc("2026-10-10T11:50:00.000Z", 18.0, RESET)], 2000)})
check("codex: a long-running session whose latest event is newer beats a newer-named file with an older event", abs(usage.read_codex(h, NOW)["limits"][0]["used"] - 0.18) < 1e-9)
h = codex_home({"2026/10/10/rollout-a.jsonl": ([tc("2026-10-10T10:00:00.000Z", 99.0, RESET, limit_id="codex_other"), tc("2026-10-10T09:00:00.000Z", 12.0, RESET)], 2000)})
check("codex: limits for another limit id are ignored", abs(usage.read_codex(h, NOW)["limits"][0]["used"] - 0.12) < 1e-9)
h = codex_home({"2026/10/10/rollout-a.jsonl": ([tc("2026-10-10T10:00:00.000Z", 40.0, usage.iso_ms("2026-10-10T11:00:00Z") // 1000)], 2000)})
r = usage.read_codex(h, NOW)
check("codex: a window that has already reset is never shown as current: no row, and it says it updates when Codex next runs", r["limits"] == [] and "reset" in r["status"] and "next time Codex runs" in r["help"])
r = usage.read_codex(codex_home({"2026/10/10/rollout-a.jsonl": (["{}"], 1)}), NOW)
check("codex: sessions but no limits recorded yet says so", r["limits"] == [] and "No Codex usage" in r["status"])
check("codex: not installed means not shown", usage.read_codex(os.path.join(tmp(), "nope"), NOW) is None)
pad = {"timestamp": "2026-10-10T09:00:00Z", "type": "x", "payload": {"pad": "y" * 900}}
big = codex_home({"2026/10/10/rollout-a.jsonl": ([pad] * 1500 + [tc("2026-10-10T10:00:00.000Z", 21.0, RESET)], 2000)})
r = usage.read_codex(big, NOW)
check("codex: a session file of over a megabyte is read from its end (400 KB), and the newest event there is found", len(r["limits"]) == 1 and abs(r["limits"][0]["used"] - 0.21) < 1e-9)
early = codex_home({"2026/10/10/rollout-a.jsonl": ([tc("2026-10-10T10:00:00.000Z", 21.0, RESET)] + [pad] * 1500, 2000)})
r = usage.read_codex(early, NOW)
check("codex: an event buried far from the end of a huge file is not dug out (and nothing crashes): it says no usage is recorded yet", r["limits"] == [] and "No Codex usage" in r["status"])

# ---- Grok: its credits line
def grok_home(lines):
    h = tmp(); os.makedirs(os.path.join(h, "logs")); open(os.path.join(h, "logs", "unified.jsonl"), "w").write("\n".join(json.dumps(x) if not isinstance(x, str) else x for x in lines) + "\n"); return h
def gl(ts, pct, end="2026-10-15T03:10:03.312033+00:00", start="2026-10-08T03:10:03.312033+00:00"):
    return {"ts": ts, "msg": "billing: fetched credits config", "ctx": {"config": {"creditUsagePercent": pct, "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY", "start": start, "end": end}}, "subscriptionTier": "SuperGrok Plus"}}
r = usage.read_grok(grok_home([gl("2026-10-09T01:00:00.000Z", 1.0), {"ts": "x", "msg": "other"}, gl("2026-10-10T04:21:58.499Z", 2.0), "garbage"]), NOW)
check("grok: the newest credits line wins; 2.0 is 2%; the window is exactly the billing period (7 days)", len(r["limits"]) == 1 and abs(r["limits"][0]["used"] - 0.02) < 1e-9 and r["limits"][0]["window_ms"] == 168 * H and r["tier"] == "SuperGrok Plus")
check("grok: it is a snapshot from when Grok started", r["measured_ms"] == usage.iso_ms("2026-10-10T04:21:58.499Z"))
r = usage.read_grok(grok_home([gl("2026-10-01T04:00:00.000Z", 9.0, end="2026-10-08T03:00:00Z", start="2026-10-01T03:00:00Z")]), NOW)
check("grok: a window that has ended is not shown as current", r["limits"] == [] and "reset" in r["status"])
check("grok: a log with no credits line says so", "No Grok billing" in usage.read_grok(grok_home(["x"]), NOW)["status"])
check("grok: not installed means not shown", usage.read_grok(os.path.join(tmp(), "nope"), NOW) is None)

# ---- Kimi: only with a key, one named line out of ~/.env, https only
KIMI = {"limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"}, "detail": {"limit": "100", "used": "8", "resetTime": "2026-10-10T13:43:48Z"}}],
        "usages": {"limit_5h": {"used_ratio": 0.5, "reset_time": "2026-10-10T13:43:48Z"}, "limit_month_total": {"used_ratio": 0.1042, "reset_time": "2026-10-19T00:00:00Z"}, "limit_month_code": {"used_ratio": 0.1042, "reset_time": "2026-10-19T00:00:00Z"}}}
home = tmp(); open(os.path.join(home, ".env"), "w").write('OTHER_SECRET=do-not-read\nexport KIMI_API_KEY="kimi-key-1"\nANOTHER=also-not\n')
f = Fake(payload=KIMI)
r = usage.read_kimi(NOW, f, {}, home)
check("kimi: the key comes from the one named line in ~/.env, is sent only to Kimi, and nothing else in that file is read or sent", len(f.calls) == 1 and f.calls[0][0] == "https://api.kimi.com/coding/v1/usages" and f.calls[0][1]["Authorization"] == "Bearer kimi-key-1" and "do-not-read" not in json.dumps(f.calls))
home = tmp(); open(os.path.join(home, ".env"), "w").write('OTHER_SECRET=do-not-read\nexport KIMI_API_KEY="kimi-key-1" # OTHER=secret\nANOTHER=also-not\n')
f = Fake(payload=KIMI)
usage.read_kimi(NOW, f, {}, home)
check("kimi: a shell comment on the key's line is not part of the token sent to Kimi", f.calls[0][1]["Authorization"] == "Bearer kimi-key-1" and "OTHER" not in f.calls[0][1]["Authorization"] and "secret" not in json.dumps(f.calls) and "do-not-read" not in json.dumps(f.calls))
home = tmp(); open(os.path.join(home, ".env"), "w").write('KIMI_API_KEY=kimi-key-1 # comment\n')
f = Fake(payload=KIMI)
usage.read_kimi(NOW, f, {}, home)
check("kimi: an unquoted key with a trailing comment sends only the key", f.calls[0][1]["Authorization"] == "Bearer kimi-key-1" and "#" not in f.calls[0][1]["Authorization"])
home = tmp(); open(os.path.join(home, ".env"), "w").write('export KIMI_API_KEY="kimi#key"\n')
f = Fake(payload=KIMI)
usage.read_kimi(NOW, f, {}, home)
check("kimi: a hash inside the quotes stays in the key", f.calls[0][1]["Authorization"] == "Bearer kimi#key")
labels = {l["label"]: l for l in r["limits"]}
check("kimi: the request window that cuts you off wins the 5-hour row (8%), its duplicate credit ratio is dropped, the month follows", abs(labels["Session (5-hour)"]["used"] - 0.08) < 1e-9 and "Monthly (total)" in labels and sum(1 for l in r["limits"] if l["label"].startswith("Session")) == 1)
check("kimi: the month is a real calendar month long (October reset on the 19th: back to September 19th is 30 days)", labels["Monthly (total)"]["window_ms"] == 30 * 86400_000)
def _kimi_row(minutes):
    raw = {"limits": [{"window": {"duration": minutes, "timeUnit": "TIME_UNIT_MINUTE"}, "detail": {"limit": "100", "used": "8", "resetTime": "2026-10-10T13:43:48Z"}}]}
    return usage.parse_kimi_usages(raw)[0]
check("kimi: a 30 and a 90 minute window are 30 and 90 minutes long, not the 5-hour fallback every session label gets",
      _kimi_row(30)["window_ms"] == 30 * 60_000 and _kimi_row(90)["window_ms"] == 90 * 60_000)
check("kimi: 300 minutes is still the 5-hour window", _kimi_row(300)["label"] == "Session (5-hour)" and _kimi_row(300)["window_ms"] == 5 * H)
check("kimi: no key means not shown, and no request", usage.read_kimi(NOW, Fake(), {}, tmp()) is None)
f = Fake(); r = usage.read_kimi(NOW, f, {"KIMI_API_KEY": "k", "KIMI_BASE_URL": "http://insecure.example"}, tmp())
check("kimi: a plain-http base URL is refused so the key is never sent in the clear", f.calls == [] and "https" in r["help"])
check("kimi: a bad key is reported with the status", "401" in usage.read_kimi(NOW, Fake(status=401), {"KIMI_API_KEY": "k"}, tmp())["help"])

# ---- the meter: probing, sample history, the view
def meter(clock, http=None, **kw):
    d = kw.pop("state", None) or tmp()
    return usage.Meter(d, kw.pop("cd", None) or claude_dir(), kw.pop("codex", None) or os.path.join(tmp(), "none"), kw.pop("grok", None) or os.path.join(tmp(), "none"),
                       env=kw.pop("env", {}), http_get=http or Fake(), clock=lambda: clock[0] / 1000.0, home=tmp()), d
clock = [NOW]
h = codex_home({"2026/10/10/rollout-a.jsonl": ([tc("2026-10-10T10:35:47.282Z", 17.0, RESET)], 2000)})
gh = grok_home([gl("2026-10-10T04:21:58.499Z", 2.0)])
f = Fake(routes={"kimi": KIMI})
m, sd = meter(clock, f, codex=h, grok=gh, env={"KIMI_API_KEY": "kimi-key-1"})
v0 = m.view()
check("meter: before the first reading there is nothing and it says it is measuring (the page never waits on the network)", v0["available"] is False and v0["measuring"] is True and f.calls == [])
m.refresh(); v = m.view()
check("meter: Claude, Codex, Kimi and Grok in that order, each only because it is installed", [a["id"] for a in v["agents"]] == ["claude", "codex", "kimi", "grok"])
a = {x["id"]: x for x in v["agents"]}
check("meter: Claude and Kimi were measured just now (live); Codex and Grok are snapshots from when they last ran", a["claude"]["live"] and a["kimi"]["live"] and not a["codex"]["live"] and not a["grok"]["live"])
check("meter: Kimi's duplicated month meter is shown once", sorted(r["label"] for r in a["kimi"]["rows"]) == ["Monthly (total)", "Session (5-hour)"])
check("meter: no secret appears anywhere in the view", SECRET not in json.dumps(v) and "kimi-key-1" not in json.dumps(v) and "REFRESH" not in json.dumps(v))
sp = os.path.join(sd, "usage-samples.jsonl")
check("meter: the sample history is Shepr's own file, private (0600), and holds no secret", os.path.exists(sp) and stat.S_IMODE(os.stat(sp).st_mode) == 0o600 and SECRET not in open(sp).read())
n = len(f.calls); m.refresh()
check("meter: asking again inside the interval makes no new requests", len(f.calls) == n)
clock[0] += 6 * 60_000; m.refresh()
check("meter: after the interval it probes again", len(f.calls) > n)
check("meter: Codex and Grok (local files) are not asked over the network", all("anthropic" in c[0] or "kimi" in c[0] for c in f.calls))

# history, rate, projection, restart
clock = [NOW]; f = Fake(payload={"five_hour": {"utilization": 10.0, "resets_ms": 0, "resets_at": "2026-10-10T16:00:00Z"}, "seven_day": {"utilization": 10.0, "resets_at": "2026-10-15T06:00:00Z"}})
m, sd = meter(clock, f)
for used in (10.0, 11.0, 12.0, 13.0):
    f.payload = {"five_hour": {"utilization": used, "resets_at": "2026-10-10T16:00:00Z"}, "seven_day": {"utilization": used, "resets_at": "2026-10-15T06:00:00Z"}}
    m.refresh(force=True); clock[0] += 10 * 60_000
clock[0] -= 10 * 60_000
row = {r["label"]: r for r in m.view()["agents"][0]["rows"]}["Weekly (7-day)"]
check("history: our own samples draw the burn-down, oldest first, ending on the figure now", len(row["series"]) >= 4 and row["series"] == sorted(row["series"]) and abs(row["series"][-1][1] - 0.13) < 1e-9)
check("history: a rising figure gives a burn rate, so a projection to the reset above the figure now", row["projected"] > 0.13)
m2, _ = meter(clock, Fake(), state=sd)
check("history: it survives a restart (the curve is still there, read from Shepr's own file)", len(m2.samples.get("claude|Weekly (7-day)", [])) >= 4)

# the gift reset: the provider wipes usage without moving the reset
clock = [NOW]; f = Fake(); m, sd = meter(clock, f)
for used in (50.0, 52.0, 2.0):
    f.payload = {"five_hour": {"utilization": 1.0, "resets_at": "2026-10-10T16:00:00Z"}, "seven_day": {"utilization": used, "resets_at": "2026-10-15T06:00:00Z"}}
    m.refresh(force=True); clock[0] += 10 * 60_000
row = {r["label"]: r for r in m.view()["agents"][0]["rows"]}["Weekly (7-day)"]
check("gift reset: a fall in usage with the reset unchanged is detected, and the plan now only has to last from that moment (a shorter window)", row["gift_at"] > 0 and row["window_ms"] < 168 * H)

# failures keep what was known, and back off
clock = [NOW]; f = Fake(); m, sd = meter(clock, f)
m.refresh(); good = {r["label"]: r["used"] for r in m.view()["agents"][0]["rows"]}
f.status, f.headers = 429, {"Retry-After": "1200"}; clock[0] += 6 * 60_000; m.refresh(); n = len(f.calls)
a = m.view()["agents"][0]
check("failure: rate limited keeps the last good numbers while their windows are open, and says why", {r["label"]: r["used"] for r in a["rows"]} == good and "rate limiting" in a["help"] and a["live"] is True)
clock[0] += 10 * 60_000; m.refresh()
check("failure: it does not ask again before the back-off is over", len(f.calls) == n)
clock[0] += 20 * 60_000; m.refresh()
check("failure: and does ask again after it", len(f.calls) > n and m.view()["agents"][0]["live"] is False)
clock = [NOW]; f = Fake(); m, sd = meter(clock, f, cd=claude_dir(expires_in_h=-2)); m.refresh(); a = m.view()["agents"][0]
check("failure: an expired sign-in shows a card that says so (no numbers, no request)", f.calls == [] and a["rows"] == [] and a["status"] == "Sign-in expired" and "claude auth login" in a["help"])
clock = [NOW]; f = Fake(); m, sd = meter(clock, f, env={"SHEPR_USAGE": "0"}); m.refresh(); v = m.view()
check("switch: SHEPR_USAGE=0 makes no request at all and says it is off", f.calls == [] and v["available"] is False and "switched off" in v["reason"])
m, sd = meter([NOW], Fake(), cd=os.path.join(tmp(), "nope")); m.refresh(); v = m.view()
check("nothing installed: it says there is nothing to meter, instead of a blank page", v["available"] is False and v["measuring"] is False and "nothing to meter" in v["reason"])
class Boom(Fake):
    def __call__(self, *a, **k): raise RuntimeError("provider exploded")
m, sd = meter([NOW], Boom()); m.refresh()
check("a provider that raises never takes the meter down: its card says it is unavailable", m.view()["agents"][0]["rows"] == [] and "unavailable" in m.view()["agents"][0]["status"])

# the code never reads another app's files
src = open(os.path.join(here, "..", "usage.py")).read()
code = "\n".join(l for l in src.split("\n") if not l.lstrip().startswith("#"))
check("independence: usage.py names no Burn Bar or Omarchy state file or path", ".local/state/omarchy" not in src and "history.json" not in src and "agents/usage" not in src and "burnbar" not in code.lower().replace("github.com/nixfred/burnbar", ""))
sys.exit(0 if ok else 1)
