"""Shepr's own subscription meter: how much of each plan is used, and whether that is ahead of or behind an even spend.

Nothing here depends on another app. Each provider is measured the way that provider allows, from this host:

  Claude  Claude Code's own login file (`.credentials.json`) is read, and its access token goes ONLY to Anthropic's usage endpoint
          (https://api.anthropic.com/api/oauth/usage) as a read-only GET. The token is never refreshed here (that is Claude Code's job and
          doing it twice can invalidate the login), never logged and never part of anything returned.
  Codex   Codex writes its plan limits into its own session files (`rate_limits` on every token_count event); they are read, not asked for.
  Grok    Grok logs its credit usage when it starts; the newest line is read.
  Kimi    One GET to Kimi's usages endpoint, only when a KIMI_API_KEY is in the environment (or is the one line in ~/.env that names it).

`SHEPR_USAGE=0` switches all of it off. A provider that is not installed is not shown; one that is installed but cannot be read says why.

The pace maths (even spend, banked, the burn-down, the gift reset) follows Burn Bar's, the Omarchy bar widget that does this job on a desktop:
https://github.com/nixfred/burnbar. Standard library only.
"""
import datetime
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
KIMI_DEFAULT_BASE = "https://api.kimi.com/coding/v1"
HTTP_CAP = 262144

LIVE_MS = 20 * 60_000                  # a reading older than this is a snapshot: shown with a ~ and "last measured"
SAMPLE_GAP_MS = 10 * 60_000            # one sample per window per 10 minutes unless the figure moved
SAMPLE_MAX_AGE_MS = 40 * 86400_000
RATE_WINDOW_MS = 120 * 60_000          # the burn rate is fitted over the last 2 hours of samples ...
RATE_WINDOW_SHARE = 0.10               # ... or a tenth of a short window
RATE_MIN_SPAN_MS = 10 * 60_000
SERIES_MAX = 48
GIFT_MIN_DROP = 0.03                   # a provider restating 41% as 40% is not a gift
GIFT_ID_SLACK_MS = 2 * 60_000

# id, display name, seconds between probes
PROVIDERS = (("claude", "Claude", 300), ("codex", "Codex", 60), ("kimi", "Kimi", 240), ("grok", "Grok", 300))


# ---------------------------------------------------------------------------------------------------------------- small parsers
def number(v, dflt=0.0):
    if isinstance(v, bool):
        return dflt
    try:
        n = float(v)
    except (TypeError, ValueError):
        return dflt
    return n if math.isfinite(n) else dflt


def text(v, n):
    return "" if v is None else str(v)[:n]


def iso_ms(v):
    """Milliseconds since the epoch from an ISO time, or epoch seconds or milliseconds. 0 when it cannot be read."""
    if v is None or isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        n = number(v)
        if n <= 0:
            return 0
        return int(n * 1000) if n < 1e12 else int(n)
    s = str(v).strip()
    if not s:
        return 0
    if s.isdigit():
        return iso_ms(int(s))
    try:
        d = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return int(d.timestamp() * 1000)


