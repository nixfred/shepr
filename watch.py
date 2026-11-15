"""Shepr's watch: what the agents on this host have been DOING, read from Claude Code's own session files.

An agent started with its permission prompts switched off never asks before it runs something, so a queue of questions says
nothing about what it did. Claude Code writes every reply into ~/.claude/projects/<project>/<session>.jsonl (and each of a
session's helpers into <session>/subagents/). One incremental read of those files gives three things:

  burn      the tokens every reply used, by session and model. Each reply carries the API's own `usage`.
  risk      the shell commands an agent RAN that a person would want to know about: a force push, a recursive delete, sudo ...
  activity  when each session last wrote anything, where it runs and what Claude Code called it.

Only this host's own Claude Code files are read (never another app's). Lines are data: nothing in them is executed, nothing is
sent anywhere, and a line that is not what Claude Code writes is skipped. Standard library only.
"""
import calendar
import json
import os
import re
import threading
import time

KEEP_S = 26 * 3600          # how far back replies are kept (the longest span shown is 24 hours)
READ_CAP = 32 << 20         # most bytes taken from one file in one pass; the rest comes on the next
LINE_CAP = 4 << 20          # an assistant line is small; a longer one is a pasted file or a tool result and is skipped unparsed
CMD_KEEP = 400              # risky commands remembered
TS_RE = re.compile(rb'"timestamp":"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d+)?Z"')

# How much one token of each kind weighs when sessions are compared. The shape is the API's own (generated tokens cost about five
# times a fresh input token, a cached one a tenth); the model factors are what Claude Code's own cost records worked out to on the
# author's machine in November 2026. Nobody outside Anthropic knows how a subscription counts them, so the result is shown as a
# SHARE of the burn, with the exact token counts beside it, never as money. SHEPR_BURN_WEIGHTS="opus=3,sonnet=1" overrides.
W_OUT, W_IN, W_CACHE_WRITE, W_CACHE_READ = 5.0, 1.0, 1.25, 0.1
MODEL_FACTOR = {"fable": 5.0, "opus": 2.6, "sonnet": 1.0, "haiku": 0.2}


def model_factors(env=None):
    out = dict(MODEL_FACTOR)
    for part in ((os.environ if env is None else env).get("SHEPR_BURN_WEIGHTS") or "").split(","):
        k, _, v = part.partition("=")
        try:
            if k.strip() and float(v) >= 0:
                out[k.strip().lower()] = float(v)
        except ValueError:
            continue
    return out


def family(model):
    """claude-opus-5-5 -> opus. A model this file has never heard of keeps its own name (shortened), and weighs 1."""
    m = str(model or "").lower()
    for f in ("fable", "opus", "sonnet", "haiku"):
        if f in m:
            return f
    return re.sub(r"^claude-", "", m)[:16] or "?"