def window_id(resets_ms):
    """Which window instance this is, to the minute: providers restate the same reset with millisecond jitter."""
    return (int(resets_ms) // 60_000) * 60_000


def month_ms(resets_ms):
    """The length of the calendar month that ends at this reset (a February window is February long)."""
    if not resets_ms:
        return 30 * 86400_000
    end = datetime.datetime.fromtimestamp(resets_ms / 1000, datetime.timezone.utc)
    y, mo = (end.year, end.month - 1) if end.month > 1 else (end.year - 1, 12)
    day = end.day
    while True:
        try:
            start = end.replace(year=y, month=mo, day=day)
            break
        except ValueError:
            day -= 1
    return max(86400_000, int((end - start).total_seconds() * 1000))


def window_ms_for(label, resets_ms, prev_id):
    """How long a window is when the provider did not say: the label carries it ("Session (5-hour)", "Weekly (7-day)",
    "Session (30-minute)"), else the distance to the previous reset we saw. A minute count is read here, before the
    "session" fallback below: Kimi labels a window that is not a whole number of hours "Session (30-minute)", which that
    fallback would pace as 5 hours."""
    t = str(label or "").lower()
    m = re.search(r"(\d+)\s*-\s*(minute|hour|day)", t)
    if m:
        return int(m.group(1)) * {"minute": 60_000, "hour": 3600_000, "day": 86400_000}[m.group(2)]
    if "week" in t:
        return 7 * 86400_000
    if "month" in t:
        return month_ms(resets_ms)
    if "session" in t:
        return 5 * 3600_000
    if "daily" in t or "day" in t:
        return 86400_000
    if prev_id and resets_ms > prev_id:
        return int(resets_ms - prev_id)
    return 0


def _limit(label, used, resets_ms, window_ms):
    return {"label": text(label, 40), "used": min(1.0, max(0.0, number(used))), "resets_ms": int(resets_ms or 0), "window_ms": int(window_ms or 0)}


# ---------------------------------------------------------------------------------------------------------------- HTTP (read-only GET)
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):   # a token must never follow a redirect to somewhere else
        return None


def https_get(url, headers, timeout=10):
    """(status, body, headers). HTTPS only, no redirects, the body capped. Raises OSError-like errors for transport failures."""
    if not str(url).startswith("https://"):
        raise ValueError("https only")
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as r:
            return r.status, r.read(HTTP_CAP), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(4096), dict(e.headers or {})


def _json(body):
    try:
        v = json.loads(body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else body)
    except ValueError:
        return None
    return v


# ---------------------------------------------------------------------------------------------------------------- Claude
def _utilization(v):
    try:
        return float(str(v).strip().replace("%", ""))
    except (TypeError, ValueError):
        return float("nan")


def _norm(v, percent_scale):
    n = _utilization(v)
    if not n >= 0:
        return -1.0
    if percent_scale or n > 1:
        return min(1.0, n / 100.0)
    return min(1.0, n)


def _scoped_window(kind):
    t = str(kind or "").lower()
    if "month" in t:
        return "Monthly", 0
    if "week" in t or "day" in t:
        return "Weekly", 7 * 86400_000
    if "hour" in t or "session" in t:
        return "Session", 5 * 3600_000
    return "", 0


def parse_claude_usage(payload):
    """Limit rows from Anthropic's usage payload. One payload speaks one convention (percent or fraction): any value >= 1 means percent."""
    if not isinstance(payload, dict):
        return []
    b = lambda k: payload.get(k) if isinstance(payload.get(k), dict) else None
    weekly = b("seven_day_oauth_apps") or b("seven_day")
    session = b("five_hour")
    entries = payload.get("limits") if isinstance(payload.get("limits"), list) else []
    raw = [session.get("utilization") if session else None, weekly.get("utilization") if weekly else None]
    raw += [e.get("percent") for e in entries if isinstance(e, dict)]
    scale = any(_utilization(v) >= 1 for v in raw)
    out = []
    for bucket, label, win in ((session, "Session (5-hour)", 5 * 3600_000), (weekly, "Weekly (7-day)", 7 * 86400_000)):
        if bucket is None:
            continue
        p = _norm(bucket.get("utilization"), scale)
        if p >= 0:
            out.append(_limit(label, p, iso_ms(bucket.get("resets_at")), win))
    seen = set()
    for e in entries:   # a model-scoped allowance (a weekly window only one model draws from) lives only in this array
        if not isinstance(e, dict):
            continue
        scope = e.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        if not isinstance(model, dict):
            continue
        name = str(model.get("display_name") or model.get("id") or "").strip()
        kind = str(e.get("kind") or "").strip()
        p = _norm(e.get("percent"), scale)
        if not name or p < 0 or (name, kind) in seen:
            continue
        seen.add((name, kind))
        word, win = _scoped_window(kind)
        resets = iso_ms(e.get("resets_at"))
        out.append(_limit(name + (" " + word if word else ""), p, resets, win or (month_ms(resets) if word == "Monthly" else 0)))
    return out


def _plan_label(tier, sub):
    m = re.search(r"max_(\d+x)", str(tier or ""), re.I)
    if m:
        return "Max " + m.group(1)
    s = str(sub or "")
    return s[:1].upper() + s[1:] if s else ""


def read_claude(claude_dir, now_ms, http_get=https_get):
    claude_dir = Path(claude_dir)
    if not claude_dir.is_dir():
        return None   # Claude Code is not used on this host
    out = {"present": True, "limits": [], "measured_ms": 0, "status": "", "help": "", "tier": ""}
    try:
        login = json.loads((claude_dir / ".credentials.json").read_text(encoding="utf-8")).get("claudeAiOauth")
    except (OSError, ValueError, AttributeError):
        login = None
    token = str(login.get("accessToken") or "") if isinstance(login, dict) else ""
    if not token:
        out["status"] = "Not signed in here"
        out["help"] = ("Claude Code keeps its login in the macOS Keychain, which Shepr cannot read yet." if sys.platform == "darwin"
                       else "Run `claude auth login` on this host and Shepr can read your plan usage.")
        return out
    out["tier"] = _plan_label(login.get("rateLimitTier"), login.get("subscriptionType"))
    exp = number(login.get("expiresAt"))
    if 0 < exp <= now_ms:
        out["status"] = "Sign-in expired"
        out["help"] = "Claude Code's saved sign-in expired. Start Claude Code, or run `claude auth login`, to refresh it."
        return out
    try:
        status, body, headers = http_get(CLAUDE_USAGE_URL, {"Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20", "Accept": "application/json"}, 10)
    except Exception:   # no route, no DNS, a timeout: say so, and keep what we knew
        out.update(status="Claude limits unavailable", help="Could not reach Anthropic's usage endpoint. Shepr tries again shortly.", error=True)
        return out
    if status == 429:
        retry = int(number((headers or {}).get("retry-after") or (headers or {}).get("Retry-After"), 0))
        out.update(status="Claude limits unavailable", help="Anthropic's usage endpoint is rate limiting checks right now. Shepr tries again later.", error=True, retry_after_s=max(900, retry))
        return out
    if status != 200:
        out.update(status="Claude limits unavailable", help="Anthropic's usage endpoint answered with status %d." % int(status), error=True)
        return out
    limits = parse_claude_usage(_json(body))
    if not limits:
        out.update(status="Claude limits unavailable", help="Anthropic's usage endpoint returned no limits.", error=True)
        return out
    out["limits"] = limits
    out["measured_ms"] = now_ms
    return out


# ---------------------------------------------------------------------------------------------------------------- Codex
def _tail(path, nbytes):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            data = f.read(nbytes)
    except OSError:
        return ""
    t = data.decode("utf-8", "replace")
    return t.split("\n", 1)[-1] if size > nbytes else t


def _codex_recent(root, days=3, limit=6):
    """The most recently written session files out of the newest few day folders (a long session started yesterday can still be the live one)."""
    def dirs(p):
        try:
            return sorted((e.name for e in os.scandir(p) if e.is_dir()), reverse=True)
        except OSError:
            return []
    files, seen = [], 0
    for y in dirs(root):
        for m in dirs(os.path.join(root, y)):
            for d in dirs(os.path.join(root, y, m)):
                p = os.path.join(root, y, m, d)
                try:
                    for e in os.scandir(p):
                        if e.name.startswith("rollout-") and e.name.endswith(".jsonl"):
                            files.append((e.stat().st_mtime, e.path))
                except OSError:
                    pass
                seen += 1
                if seen >= days:
                    break
            if seen >= days:
                break
        if seen >= days:
            break
    files.sort(reverse=True)
    return [p for _, p in files[:limit]]