def parse_ts(v):
    """An ISO time as Claude Code writes it ("2026-11-14T20:48:34.534Z") -> epoch seconds, or None."""
    if not isinstance(v, str) or len(v) < 19:
        return None
    try:
        return float(calendar.timegm(time.strptime(v[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return None


# ---------------------------------------------------------------------------------------------------------------- risk
# (level, what a person would call it, pattern). "danger" is hard or impossible to undo, or reaches outside the machine in a way that
# cannot be taken back; "note" is worth knowing. Danger is looked for before note; the first match is the answer. Deliberately a
# short list of things that matter, tuned on a day of real sessions: a watch that cries at every `rm` or `sudo` is one nobody reads.
_SEP = r"(?:^|[;&|(`\n]|\$\(|\bthen\b|\bdo\b|\bsudo\b(?:\s+-\w+)*|\bxargs\b(?:\s+-\w+)*)\s*(?:[A-Za-z_]\w*=\S*\s+)*"   # where a command can start inside a shell line (after any VAR=value in front of it)
_WIDE = r"(?:/|~|~/|\$HOME/?|\$\{HOME\}/?|/\*|\*|\.|\.\./?|/home/?|/etc/?|/usr/?|/var/?|/opt/?|/boot/?)"   # a target that is a whole tree
_REAL = r"(?:/(?!tmp/|var/tmp/|dev/shm/|run/user/|home/[^/\s]+/\.cache/)|(?:~|\$HOME|\$\{HOME\})/(?!\.cache/))\S"                             # a path that is plainly not a scratch folder
RISK = [
    ("danger", "force push", re.compile(_SEP + r"git\b[^;&|\n]*\bpush\b[^;&|\n]*(?:\s--force(?!-with-lease)\b|\s-[a-zA-Z]*f[a-zA-Z]*\b|\s\+[\w./-]+)")),
    ("danger", "discarded local changes (git reset --hard)", re.compile(_SEP + r"git\b[^;&|\n]*\breset\b[^;&|\n]*--hard\b")),
    ("danger", "deleted untracked files (git clean)", re.compile(_SEP + r"git\b[^;&|\n]*\bclean\b[^;&|\n]*\s-[a-zA-Z]*f")),
    ("danger", "discarded working changes (git checkout or restore .)", re.compile(_SEP + r"git\b[^;&|\n]*\b(?:checkout\s+(?:--\s+)?\.|restore\s+(?:(?:--worktree|-W|--source=\S+)\s+)*\.)(?:\s|$)")),
    ("danger", "rewrote git history", re.compile(_SEP + r"git\b[^;&|\n]*\b(?:filter-branch|filter-repo)\b")),
    ("danger", "recursive delete of a whole tree", re.compile(_SEP + r"rm\b(?=[^;&|\n]*\s-[a-zA-Z]*[rR])[^;&|\n]*\s" + _WIDE + r"(?:\s|$|[;&|])")),
    ("danger", "wrote straight to a disk", re.compile(_SEP + r"(?:mkfs(?:\.\w+)?|wipefs|shred)\s|" + _SEP + r"dd\b[^;&|\n]*\bof=/dev/(?!null\b|zero\b|stdout\b|stderr\b)")),
    ("danger", "piped a download into a shell", re.compile(_SEP + r"(?:curl|wget)\b[^;&\n]*\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b")),
    ("danger", "destroyed infrastructure", re.compile(_SEP + r"(?:terraform\b[^;&|\n]*\bdestroy\b|kubectl\s+(?:-\S+\s+)*delete\b|gh\s+repo\s+delete\b)")),
    ("danger", "published a port (tailscale serve or funnel)", re.compile(_SEP + r"(?:\S*/)?tailscale\s+(?:--?\S+\s+)*(?:serve|funnel)\b(?![^;&|\n]*\b(?:status|off|reset|get-config)\b)")),
    ("danger", "sent email", re.compile(_SEP + r"(?:msmtp\s|sendmail\s|himalaya\b[^;&|\n]*\bsend\b|mail\s+-s\s|hey\s+(?:compose|reply|forward|bulk-reply)\b)")),
    ("danger", "shut the machine down", re.compile(_SEP + r"(?:(?:reboot|poweroff|halt)\s*(?:$|[;&|)\n]|-\w)|shutdown\s+(?:-[hrP]\b|now\b|\+\d)|systemctl\s+(?:-\S+\s+)*(?:reboot|poweroff|halt)\b)")),
    ("note", "force push (with lease)", re.compile(_SEP + r"git\b[^;&|\n]*\bpush\b[^;&|\n]*--force-with-lease\b")),
    ("note", "recursive delete outside a scratch folder", re.compile(_SEP + r"rm\b(?=[^;&|\n]*\s-[a-zA-Z]*[rR])[^;&|\n]*?\s" + _REAL)),
    ("note", "skipped git hooks (--no-verify)", re.compile(_SEP + r"git\b[^;&|\n]*\b(?:commit|push)\b[^;&|\n]*--no-verify\b")),
    ("note", "disabled a service", re.compile(_SEP + r"systemctl\s+(?:-\S+\s+)*(?:disable|mask)\s")),   # a plain stop is deploy routine; these two outlive a reboot
    ("note", "made files world-writable", re.compile(_SEP + r"chmod\s+(?:-\S+\s+)*(?:[0-7]?777|a\+rwx|o\+w)\s")),
    ("note", "removed packages", re.compile(_SEP + r"(?:pacman\s+-R\w*|yay\s+-R\w*|apt(?:-get)?\s+(?:remove|purge)|dnf\s+remove|brew\s+uninstall)\s")),
    ("note", "published a package or release", re.compile(_SEP + r"(?:npm\s+publish|cargo\s+publish|twine\s+upload|gem\s+push|gh\s+release\s+create)\b")),
    ("note", "pruned containers or volumes", re.compile(_SEP + r"docker\s+(?:system\s+prune|volume\s+(?:rm|prune)|rm\s+-[a-zA-Z]*f)\b")),
]
_LEVELS = ("danger", "note")
# Judged on the command as written, quotes and here-documents included: SQL reaches a database client AS a quoted string or a
# here-document, so that is exactly where it has to be looked for. It must follow a database client on the same command.
RISK_RAW = [
    ("danger", "dropped a table or database", re.compile(r"\b(?:psql|sqlite3?|mysql|mariadb|duckdb|clickhouse-client|wrangler\s+d1)\b[\s\S]{0,600}?\b(?:DROP\s+(?:TABLE|DATABASE|SCHEMA)|TRUNCATE\s+TABLE)\s+\S", re.I)),
]
HEREDOC =re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
HEREDOC_TO_SHELL = re.compile(r"(?:^|[;&|(]\s*|\s)(?:bash|sh|zsh|dash|ssh)\b[^|;&]*<<")
HEREDOC_TO_DB = re.compile(r"(?:^|[;&|(]\s*|\s)(?:psql|sqlite3?|mysql|mariadb|duckdb|clickhouse-client)\b[^|;&]*<<")
# A quoted string is run as code only when what it is handed to is a shell (or it is the target of an rm). Anywhere else it is
# something being TALKED about: a commit message, a grep pattern, an echo, a line of Python.
SHELLISH = re.compile(r"(?:^|[\s;&|(])(?:ssh|bash|sh|zsh|dash|fish|eval|su|rm|watch|parallel)\b")


def strip_heredocs(cmd, also=None):
    """The command without here-documents that are only DATA (a file being written, a script for Python). One fed to a shell
    stays, and so does one fed to whatever `also` matches."""
    lines, out, i = cmd.split("\n"), [], 0
    while i < len(lines):
        out.append(lines[i])
        m = HEREDOC.search(lines[i])
        if not m:
            i += 1
            continue
        keep, j = bool(HEREDOC_TO_SHELL.search(lines[i]) or (also and also.search(lines[i]))), i + 1
        while j < len(lines) and lines[j].strip() != m.group(2):
            if keep:
                out.append(lines[j])
            j += 1
        i = j + 1
    return "\n".join(out)


def shell_view(cmd):
    """What a shell would actually RUN: here-documents that are data are gone, and so is the inside of every quoted string that is
    not handed to a shell. A commit message or a grep pattern that merely mentions a command then says nothing; the same words
    handed to ssh or `bash -c` still do."""
    cmd = strip_heredocs(cmd)
    out, seg, size, i, n = [], 0, 0, 0, len(cmd)
    while i < n:
        c = cmd[i]
        if c in "'\"":
            j = i + 1
            while j < n and cmd[j] != c:
                if c == '"' and cmd[j] == "\\":
                    j += 1
                j += 1
            # kept words start a command of their own (a newline in front), so `ssh box 'x'` is judged as `x` would be
            piece = "\n" + cmd[i + 1:j] + "\n" if SHELLISH.search("".join(out)[seg:]) else "_"
            out.append(piece)
            size += len(piece)
            i = j + 1
            continue
        out.append(c)
        size += 1
        if c in ";&|\n":
            seg = size
        i += 1
    return "".join(out)


def risk_scan(cmd):
    """The one thing most worth knowing about a shell command, as (level, label), or None. Never executes or expands anything."""
    if not isinstance(cmd, str) or not cmd.strip():
        return None
    cmd = cmd[:12000]
    text = shell_view(cmd)
    raw = strip_heredocs(cmd, also=HEREDOC_TO_DB)   # quotes kept, and a here-document only when it feeds a shell or a database client
    for want in _LEVELS:
        for level, label, rx in RISK:
            if level == want and rx.search(text):
                return level, label
        for level, label, rx in RISK_RAW:
            if level == want and rx.search(raw):
                return level, label
    return None


SENSITIVE_PATH = re.compile(r"(?:^|/)(?:\.ssh/|\.aws/|\.gnupg/|\.env(?:\.[\w.-]+)?$|credentials(?:\.json)?$|id_(?:rsa|ed25519|ecdsa)\b)|^/etc/(?:sudoers|passwd|shadow|ssh/)")


# A command line can carry a credential (a token in a header, a password in a URL). The command is kept so a person can see what
# ran, but what looks like a secret is cut out before it is stored or sent to a phone. Deliberately greedy: a false cut costs
# nothing, a kept token would sit in a log.
SECRET_AFTER = re.compile(r"(?i)(bearer\s+|authorization:\s*(?:\w+\s+)?|[?&\s\"'](?:token|secret|password|passwd|api_key|apikey|access_key|key)=|://[^/\s:@]+:)([^\s&\"'@]{4,})")
SECRET_TOKEN = re.compile(r"(?i)\b(?:sk|pk|rk|ghp|gho|ghu|ghs|github_pat|glpat|xox[abprs]|tskey|AKIA|ASIA|hf|npm)[-_][A-Za-z0-9_\-]{10,}|\b[A-Fa-f0-9]{40,}\b|\b[A-Za-z0-9+/]{48,}={0,2}")


def redact(s):
    return SECRET_TOKEN.sub("[cut]", SECRET_AFTER.sub(lambda m: m.group(1) + "[cut]", s))


def one_line(s, n):
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ---------------------------------------------------------------------------------------------------------------- the watch
class Watch:
    def __init__(self, claude_home, clock=None, keep_s=KEEP_S, env=None):
        self.root = os.path.join(str(claude_home), "projects")
        self.clock = clock or time.time
        self.keep_s = keep_s
        self.factors = model_factors(env)
        self.lock = threading.RLock()
        self.files = {}      # path -> {"sid", "sub", "pos", "carry", "skip"}
        self.sess = {}       # session id -> {"cwd", "title", "last"}
        self.msgs = []       # (ts, sid, family, out, inp, cache_write, cache_read, thinking, is_subagent)
        self.ids = {}        # reply id -> ts: one API reply is written as several lines, and a resumed session copies old ones
        self.cmds = []       # {"t", "sid", "level", "what", "cmd", "id"} oldest first
        self.cmd_ids = set()
        self.scanned_at = 0.0
        self.passes = 0
        self.busy = False

    # ---- reading
    def refresh(self):
        """Read whatever was appended since the last pass. Cheap when nothing changed: one stat per recent session file."""
        with self.lock:
            if self.busy:
                return False
            self.busy = True
        try:
            now = self.clock()
            cutoff = now - self.keep_s
            for path, sid, sub, st in self._recent_files(cutoff):
                try:
                    self._read(path, sid, sub, st, cutoff)
                except (OSError, ValueError):
                    continue   # a file that vanished or cannot be read is that file's problem
            with self.lock:
                self._prune(cutoff)
                self.scanned_at = now
                self.passes += 1
            return True
        finally:
            with self.lock:
                self.busy = False

    def _recent_files(self, cutoff):
        try:
            projects = list(os.scandir(self.root))
        except OSError:
            return
        for proj in projects:
            try:
                if not proj.is_dir(follow_symlinks=False):
                    continue
                entries = list(os.scandir(proj.path))
            except OSError:
                continue
            for e in entries:
                try:
                    if e.name.endswith(".jsonl") and e.is_file(follow_symlinks=False):
                        st = e.stat()
                        if st.st_mtime >= cutoff:
                            yield e.path, e.name[:-6], False, st
                    elif e.is_dir(follow_symlinks=False):
                        sub = os.path.join(e.path, "subagents")
                        for s in (list(os.scandir(sub)) if os.path.isdir(sub) else []):
                            if s.name.endswith(".jsonl") and s.is_file(follow_symlinks=False):
                                st = s.stat()
                                if st.st_mtime >= cutoff:
                                    yield s.path, e.name, True, st
                except OSError:
                    continue

    def _first_ts(self, fh, pos, size):
        fh.seek(pos)
        m = TS_RE.search(fh.read(min(4 << 20, size - pos)))
        return calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0)) if m else None

    def _start(self, path, size, cutoff):
        """Where to begin a file seen for the first time: the first whole line at or after `cutoff`. Times only grow down a
        session file, so this is a bisection (about twenty small reads) instead of a walk through a file of hundreds of megabytes."""
        if size <= (1 << 20):
            return 0
        with open(path, "rb") as fh:
            lo, hi = 0, size
            while hi - lo > (1 << 20):
                mid = (lo + hi) // 2
                t = self._first_ts(fh, mid, size)
                if t is None or t >= cutoff:
                    hi = mid
                else:
                    lo = mid
            if lo == 0:
                return 0
            fh.seek(lo)
            nl = fh.read(min(LINE_CAP * 4, size - lo)).find(b"\n")
            return lo + nl + 1 if nl >= 0 else size

    def _read(self, path, sid, sub, st, cutoff):
        f = self.files.get(path)
        if f is None:
            f = self.files[path] = {"sid": sid, "sub": sub, "pos": self._start(path, st.st_size, cutoff), "carry": b"", "skip": False}
        elif st.st_size < f["pos"]:      # replaced by a shorter file: start it again
            f.update(pos=0, carry=b"", skip=False)
        if st.st_size <= f["pos"]:
            return
        with open(path, "rb") as fh:
            fh.seek(f["pos"])
            data = fh.read(min(st.st_size - f["pos"], READ_CAP))
        f["pos"] += len(data)
        parts = (f["carry"] + data).split(b"\n")
        f["carry"] = parts.pop()
        if f["skip"] and parts:          # the tail of a line too long to keep: it ends at the first newline
            parts.pop(0)
            f["skip"] = False
        if len(f["carry"]) > LINE_CAP:   # still inside a huge line: drop what was read of it and skip to its end
            f["carry"], f["skip"] = b"", True
        # No lock here: only this one pass ever writes, the lists are append-only until _prune swaps them, and a reader that
        # takes a list mid-pass just sees a few replies fewer. Holding the lock through megabytes of parsing would stall every
        # request that asks for the queue during the first pass after a restart.
        for raw in parts:
            self._line(raw, f, cutoff)

    def _line(self, raw, f, cutoff):
        if b'"type":"assistant"' not in raw and b'"type": "assistant"' not in raw:
            if b'"ai-title"' in raw and len(raw) < 4096:
                try:
                    d = json.loads(raw)
                    if d.get("type") == "ai-title" and isinstance(d.get("aiTitle"), str) and not f["sub"]:
                        self.sess.setdefault(f["sid"], {"cwd": "", "title": "", "last": 0.0})["title"] = one_line(d["aiTitle"], 80)
                except (ValueError, AttributeError):
                    pass
            return
        if len(raw) > LINE_CAP:
            return
        try:
            d = json.loads(raw)
        except ValueError:
            return
        if not isinstance(d, dict) or d.get("type") != "assistant":
            return
        ts = parse_ts(d.get("timestamp"))
        m = d.get("message")
        if ts is None or ts < cutoff or not isinstance(m, dict):
            return
        sid = f["sid"]
        s = self.sess.setdefault(sid, {"cwd": "", "title": "", "last": 0.0})
        s["last"] = max(s["last"], ts)
        if not f["sub"] and isinstance(d.get("cwd"), str) and d["cwd"]:
            s["cwd"] = d["cwd"][:400]
        u, mid = m.get("usage"), m.get("id")
        if isinstance(u, dict) and isinstance(mid, str) and mid and mid not in self.ids:
            self.ids[mid] = ts
            n = lambda k: max(0, int(u.get(k) or 0)) if isinstance(u.get(k), (int, float)) and not isinstance(u.get(k), bool) else 0
            det = u.get("output_tokens_details") if isinstance(u.get("output_tokens_details"), dict) else {}
            think = det.get("thinking_tokens") if isinstance(det.get("thinking_tokens"), int) else 0
            self.msgs.append((ts, sid, family(m.get("model")), n("output_tokens"), n("input_tokens"),
                              n("cache_creation_input_tokens"), n("cache_read_input_tokens"), max(0, think), bool(f["sub"])))
        content = m.get("content")
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            inp, bid = b.get("input"), str(b.get("id") or "")
            if not isinstance(inp, dict) or (bid and bid in self.cmd_ids):
                continue
            hit, shown = None, ""
            if b.get("name") == "Bash" and isinstance(inp.get("command"), str):
                hit, shown = risk_scan(inp["command"]), inp["command"]
            elif b.get("name") in ("Write", "Edit", "MultiEdit", "NotebookEdit") and isinstance(inp.get("file_path"), str) \
                    and SENSITIVE_PATH.search(inp["file_path"]):
                hit, shown = ("note", "changed a credentials or system file"), inp["file_path"]
            if hit:
                if bid:
                    self.cmd_ids.add(bid)
                self.cmds.append({"t": ts, "sid": sid, "level": hit[0], "what": hit[1], "cmd": one_line(redact(shown), 240), "id": bid, "sub": bool(f["sub"])})

    def _prune(self, cutoff):
        # Files are read one after another, so what was just added is in file order, not time order: put it in time order.
        self.msgs = sorted(m for m in self.msgs if m[0] >= cutoff)
        if len(self.ids) > 4 * max(1, len(self.msgs)) + 2000:
            self.ids = {k: t for k, t in self.ids.items() if t >= cutoff}
        self.cmds = sorted((c for c in self.cmds if c["t"] >= cutoff), key=lambda c: c["t"])[-CMD_KEEP:]
        self.cmd_ids = {c["id"] for c in self.cmds if c["id"]}
        for sid in [k for k, s in self.sess.items() if s["last"] < cutoff]:
            del self.sess[sid]
        for path in [p for p, f in self.files.items() if f["sid"] not in self.sess and not os.path.exists(p)]:
            del self.files[path]

    # ---- what the phone is shown
    def weight(self, fam, out, inp, cw, cr):
        return self.factors.get(fam, 1.0) * (W_OUT * out + W_IN * inp + W_CACHE_WRITE * cw + W_CACHE_READ * cr)

    def burn(self, span_s, top=8, buckets=24):
        """Who used the plan in the last `span_s` seconds. Rows are sessions, biggest first, each with its SHARE of the weighted
        total and its exact token counts; `series` is the whole host's burn over the span in `buckets` steps (0..1)."""
        with self.lock:
            msgs = list(self.msgs)
        now = self.clock()
        start = now - span_s
        rows, series, total = {}, [0.0] * buckets, 0.0
        for ts, sid, fam, out, inp, cw, cr, think, sub in msgs:
            if ts < start or ts > now + 60:
                continue
            w = self.weight(fam, out, inp, cw, cr)
            r = rows.setdefault(sid, {"w": 0.0, "out": 0, "ctx": 0, "think": 0, "n": 0, "sub_w": 0.0, "fam": {}, "last": 0.0})
            r["w"] += w; r["out"] += out; r["ctx"] += inp + cw + cr; r["think"] += think; r["n"] += 1
            r["fam"][fam] = r["fam"].get(fam, 0.0) + w
            r["last"] = max(r["last"], ts)
            if sub:
                r["sub_w"] += w
            total += w
            series[min(buckets - 1, max(0, int((ts - start) / span_s * buckets)))] += w
        order = sorted(rows.items(), key=lambda kv: -kv[1]["w"])
        out_rows = []
        for sid, r in order[:top]:
            s = self.sess.get(sid) or {}
            fams = sorted(r["fam"].items(), key=lambda kv: -kv[1])
            out_rows.append({"sid": sid, "share": round(r["w"] / total, 4) if total else 0.0, "out": r["out"], "ctx": r["ctx"], "think": r["think"],
                             "replies": r["n"], "model": fams[0][0] if fams else "?", "models": [f for f, _w in fams][:3],
                             "helpers": round(r["sub_w"] / r["w"], 3) if r["w"] else 0.0, "last": r["last"],
                             "cwd": s.get("cwd", ""), "title": s.get("title", "")})
        rest = order[top:]
        peak = max(series) or 1.0
        by_model = {}
        for _sid, r in order:
            for fam, w in r["fam"].items():
                by_model[fam] = by_model.get(fam, 0.0) + w
        return {"span_s": span_s, "rows": out_rows, "others": {"n": len(rest), "share": round(sum(r["w"] for _s, r in rest) / total, 4) if total else 0.0},
                "sessions": len(order), "replies": sum(r["n"] for _s, r in order), "out": sum(r["out"] for _s, r in order),
                "ctx": sum(r["ctx"] for _s, r in order), "series": [round(v / peak, 3) for v in series],
                "models": [{"model": f, "share": round(w / total, 4) if total else 0.0} for f, w in sorted(by_model.items(), key=lambda kv: -kv[1])][:4],
                "ready": self.passes > 0, "scanned_at": self.scanned_at, "now": now}

    def risks(self, since=0.0, limit=100):
        """Risky commands that ran after `since`, newest first, each with where its session runs."""
        with self.lock:
            cmds = list(self.cmds)   # a pass in flight appends in file order, so sort here instead of trusting the order
        out = []
        for c in sorted((c for c in cmds if c["t"] > since), key=lambda c: -c["t"])[:limit]:
            s = self.sess.get(c["sid"]) or {}
            out.append(dict(c, cwd=s.get("cwd", ""), title=s.get("title", "")))
        return out

    def session(self, sid):
        with self.lock:
            s = self.sess.get(sid)
            return dict(s) if s else None