def _codex_label(mins):
    if mins == 10080:
        return "Weekly (7-day)"
    if mins == 300:
        return "Session (5-hour)"
    if mins and mins % 60 == 0:
        return "%d-hour window" % (mins // 60)
    return "%d-minute window" % mins if mins else "Limit"


def parse_codex_rate_limits(rl, now_ms):
    """(limits, saw_expired) from one rate_limits object. A window whose reset has passed describes a period that is over: dropped."""
    limits, expired = [], False
    if not isinstance(rl, dict):
        return limits, expired
    for k in ("primary", "secondary"):
        w = rl.get(k)
        if not isinstance(w, dict) or w.get("used_percent") is None:
            continue
        used = number(w.get("used_percent"), -1)
        if used < 0:
            continue
        mins = int(number(w.get("window_minutes")))
        resets = iso_ms(w.get("resets_at"))
        if resets and resets <= now_ms:
            expired = True
            continue
        limits.append(_limit(_codex_label(mins), used / 100.0, resets, mins * 60_000))
    return limits, expired


def read_codex(codex_home, now_ms):
    root = os.path.join(str(codex_home), "sessions")
    if not os.path.isdir(root):
        return None
    out = {"present": True, "limits": [], "measured_ms": 0, "status": "", "help": "", "tier": ""}
    best = None   # (event time, rate_limits): the newest across the recent files, not the newest file
    for f in _codex_recent(root):
        for line in reversed(_tail(f, 400_000).splitlines()):
            if '"rate_limits"' not in line or '"token_count"' not in line:
                continue
            j = _json(line)
            pl = (j or {}).get("payload") if isinstance(j, dict) else None
            rl = pl.get("rate_limits") if isinstance(pl, dict) else None
            if not isinstance(rl, dict) or rl.get("limit_id") not in (None, "codex") or not (rl.get("primary") or rl.get("secondary")):
                continue
            ts = iso_ms(j.get("timestamp"))
            if best is None or ts > best[0]:
                best = (ts, rl)
            break
    if best is None:
        out["status"] = "No Codex usage recorded yet"
        out["help"] = "Shepr reads Codex's limits from its own session files, which update whenever Codex runs."
        return out
    limits, expired = parse_codex_rate_limits(best[1], now_ms)
    out["tier"] = text(best[1].get("plan_type"), 24)
    if not limits:
        out["status"] = "Codex's limit window has reset"
        out["help"] = "Shepr reads Codex's limits from its own session files, so the new figure appears the next time Codex runs." if expired else ""
        return out
    out["limits"], out["measured_ms"] = limits, best[0]
    return out


# ---------------------------------------------------------------------------------------------------------------- Grok
def read_grok(grok_home, now_ms):
    home = Path(grok_home)
    if not home.is_dir():
        return None
    out = {"present": True, "limits": [], "measured_ms": 0, "status": "", "help": "", "tier": ""}
    log = home / "logs" / "unified.jsonl"
    entry = None
    try:
        size = log.stat().st_size
    except OSError:
        size = 0
    for nbytes in (2_000_000, 16_000_000):
        for line in reversed(_tail(log, nbytes).splitlines()):
            if "billing: fetched credits config" not in line:
                continue
            j = _json(line)
            ctx = j.get("ctx") if isinstance(j, dict) else None
            cfg = ctx.get("config") if isinstance(ctx, dict) else None
            if isinstance(cfg, dict) and cfg.get("creditUsagePercent") is not None:
                entry = (j, ctx, cfg)
                break
        if entry or size <= nbytes:
            break
    if not entry:
        out["status"] = "No Grok billing snapshot yet"
        out["help"] = "Grok writes its credit usage to its log when it starts. Start Grok once."
        return out
    j, ctx, cfg = entry
    out["tier"] = text(ctx.get("subscriptionTier"), 24)
    period = cfg.get("currentPeriod") if isinstance(cfg.get("currentPeriod"), dict) else {}
    end = iso_ms(period.get("end") or cfg.get("billingPeriodEnd"))
    start = iso_ms(period.get("start") or cfg.get("billingPeriodStart"))
    kind = str(period.get("type") or "").upper()
    label = "Weekly (7-day)" if "WEEK" in kind or not kind else ("Monthly" if "MONTH" in kind else "Credits")
    window = end - start if end > start > 0 else (7 * 86400_000 if "WEEK" in kind or not kind else 0)
    if end and end <= now_ms:
        out["status"] = "Grok's window has reset"
        out["help"] = "Start Grok to refresh its figure."
        return out
    out["limits"] = [_limit(label, number(cfg.get("creditUsagePercent")) / 100.0, end, window)]
    out["measured_ms"] = iso_ms(j.get("ts"))
    return out


# ---------------------------------------------------------------------------------------------------------------- Kimi
def _env_unquote(raw):
    """The value only. A shell comment after it is not part of the value, and a # inside quotes is."""
    s = raw.strip()
    if s[:1] in "\"'":
        q, i = s[0], 1
        while i < len(s):
            if s[i] == "\\" and q == '"':
                i += 2
                continue
            if s[i] == q:
                return s[1:i]
            i += 1
        return s.strip().strip('"').strip("'")
    out = []
    for i, ch in enumerate(s):
        if ch == "#" and (i == 0 or s[i - 1].isspace()):
            break
        out.append(ch)
    return "".join(out).strip()


def _env_file_key(home, name):
    """The one named variable out of ~/.env, and nothing else. A comment on that line is not part of the value."""
    try:
        for line in (Path(home) / ".env").read_text(errors="replace").splitlines():
            m = re.match(r"^\s*(?:export\s+)?" + re.escape(name) + r"\s*=\s*(.*?)\s*$", line)
            if m:
                return _env_unquote(m.group(1))
    except OSError:
        pass
    return ""


def _kimi_window_label(w):
    n = int(number(w.get("duration"))) if isinstance(w, dict) else 0
    unit = str((w or {}).get("timeUnit") or "").replace("TIME_UNIT_", "").lower() if isinstance(w, dict) else ""
    if n <= 0 or not unit:
        return ""
    if unit == "minute" and n % 60 == 0:
        n, unit = n // 60, "hour"
    return "%d-%s" % (n, unit)


def parse_kimi_usages(raw):
    """Limit rows from Kimi's /usages payload: the request window that really cuts you off wins its span; the credit ratios for the month follow."""
    out, seen = [], set()
    if not isinstance(raw, dict):
        return out
    for e in (raw.get("limits") or []):
        if not isinstance(e, dict):
            continue
        d = e.get("detail") if isinstance(e.get("detail"), dict) else {}
        label = _kimi_window_label(e.get("window"))
        limit, used = number(d.get("limit")), number(d.get("used"))
        if not label or limit <= 0:
            continue
        seen.add(label)
        out.append(_limit("Session (%s)" % label, used / limit, iso_ms(d.get("resetTime")), window_ms_for("Session (%s)" % label, 0, 0)))
    usages = raw.get("usages") if isinstance(raw.get("usages"), dict) else {}
    for key, (label, span) in (("limit_5h", ("Session (5-hour)", "5-hour")), ("limit_month_total", ("Monthly (total)", "")), ("limit_month_code", ("Monthly (code)", ""))):
        e = usages.get(key)
        if not isinstance(e, dict) or (span and span in seen):
            continue
        resets = iso_ms(e.get("reset_time"))
        out.append(_limit(label, number(e.get("used_ratio")), resets, window_ms_for(label, resets, 0)))
    return out


def read_kimi(now_ms, http_get, env, home):
    key = str(env.get("KIMI_API_KEY") or "").strip() or _env_file_key(home, "KIMI_API_KEY")
    if not key:
        return None   # not configured: not shown
    out = {"present": True, "limits": [], "measured_ms": 0, "status": "", "help": "", "tier": ""}
    base = str(env.get("KIMI_BASE_URL") or KIMI_DEFAULT_BASE).rstrip("/")
    if not base.startswith("https://"):
        out.update(status="Kimi limits unavailable", help="KIMI_BASE_URL must be an https address.", error=True)
        return out
    try:
        status, body, _h = http_get(base + "/usages", {"Authorization": "Bearer " + key, "Accept": "application/json"}, 5)
    except Exception:
        out.update(status="Kimi limits unavailable", help="Could not reach Kimi's usage endpoint. Shepr tries again shortly.", error=True)
        return out
    limits = parse_kimi_usages(_json(body)) if status == 200 else []
    if not limits:
        out.update(status="Kimi limits unavailable", help="Check KIMI_API_KEY: Kimi's usage endpoint answered with status %d." % int(status) if status != 200 else "Kimi's usage endpoint returned no limits.", error=True)
        return out
    out["limits"], out["measured_ms"] = limits, now_ms
    return out


# ---------------------------------------------------------------------------------------------------------------- pace maths
def recent_rate_per_hour(points, window_ms=0):
    """Least-squares slope over the recent samples, as a fraction of the plan per hour. None when the samples cannot carry a rate: fewer than
    two, too short a span, or a percentage that fell (that is a reset, not a negative burn)."""
    if len(points) < 2:
        return None
    fit = RATE_WINDOW_MS
    if window_ms > 0:
        fit = min(fit, int(window_ms * RATE_WINDOW_SHARE))
    floor = points[-1][0] - fit
    pts = [(t, p) for t, p, _ in points if t >= floor]
    if len(pts) < 2 or pts[-1][0] - pts[0][0] < RATE_MIN_SPAN_MS:
        return None
    for i in range(1, len(pts)):
        if pts[i][1] < pts[i - 1][1] - 1e-9:
            return None
    n = len(pts)
    mt, mp = sum(t for t, _ in pts) / n, sum(p for _, p in pts) / n
    denom = sum((t - mt) ** 2 for t, _ in pts)
    if denom <= 0:
        return None
    return max(0.0, sum((t - mt) * (p - mp) for t, p in pts) / denom * 3600_000)


def gift_reset_at(points, rid, pct_now, now_ms):
    """When the provider last wiped this window's usage WITHOUT moving its reset (the occasional friendly reset). A percentage only climbs inside
    one window, so a real fall while the reset stays put is that gift, and the plan then has to last only from that moment to the reset."""
    mine = [(t, p) for t, p, r in points if abs(r - rid) <= GIFT_ID_SLACK_MS and t <= now_ms]
    mine.append((now_ms, float(pct_now)))
    at = 0
    for i in range(1, len(mine)):
        if mine[i][1] < mine[i - 1][1] - GIFT_MIN_DROP + 1e-9:
            at = mine[i][0]
    return int(at)


def burndown(points, resets_ms, window_ms, pct_now, now_ms, since=0):
    """This window's own samples as [x, y] in 0..1 (x the share of the window elapsed, y the share of the plan used), thinned and ending on now."""
    start = resets_ms - window_ms
    mine = [(t, p) for t, p, _ in points if max(start, since) <= t <= now_ms]
    if len(mine) > SERIES_MAX - 1:
        step = len(mine) / float(SERIES_MAX - 1)
        mine = [mine[int(i * step)] for i in range(SERIES_MAX - 1)]
    out = [[round((t - start) / float(window_ms), 5), round(p, 5)] for t, p in mine]
    out.append([round(min(1.0, max(0.0, (now_ms - start) / float(window_ms))), 5), round(float(pct_now), 5)])
    return out


# ---------------------------------------------------------------------------------------------------------------- the meter
class Meter:
    def __init__(self, state_dir, claude_dir, codex_home=None, grok_home=None, env=None, http_get=None, clock=None, home=None):
        self.env = os.environ if env is None else env
        self.home = Path(home or Path.home())
        self.dir = str(state_dir)
        self.claude_dir = claude_dir
        self.codex_home = codex_home or self.env.get("CODEX_HOME") or str(self.home / ".codex")
        self.grok_home = grok_home or self.env.get("GROK_HOME") or str(self.home / ".grok")
        self.http_get = http_get or https_get
        self.clock = clock or time.time
        self.enabled = str(self.env.get("SHEPR_USAGE", "1")) != "0"
        self.lock = threading.RLock()
        self.state = {pid: {"reading": None, "good": None, "next": 0} for pid, _n, _t in PROVIDERS}
        self.samples, self.win, self.dirty, self.ran, self.busy = {}, {}, False, False, False
        self._load()

    def now_ms(self):
        return int(self.clock() * 1000)

    # ---- sample history (our own, in Shepr's state directory)
    def _path(self):
        return os.path.join(self.dir, "usage-samples.jsonl")

    def _load(self):
        try:
            data = open(self._path(), "rb").read()
        except OSError:
            return
        floor = self.now_ms() - SAMPLE_MAX_AGE_MS
        for raw in data.split(b"\n"):
            try:
                r = json.loads(raw)
                ts, key, pct, rid = int(r["ts"]), str(r["key"])[:96], float(r["pct"]), int(r.get("resets") or 0)
            except (ValueError, KeyError, TypeError):
                continue
            if ts >= floor and 0 <= pct <= 1:
                self.samples.setdefault(key, []).append((ts, pct, rid))
        for v in self.samples.values():
            v.sort()

    def _save(self):
        if not self.dirty:
            return
        now = self.now_ms()
        lines = []
        for key, pts in self.samples.items():
            keep = max(now - SAMPLE_MAX_AGE_MS, now - 2 * max(self.win.get(key, 0), 3600_000))
            pts[:] = [p for p in pts if p[0] >= keep]
            lines += [json.dumps({"ts": t, "key": key, "pct": round(p, 6), "resets": r}) for t, p, r in pts]
        try:
            os.makedirs(self.dir, mode=0o700, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".usage-samples.", suffix=".tmp")
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write("\n".join(lines) + ("\n" if lines else ""))
            os.replace(tmp, self._path())
            self.dirty = False
        except OSError:
            pass

    def _record(self, key, ts, pct, rid):
        pts = self.samples.setdefault(key, [])
        if pts and pts[-1][0] >= ts:
            return
        last = pts[-1] if pts else None
        if last is None or last[2] != rid or abs(last[1] - pct) > 1e-9 or ts - last[0] >= SAMPLE_GAP_MS:
            pts.append((ts, pct, rid))
            self.dirty = True

    # ---- probing
    def _probe(self, pid, now):
        fns = {"claude": lambda: read_claude(self.claude_dir, now, self.http_get), "codex": lambda: read_codex(self.codex_home, now),
               "kimi": lambda: read_kimi(now, self.http_get, self.env, self.home), "grok": lambda: read_grok(self.grok_home, now)}
        try:
            return fns[pid]()
        except Exception:   # a provider failing must never take the meter, or the server, with it
            return {"present": True, "limits": [], "measured_ms": 0, "status": "%s limits unavailable" % pid.capitalize(), "help": "", "tier": "", "error": True}

    def refresh(self, force=False):
        if not self.enabled:
            return
        with self.lock:
            if self.busy:
                return
            self.busy = True
        try:
            now = self.now_ms()
            for pid, _name, ttl in PROVIDERS:
                if not force and now < self.state[pid]["next"]:
                    continue
                r = self._probe(pid, now)
                with self.lock:
                    self._absorb(pid, r, now, ttl)
            with self.lock:
                self.ran = True
                self._save()
        finally:
            with self.lock:
                self.busy = False

    def _absorb(self, pid, r, now, ttl):
        st = self.state[pid]
        if r is None:
            st.update(reading=None, good=None, next=now + ttl * 1000)
            return
        if r["limits"]:
            st["good"], st["reading"] = r, r
            ts = min(r["measured_ms"] or now, now + 60_000)
            for l in r["limits"]:
                key = ("%s|%s" % (pid, l["label"]))[:96]
                self.win[key] = l["window_ms"]
                if l["resets_ms"] > 0:
                    self._record(key, ts, l["used"], window_id(l["resets_ms"]))
        else:   # nothing fresh: keep what was last measured while its windows are still open, and say what went wrong
            g = st["good"]
            kept = [l for l in g["limits"] if l["resets_ms"] <= 0 or l["resets_ms"] > now] if g else []
            st["reading"] = dict(r, limits=kept, measured_ms=g["measured_ms"] if kept else r["measured_ms"], tier=r["tier"] or (g["tier"] if g else ""))
        st["next"] = now + int(max(ttl, r.get("retry_after_s", 0)) * 1000)

    def loop(self, every=30):
        while True:
            try:
                self.refresh()
            except Exception:
                pass
            time.sleep(every)

    # ---- the view the phone gets
    def _row(self, pid, lim, now, live):
        label = lim["label"]
        key = ("%s|%s" % (pid, label))[:96]
        resets, used = lim["resets_ms"], lim["used"]
        rid = window_id(resets) if resets > 0 else 0
        allpts = self.samples.get(key, [])
        pts = [p for p in allpts if rid and p[2] == rid]
        prev = max([p[2] for p in allpts if rid and p[2] and p[2] < rid] or [0])
        window = lim["window_ms"] or window_ms_for(label, resets, prev)
        row = {"label": label, "used": round(used, 4), "resets_ms": int(resets), "window_ms": int(window), "projected": -1.0, "dry_at": 0, "gift_at": 0, "series": []}
        if not (resets > now and window > 0):
            return row
        gift = gift_reset_at(pts, rid, used, now)
        if gift and resets - window < gift < resets:
            row["gift_at"], window = gift, resets - gift
            pts = [p for p in pts if p[0] >= gift]
            row["window_ms"] = int(window)
        row["series"] = burndown(pts, resets, window, used, now, row["gift_at"])
        rate = recent_rate_per_hour(pts, window) if live else None
        if rate is not None:
            hours_left = (resets - now) / 3600_000.0
            row["projected"] = round(used + rate * hours_left, 3)
            if rate > 0 and row["projected"] > 1.0:
                row["dry_at"] = int(now + ((1.0 - used) / rate) * 3600_000)
        return row

    def view(self):
        now = self.now_ms()
        if not self.enabled:
            return {"available": False, "measuring": False, "reason": "The subscription meter is switched off (SHEPR_USAGE=0).", "agents": [], "now": now}
        with self.lock:
            agents = []
            for pid, name, _ttl in PROVIDERS:
                rd = self.state[pid]["reading"]
                if not rd:
                    continue
                live = rd["measured_ms"] > 0 and now - rd["measured_ms"] <= LIVE_MS
                rows, seen = [], set()
                for lim in rd["limits"]:
                    same = (round(lim["used"], 4), window_id(lim["resets_ms"]), lim["window_ms"])
                    if same in seen:   # Kimi reports its "total" and "code" month as one meter twice
                        continue
                    seen.add(same)
                    rows.append(self._row(pid, lim, now, live))
                agents.append({"id": pid, "name": name, "rows": rows, "live": live, "measured_at": int(rd["measured_ms"]), "tier": text(rd["tier"], 24),
                               "status": text(rd["status"], 120), "help": text(rd["help"], 200)})
            measuring = not self.ran
            reason = "" if agents else ("Reading your subscriptions…" if measuring else
                                        "Shepr found no Claude Code, Codex, Grok or Kimi on this host, so there is nothing to meter.")
            return {"available": bool(agents), "measuring": measuring and not agents, "reason": reason, "generated_at": now, "now": now, "agents": agents}
