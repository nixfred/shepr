#!/usr/bin/env python3
"""Shepr server.

Static files, /ping, and a small /api that fronts the `herdr` CLI so a phone can see which agent
sessions are waiting on a human, read the tail of one, and answer it. Standard library only.

    python3 server.py [port] --tailnet      the host serves: its own TLS on this node's tailnet addresses
    python3 server.py [port] [--bind ADDR]  loopback only, for a `tailscale serve` (or a proxy of your own) in front

With --tailnet the server listens on 127.0.0.1 (plain HTTP, for this machine) and on the node's own tailnet addresses,
bound to the Tailscale interface, with the certificate `tailscale cert` hands out, and it asks `tailscale whois` whose
device is on the other end of every connection before it reads a byte from it. Without it, it listens on loopback only
and trusts the Tailscale-User-Login header that `tailscale serve` adds.

The rules this file exists to keep:

  * Reads are free. Writes confirm. A send is TWO requests: /api/send/prepare returns exactly what
    will happen plus a one-use nonce, /api/send/commit executes it. Nothing here sends on its own.
  * Never auto-approve. There is no endpoint, flag or timer that answers a dialog without a person
    having seen the plan for THAT dialog. The plan is bound to the agent's state_change_seq, so if
    the agent moved on between the preview and the tap, the commit is refused.
  * Everything read from a pane is DATA. It is returned as text and never interpreted here.
  * A failed write may still have landed. Errors say so and nothing is ever retried automatically.
"""
import argparse
import base64
import calendar
import concurrent.futures
import contextlib
import email.header
import errno
import fcntl
import functools
import glob
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import socketserver
import ssl
import stat
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

HERE = os.path.dirname(os.path.realpath(__file__))   # realpath: `shepr` is a symlink to this file
sys.path.insert(0, HERE)   # usage.py sits beside this file, whichever way it was started or imported
import usage  # noqa: E402
import live as shepr_live  # noqa: E402   (the Lock Screen Live Activity: ios/ draws it, this feeds it)
import watch as shepr_watch  # noqa: E402   (what the agents here have been doing, from Claude Code's own session files)
HERDR = os.environ.get("HERDR_BIN", "herdr")
LOCAL = os.environ.get("SHEPR_LABEL") or socket.gethostname().split(".")[0]
READONLY = os.environ.get("SHEPR_READONLY") == "1"
POLL_S = float(os.environ.get("SHEPR_POLL", "15"))
STATE_DIR = os.environ.get("SHEPR_STATE") or os.path.expanduser("~/.local/state/shepr")
CLAUDE_HOME = os.environ.get("CLAUDE_HOME") or os.path.expanduser("~/.claude")
NONCE_TTL_S = 300   # a phone user reads the sheet; the preview is also bound to state_change_seq, so a longer wait cannot approve a stale screen
TAIL_MAX_BYTES = 48 * 1024

PANE_RE = re.compile(r"^w[0-9A-Za-z]+:p[0-9A-Za-z]+$")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
OPT_RE = re.compile(r"^\s*(❯|>)?\s*(\d{1,2})\.\s+(\S.*?)\s*$")
CHK_RE = re.compile(r"^\[( |✔|✓|x|X)\]\s*(.*)$")             # a multi-select option: "[ ] label" or "[✔] label"
ACT_RE = re.compile(r"^\s*(❯|>)?\s+(Submit|Next)\s*$")           # the unnumbered row that ends a multi-select question
FREE_RE = re.compile(r"^type something", re.I)
APPROVE_RE = re.compile(r"^(yes|allow|approve|accept|proceed|always|trust|continue)\b", re.I)

# What a phone may press. Letters and digits cover y/n and numbered menus; no ctrl-chords, because
# ctrl+c twice quits Claude Code and that is not something to do from a thumb.
KEY_NAMED = {"up", "down", "left", "right", "enter", "esc", "tab", "shift+tab", "space", "backspace"}
KEY_CHAR = re.compile(r"^[0-9A-Za-z]$")
# Claude Code's own keyboard hints that do nothing in Shepr ("Press up to edit queued messages", "(ctrl+o to expand)") are hidden from the
# tail; the one that matters, "ctrl+enter to send now", becomes the Send now button.
QUEUED_RE = re.compile(r"^\s*ctrl\+enter to send now\s*$", re.I | re.M)
HINT_LINE_RE = re.compile(r"^\s*(?:\u276f\s*)?(?:Press up to edit queued messages|ctrl\+enter to send now)\s*$", re.I)
HINT_INLINE_RE = re.compile(r"\s*\((?:ctrl|shift)\+[a-z]+ to (?:expand|show|toggle|view|edit)[^)]*\)", re.I)


def clean_tail(text):
    """The pane text without keyboard hints that cannot be used from the phone, and whether a message is queued."""
    queued = bool(QUEUED_RE.search(text))
    lines = [HINT_INLINE_RE.sub("", l) for l in text.split("\n") if not HINT_LINE_RE.match(l)]
    return "\n".join(lines), queued

LOCK = threading.RLock()
SNAP = {"ts": 0.0, "hosts": [], "agents": [], "polls": 0}
PLANS = {}
LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "[::1]"})
OWNER = {"login": None, "dns": None, "names": set(LOOPBACK_NAMES), "ips": []}


class HerdrError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


# --------------------------------------------------------------------------------------------
# talking to herdr
# --------------------------------------------------------------------------------------------
MAX_OUT = 4 << 20   # bytes kept from one herdr call; a runaway remote cannot grow this process without limit


def bounded_run(cmd, timeout):
    """subprocess.run with a byte cap on stdout and stderr: past MAX_OUT the child is killed."""
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    bufs, over = {"out": bytearray(), "err": bytearray()}, threading.Event()

    def pump(name, f):
        while True:
            chunk = f.read(65536)
            if not chunk:
                return
            room = MAX_OUT - len(bufs[name])
            bufs[name] += chunk[:max(room, 0)]
            if len(chunk) > room:
                over.set()
                p.kill()
                return
    ts = [threading.Thread(target=pump, args=(n, f), daemon=True) for n, f in (("out", p.stdout), ("err", p.stderr))]
    for t in ts:
        t.start()
    try:
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        raise
    for t in ts:
        t.join(2)
    if over.is_set():
        raise HerdrError("too_big", f"herdr answered with more than {MAX_OUT >> 20} MB, so it was cut off")
    dec = lambda b: bytes(b).decode("utf-8", "replace")
    return p.returncode, dec(bufs["out"]), dec(bufs["err"])


class Session(str):
    """The `machine` of a host that is a named herdr session on THIS machine; the string is the session's name. It is a str so
    every place that carries host["machine"] around keeps working, and run_herdr is the only place that tells it from a
    saved machine's label."""


def run_herdr(machine, args, timeout=10, want_json=True):
    """Run one herdr command. machine=None is this host's plain session; a Session is another named session on this host;
    otherwise it is a saved machine label. --machine goes in front of the subcommand, never combined with --session (herdr --skill).
    --session is sent as a flag, not through HERDR_SOCKET_PATH: the flag wins over a socket path this process inherited
    (checked on herdr 0.9.5: 5 agents with --session hrtest, 18 without, with the inherited path pointing at the default)."""
    route = ["--session", str(machine)] if isinstance(machine, Session) else (["--machine", machine] if machine else [])
    cmd = [HERDR] + route + list(args)
    try:
        rc, out, err = bounded_run(cmd, timeout)
    except subprocess.TimeoutExpired:
        raise HerdrError("timeout", f"herdr took longer than {timeout}s")
    except FileNotFoundError:
        raise HerdrError("no_herdr", f"{HERDR} not found")
    if rc != 0:
        msg = (err or out).strip()
        try:
            e = json.loads(msg)["error"]
            raise HerdrError(e.get("code", "error"), e.get("message", msg))
        except (ValueError, KeyError, TypeError):
            raise HerdrError("error", msg.splitlines()[-1] if msg else f"exit {rc}")
    if not want_json:
        return out
    try:
        return json.loads(out)
    except ValueError:
        raise HerdrError("bad_json", "herdr returned something that is not JSON")


_tls = threading.local()   # per thread: did THIS thread's last discover_hosts() really read herdr's saved-machine list?


SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")   # it becomes a command-line argument: never a leading dash
SESSIONS_TTL_S = 5.0
_sessions = {"at": 0.0, "found": [], "read": False}


def _same_socket(a, b):
    return bool(a) and bool(b) and os.path.realpath(os.path.expanduser(a)) == os.path.realpath(os.path.expanduser(b))


def local_sessions():
    """(found, read): the running NAMED herdr sessions on this machine that a plain `herdr` call does not already reach, as
    [(name, Session)], and whether the list was really read. A plain call reaches the socket in HERDR_SOCKET_PATH, or the default
    session's when that is unset; that one is the host called LOCAL and is never listed again (a Shepr started with the socket of
    a named session, as the old workaround did, would otherwise show every agent twice). Read through `herdr session list --json`,
    kept for SESSIONS_TTL_S because discover_hosts runs on every request. SHEPR_SESSIONS limits it: unset means every running
    session, a comma list means only those names ("default" is LOCAL's own and adds nothing)."""
    now = time.time()
    if now - _sessions["at"] < SESSIONS_TTL_S:
        return _sessions["found"], _sessions["read"]
    found, read = [], False
    only = os.environ.get("SHEPR_SESSIONS")
    only = None if only is None or only.strip().lower() in ("", "all") else {n.strip() for n in only.split(",") if n.strip()}
    if only is not None and not only - {"default"}:
        found, read = [], True   # asked for nothing beyond the plain session: no herdr call at all
    else:
        try:
            raw = run_herdr(None, ["session", "list", "--json"], timeout=6)
            rows = raw.get("sessions") if isinstance(raw, dict) else raw
            if isinstance(rows, list):
                rows = [r for r in rows if isinstance(r, dict) and isinstance(r.get("name"), str)]
                plain = os.environ.get("HERDR_SOCKET_PATH") or next((r.get("socket_path") for r in rows if r.get("default")), None)
                for r in rows:
                    name = r["name"]
                    up = r.get("running") is True or r.get("status") == "running"
                    if not up or not SESSION_NAME_RE.match(name) or (only is not None and name not in only):
                        continue
                    if r.get("default") and not os.environ.get("HERDR_SOCKET_PATH"):
                        continue   # the plain call already reaches it
                    if _same_socket(r.get("socket_path"), plain):
                        continue
                    found.append((name, Session(name)))
                read = True
        except HerdrError:
            pass   # an older herdr without `session list`, or herdr down: the plain host is all there is
    _sessions.update(at=now, found=found, read=read)
    return found, read


def discover_hosts():
    """Every saved herdr machine, this host, and the other running herdr sessions on this host. Sets _tls.listed: True only
    when the saved-machine list was really read, and _tls.sessions_listed: True only when the session list was. They are
    separate because they vouch for different hosts: a herdr with no `session list` must not stop a vanished MACHINE from
    being forgotten, and a failed machine list must not make a session look gone. A failed or skipped read leaves fewer hosts
    in the answer, and that must never be taken for "every other machine was removed". They are per thread, so another
    thread's good read can never vouch for this thread's failed one."""
    _tls.listed = _tls.sessions_listed = False
    hosts = [{"id": LOCAL, "machine": None, "kind": "local", "enabled": True}]
    if os.environ.get("SHEPR_LOCAL_ONLY"):   # tests: never read real remote machines or other sessions, so one being up or down cannot change a result
        return hosts
    machines_read = True
    try:
        for m in run_herdr(None, ["machine", "list", "--json"], timeout=6):
            label = m.get("label") or m.get("id")
            if label and label != LOCAL:
                hosts.append({"id": label, "machine": label, "kind": "remote", "enabled": bool(m.get("enabled"))})
    except HerdrError:
        machines_read = False
    found, sessions_read = local_sessions()
    for name, route in found:   # "<this host>/<session>": the same machine, so kind stays "local" (transcripts, uploads, folders all apply)
        hid = f"{LOCAL}/{name}"
        if all(h["id"] != hid for h in hosts):   # a saved machine that happens to be called this keeps the name
            hosts.append({"id": hid, "machine": route, "kind": "local", "enabled": True, "session": name})
    _tls.listed, _tls.sessions_listed = machines_read, sessions_read
    return hosts


GONE_POLLS = 2   # saved-machine lists in a row, each really read, that must lack a machine before its ledger is dropped
_absent = {}     # host id -> how many such lists in a row have lacked it


def forget_gone_hosts(hosts, listed, sessions_listed=None):
    """A saved machine can go away for good (a throwaway container, a cloud box that was deleted). Its ledger rows would
    stay in seen.json for ever, and if the name is used again (a container id that is recycled gives the same label) the new
    machine would inherit the old one's "blocked for" times. So once `GONE_POLLS` real lists in a row have lacked a host,
    its host entry and every ledger row of it are dropped. A list that could not be read changes nothing. A named session of
    this host ("<host>/<name>") is judged by the session list, every other host by the saved-machine list; sessions_listed
    left out means the same as `listed`."""
    if sessions_listed is None:
        sessions_listed = listed
    if not (listed or sessions_listed):
        return
    ids = {h["id"] for h in hosts}
    changed = False
    with LOCK:
        for hid in [k for k in _absent if k in ids]:
            del _absent[hid]
        for hid in [k for k in STATE["hosts"] if k not in ids]:
            if not (sessions_listed if hid.startswith(LOCAL + "/") else listed):
                continue   # the list that would have named this host was not read
            _absent[hid] = _absent.get(hid, 0) + 1
            if _absent[hid] < GONE_POLLS:
                continue
            del _absent[hid]
            del STATE["hosts"][hid]
            for k in [k for k in STATE["seen"] if k.startswith(hid + "|")]:
                del STATE["seen"][k]
            changed = True
        if changed:
            try:
                save_state(STATE)
            except OSError as e:
                print(f"shepr: could not save the ledger: {e!r}", file=sys.stderr, flush=True)


def clean_text(s):
    return CTRL_RE.sub("", ANSI_RE.sub("", s)).replace("\r", "")


def read_tail(host, pane, lines, max_bytes=TAIL_MAX_BYTES):
    out = run_herdr(host["machine"], ["agent", "read", pane, "--source", "recent-unwrapped",
                                      "--lines", str(lines)], timeout=10 if lines <= 200 else 20, want_json=False)
    text = clean_text(out).rstrip()
    ls = text.split("\n")[-lines:]
    text = "\n".join(l[:600] for l in ls)
    return text[-max_bytes:]


def get_agent(host, pane):
    r = run_herdr(host["machine"], ["agent", "get", pane], timeout=8)
    return r["result"]["agent"]


# --------------------------------------------------------------------------------------------
# "blocked since" -- herdr exposes a change counter but no clock, so we keep our own.
# --------------------------------------------------------------------------------------------
def state_path(name):
    return os.path.join(STATE_DIR, name)


def load_state():
    try:
        with open(state_path("seen.json")) as f:
            d = json.load(f)
        d.setdefault("hosts", {}); d.setdefault("seen", {})
        return d
    except (OSError, ValueError):
        return {"hosts": {}, "seen": {}}


def save_state(d):
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    tmp = state_path("seen.json.tmp")
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, state_path("seen.json"))


STATE = load_state()


def transcript_file(agent):
    """Local Claude only: the path of this session's transcript (every message ever, unlike the ~1000 lines
    herdr keeps), or None. The session id comes from herdr but only ever picks a file under CLAUDE_HOME/projects."""
    sess = agent.get("agent_session") or {}
    sid = sess.get("value")
    if sess.get("agent") != "claude" or sess.get("kind") != "id" or not sid or not re.fullmatch(r"[0-9A-Za-z._-]+", sid):
        return None
    for cwd in (agent.get("cwd"), agent.get("foreground_cwd")):
        if cwd:
            p = os.path.join(CLAUDE_HOME, "projects", re.sub(r"[^A-Za-z0-9]", "-", cwd), sid + ".jsonl")
            if os.path.exists(p):
                return p
    hits = glob.glob(os.path.join(CLAUDE_HOME, "projects", "*", sid + ".jsonl"))
    return hits[0] if hits else None


def transcript_mtime(agent):
    """The session transcript stops being written the moment the agent blocks, so its mtime is a good
    estimate of WHEN it blocked for a session that was already blocked before we started watching."""
    p = transcript_file(agent)
    try:
        return os.path.getmtime(p) if p else None
    except OSError:   # the session file went away between the look and the stat
        return None


# --------------------------------------------------------------------------------------------
# full history: the transcript, read BACKWARDS a page at a time, so a 50 MB session scrolls forever
# --------------------------------------------------------------------------------------------
HIST_ENTRIES = 80            # rendered entries per page (a page holds fewer if it hits the scan cap)
HIST_SCAN = 8 << 20          # most bytes read for one page
HIST_LINE = 8 << 20          # a single transcript line larger than this is not shown
SKIP_TEXT = ("<system-reminder>", "<command-name>", "<command-message>", "<local-command", "<user-prompt-submit-hook>")


def _clip(text, n):
    text = text.strip("\n")
    return text if len(text) <= n else text[:n] + f"\n… ({len(text) - n} more characters)"


def _tool_line(b):
    name, inp = b.get("name") or "tool", b.get("input") or {}
    if not isinstance(inp, dict):
        inp = {}
    arg = inp.get("command") or inp.get("file_path") or inp.get("path") or inp.get("pattern") or inp.get("url") \
        or inp.get("query") or inp.get("description") or inp.get("prompt") or ""
    if not arg and inp:
        arg = json.dumps(inp, ensure_ascii=False)
    return f"{name}({_clip(str(arg), 400)})"


def _result_text(b):
    c = b.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join((x.get("text") or "[image]") if isinstance(x, dict) else str(x) for x in c)
    return ""


def render_line(raw):
    """One transcript line -> the entries worth reading (message text, tool calls, short tool results)."""
    try:
        d = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(d, dict):
        return []
    t, ts = d.get("type"), d.get("timestamp")
    if t == "system":
        return [{"ts": ts, "r": "divider", "x": "the conversation was compacted here"}] if d.get("subtype") == "compact_boundary" else []
    if t not in ("user", "assistant") or d.get("isMeta") or d.get("isSidechain"):
        return []
    c = (d.get("message") or {}).get("content")
    blocks = [{"type": "text", "text": c}] if isinstance(c, str) else [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []
    out = []
    for b in blocks:
        bt = b.get("type")
        if bt == "text":
            txt = b.get("text") or ""
            if not txt.strip() or (t == "user" and txt.lstrip().startswith(SKIP_TEXT)):
                continue
            out.append({"ts": ts, "r": "user" if t == "user" else "assistant", "x": _clip(txt, 12000)})
        elif bt == "tool_use":
            out.append({"ts": ts, "r": "tool", "x": _tool_line(b)})
        elif bt == "tool_result":
            out.append({"ts": ts, "r": "result", "x": _clip(_result_text(b), 700), "err": bool(b.get("is_error"))})
        elif bt == "image":
            out.append({"ts": ts, "r": "result", "x": "[image]"})
    return out


def history_page(path, before):
    """The page of transcript that ENDS at byte offset `before` (None = the end of the file).
    Returns (entries oldest-first, offset to ask for next or None at the start of the file, file size, note)."""
    size = os.path.getsize(path)
    end = size if before is None else max(0, min(int(before), size))
    pos, scanned, carry = end, 0, b""
    got, oldest, note = [], end, None
    with open(path, "rb") as f:
        while pos > 0 and len(got) < HIST_ENTRIES and scanned < HIST_SCAN:
            step = min(256 * 1024, pos)
            pos -= step
            f.seek(pos)
            chunk = f.read(step) + carry
            scanned += step
            parts = chunk.split(b"\n")
            if pos > 0:                      # the first piece is the tail of a line that began earlier
                carry, parts, off = parts[0], parts[1:], pos + len(parts[0]) + 1
                if len(carry) > HIST_LINE:
                    # Still inside a line too long to show. `pos` is the earliest byte read and lies before where this page
                    # began, so the next page walks further back. Offset 0 would tell the phone the session starts here and
                    # hide every message older than this line.
                    note, oldest, carry = "an entry too large to show was skipped here", pos, b""
                    break
            else:
                carry, off = b"", 0
            starts = []
            for part in parts:
                starts.append(off)
                off += len(part) + 1
            stop = False
            for part, st in zip(reversed(parts), reversed(starts)):
                if part.strip():
                    got.extend(reversed(render_line(part)))   # newest-first while scanning
                oldest = st
                if len(got) >= HIST_ENTRIES:
                    stop = True
                    break
            if stop:
                break
    got.reverse()
    return got, (oldest if oldest > 0 else None), size, note


def note_seen(host, agents, now, ws=None):
    """Record the first time each blocked/done agent was seen in its current state."""
    hid = host["id"]
    hs = STATE["hosts"].setdefault(hid, {"watching_since": now, "last_ok": None})
    prev_ok = hs.get("last_ok")
    exact_ok = prev_ok is not None and now - prev_ok <= 3 * POLL_S
    live, changed = set(), False
    live_w = set()
    for a in agents:
        if a["agent_status"] == "working":   # when a turn began, for "working 12m" and for "quiet since": kept in memory only
            wk = f"{hid}|{a['pane_id']}|{a.get('state_change_seq')}"
            live_w.add(wk)
            WORKING.setdefault(wk, {"t": now, "exact": exact_ok})
        if a["agent_status"] not in ("blocked", "done"):
            continue
        key = f"{hid}|{a['pane_id']}|{a.get('state_change_seq')}|{a['agent_status']}"
        live.add(key)
        if key in STATE["seen"]:
            continue
        t, basis = now, "observed" if exact_ok else "first-look"
        if not exact_ok and host["kind"] == "local":
            mt = transcript_mtime(a)
            if mt and mt < now:
                t, basis = mt, "transcript"
        label = (ws or {}).get(a.get("workspace_id")) or os.path.basename((a.get("cwd") or a.get("foreground_cwd") or "").rstrip("/")) or a["pane_id"]
        STATE["seen"][key] = {"t": t, "exact": exact_ok, "basis": basis, "label": label}
        if exact_ok:   # a transition we watched happen (not what was already true when the host started)
            PUSH_EVENTS.append(a["agent_status"])
            ask = a.get("_ask") if isinstance(a.get("_ask"), dict) else {}
            log_event(a["agent_status"], hid, a["pane_id"], label, **({"q": str(ask["q"])[:160]} if a["agent_status"] == "blocked" and ask.get("q") else {}))
        changed = True
    for key in [k for k in STATE["seen"] if k.startswith(hid + "|") and k not in live]:
        ent = STATE["seen"].pop(key); changed = True
        if exact_ok and ent.get("exact") and key.endswith("|blocked"):   # a wait we watched from its first moment to its last
            record_wait(hid, ent.get("label") or key.split("|")[1], ent["t"], now)
            log_event("answered", hid, key.split("|")[1], ent.get("label") or key.split("|")[1], wait=int(max(0, now - ent["t"])))
    for wk in [k for k in WORKING if k.startswith(hid + "|") and k not in live_w]:
        del WORKING[wk]
        QUIET_TOLD.discard(wk)
    hs["last_ok"] = now
    if changed or prev_ok is None or now - prev_ok > 60:
        try:
            save_state(STATE)
        except OSError as e:
            print(f"shepr: cannot persist state: {e}", file=sys.stderr)


ASLEEP_N = {}   # host id -> how many herdr workspaces have no agent running (the count behind "Asleep")

# ---- the watch: what agents are doing, on top of whether they wait. See watch.py for the reading of Claude Code's session files.
WATCH_ON = os.environ.get("SHEPR_WATCH", "1") != "0"
WATCH_EVERY_S = max(5.0, float(os.environ.get("SHEPR_WATCH_S", "60")))
QUIET_S = max(60.0, float(os.environ.get("SHEPR_QUIET_MIN", "15")) * 60)   # a working agent that has written nothing for this long is "quiet"
FLEET_STOP_S = 45.0              # nothing working for this long, with something waiting, is "everything has stopped"
WATCH_FIXTURE = os.environ.get("SHEPR_WATCH_FIXTURE")   # tests only: {"burn": {...}, "risks": [...]} returned as it is
WATCH = shepr_watch.Watch(CLAUDE_HOME)
BOOT_T = time.time()
WORKING = {}        # "host|pane|seq" -> {"t", "exact"}: when Shepr saw that turn begin
QUIET_TOLD = set()  # working keys already reported as quiet, so one silence is one notice
SID_AGENT = {}      # Claude session id -> {"host", "pane", "label"} for sessions that are open in herdr right now
FLEET = {"had_work": False, "stopped_at": None, "told": False}
RISK_SEEN = set()   # tool-use ids of risky commands already written to the activity log
EVENT_KEEP_DAYS = 7
EVENT_MAX_BYTES = 2 << 20


def log_event(ev, host=None, pane=None, label=None, t=None, **more):
    """One line in events.jsonl: something happened that a person may want to read later ("what did they do while I was away").
    Owner-only, like every file here. A failure to write is never the caller's problem."""
    rec = {"t": round(t if t is not None else time.time(), 1), "ev": ev}
    if host:
        rec["host"] = host
    if pane:
        rec["pane"] = pane
    if label:
        rec["label"] = str(label)[:60]
    rec.update({k: v for k, v in more.items() if v is not None})
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        fd = os.open(state_path("events.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_events(since, limit=2000):
    """The events newer than `since`, oldest first. Only the tail of the file is read."""
    out = []
    try:
        with open(state_path("events.jsonl"), "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - EVENT_MAX_BYTES))
            lines = f.read().decode("utf-8", "replace").split("\n")
        if size > EVENT_MAX_BYTES:
            lines = lines[1:]   # the first one may be half a line
    except OSError:
        return out
    for line in lines:
        try:
            e = json.loads(line)
            if isinstance(e, dict) and isinstance(e.get("ev"), str) and float(e["t"]) > since:
                out.append(e)
        except (ValueError, KeyError, TypeError):
            continue
    return out[-limit:]


def prune_events():
    """Once at start: drop what is older than EVENT_KEEP_DAYS, and remember which risky commands were already logged."""
    keep = read_events(time.time() - EVENT_KEEP_DAYS * 86400, limit=20000)
    RISK_SEEN.update(e["id"] for e in keep if e.get("ev") == "risk" and isinstance(e.get("id"), str))
    try:
        with open(state_path("events.jsonl"), "rb") as f:
            before = sum(1 for _ in f)
    except OSError:
        return
    if len(keep) < before:
        fd = os.open(state_path("events.jsonl.tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            for e in keep:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.replace(state_path("events.jsonl.tmp"), state_path("events.jsonl"))


def session_written(agent):
    """Local Claude only: when this session, or one of its helpers, last wrote to its session file. None when there is no file."""
    p = transcript_file(agent)
    if not p:
        return None
    try:
        t = os.path.getmtime(p)
        sub = os.path.join(p[:-len(".jsonl")], "subagents")
        if os.path.isdir(sub):
            for e in os.scandir(sub):
                if e.name.endswith(".jsonl"):
                    t = max(t, e.stat().st_mtime)
        return t
    except OSError:
        return None


def watch_local(host, agents, ws, now):
    """For a host that is this machine: which Claude session each open terminal holds, when each working one last wrote, and
    which have gone quiet. Called under LOCK from the poll. A remote host has no session files here, so it is never judged."""
    hid = host["id"]
    for sid in [k for k, v in SID_AGENT.items() if v["host"] == hid]:
        del SID_AGENT[sid]
    for a in agents:
        sess = a.get("agent_session") or {}
        label = (ws or {}).get(a.get("workspace_id")) or os.path.basename((a.get("cwd") or "").rstrip("/")) or a["pane_id"]
        if sess.get("agent") == "claude" and sess.get("kind") == "id" and isinstance(sess.get("value"), str):
            SID_AGENT[sess["value"]] = {"host": hid, "pane": a["pane_id"], "label": label, "status": a["agent_status"]}
        if a["agent_status"] != "working":
            continue
        wrote = session_written(a)
        a["_active"] = wrote
        wk = f"{hid}|{a['pane_id']}|{a.get('state_change_seq')}"
        began = WORKING.get(wk) or {"t": now, "exact": False}
        if wrote is None:
            continue
        quiet = now - max(wrote, began["t"] if began["exact"] else wrote)
        if quiet >= QUIET_S and wk not in QUIET_TOLD:
            QUIET_TOLD.add(wk)
            log_event("quiet", hid, a["pane_id"], label, mins=int(quiet // 60))
            PUSH_EVENTS.append("quiet")
        elif quiet < QUIET_S / 2:
            QUIET_TOLD.discard(wk)   # it wrote again: a later silence is news again


def fleet_watch(agents, hosts, now):
    """Everything has stopped: nothing is working any more and something is waiting on a person. Said once per stop, and only
    after it has held for FLEET_STOP_S (an agent between two steps is not a stop). Never judged while a host cannot be read."""
    if any(h.get("status") == "down" for h in hosts):
        return
    working = sum(1 for a in agents if a["status"] == "working")
    blocked = sum(1 for a in agents if a["status"] == "blocked")
    done = sum(1 for a in agents if a["status"] == "done")
    if working:
        FLEET.update(had_work=True, stopped_at=None, told=False)
    elif not (blocked or done):
        FLEET.update(stopped_at=None)
    elif FLEET["had_work"]:
        if FLEET["stopped_at"] is None:
            FLEET["stopped_at"] = now
        elif not FLEET["told"] and now - FLEET["stopped_at"] >= FLEET_STOP_S:
            FLEET["told"] = True
            log_event("stopped", blocked=blocked, done=done)
            PUSH_EVENTS.append("stopped")


def watch_risks():
    """The risky commands the watch has seen (or the test fixture's), newest first."""
    if WATCH_FIXTURE:
        return [r for r in (_watch_fixture().get("risks") or []) if isinstance(r, dict)]
    return WATCH.risks(limit=shepr_watch.CMD_KEEP)


def _watch_fixture():
    try:
        with open(WATCH_FIXTURE) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def note_risks():
    """Write each newly seen risky command to the activity log once. One that ran after this server started, and only in the
    last few minutes, also counts as news for a phone that asked to be told (a danger, never a mere note)."""
    now = time.time()
    with LOCK:
        for r in reversed(watch_risks()):   # oldest first, so the log stays in time order
            rid = r.get("id") or f"{r.get('sid')}@{r.get('t')}"
            if rid in RISK_SEEN:
                continue
            RISK_SEEN.add(rid)
            ag = SID_AGENT.get(r.get("sid")) or {}
            label = ag.get("label") or os.path.basename(str(r.get("cwd") or "").rstrip("/")) or str(r.get("sid") or "")[:8]
            log_event("risk", ag.get("host") or LOCAL, ag.get("pane"), label, t=r.get("t"), level=r.get("level"), what=r.get("what"),
                      cmd=str(r.get("cmd") or "")[:240], id=rid, helper=bool(r.get("sub")) or None)
            if r.get("level") == "danger" and (r.get("t") or 0) > max(BOOT_T, now - 600):
                PUSH_EVENTS.append("risk")
        pending = bool(PUSH_EVENTS)
    if pending:
        _publish()   # hands the events to the push dispatcher


def watch_loop():
    time.sleep(5)   # the first poll goes first
    first, pruned = True, time.time()
    while True:
        try:
            t0 = time.time()
            if t0 - pruned > 86400:   # a server that runs for months must not let the activity log grow without end
                pruned = t0
                prune_events()
            if not WATCH_FIXTURE:
                WATCH.refresh()
            note_risks()
            if first:   # one journal line, so a watch that is alive is visibly alive (and how much the first pass cost)
                first = False
                print(f"shepr: watch: {len(WATCH.sess)} Claude sessions written in the last {int(shepr_watch.KEEP_S // 3600)} h, {len(WATCH.msgs)} replies, "
                      f"{len(WATCH.cmds)} risky commands; the first pass took {time.time() - t0:.1f} s", file=sys.stderr, flush=True)
        except Exception as e:   # the watch must never take the server with it
            print(f"shepr: watch pass failed: {e!r}", file=sys.stderr, flush=True)
        time.sleep(WATCH_EVERY_S)


def poll_host(host):
    try:
        return _poll_host(host)
    except Exception as e:   # a host that answers with the wrong shape is that host's problem, not the whole queue's
        return host, None, {}, f"bad_response: {type(e).__name__}: {e}"


def _poll_host(host):
    now = time.time()
    try:
        agents = run_herdr(host["machine"], ["agent", "list"], timeout=9)["result"]["agents"]
    except HerdrError as e:
        return host, None, {}, f"{e.code}: {e.message}"
    except (KeyError, TypeError):
        return host, None, {}, "bad_response: herdr's agent list is not in the expected shape"
    if not isinstance(agents, list):
        return host, None, {}, "bad_response: herdr's agent list is not a list"
    agents = [a for a in agents if isinstance(a, dict) and isinstance(a.get("pane_id"), str)
              and isinstance(a.get("agent_status"), str)]
    for a in [x for x in agents if x["agent_status"] == "blocked"][:6]:
        try:   # what is it waiting on? the question on screen, or "nothing", which is itself worth knowing
            m = parse_menu(read_tail(host, a["pane_id"], 40))
            a["_ask"] = {"q": (m or {}).get("question"), "n": len((m or {}).get("options") or []), "menu": bool(m)}
        except Exception:
            a["_ask"] = None
    try:
        wlist = run_herdr(host["machine"], ["workspace", "list"], timeout=9)["result"]["workspaces"]
        ws = {w["workspace_id"]: w.get("label") for w in wlist}
        ASLEEP_N[host["id"]] = sum(1 for w in wlist if isinstance(w, dict) and w.get("agent_status") == "unknown")
    except (HerdrError, KeyError, TypeError):
        ws = {}
    with LOCK:
        note_seen(host, agents, now, ws)
        if host["kind"] == "local":
            watch_local(host, agents, ws, now)
    return host, agents, ws, None


HOST_SNAP = {}       # host id -> {"entry": {...}, "agents": [...]}: each host publishes on its OWN clock
HOST_ORDER = []
_inflight = {}       # host id -> the Future of its poll in flight (one poll per host at a time)
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=16)


def _publish():
    """Put every host's latest answer together. A slow or dead host keeps its old (or 'down') entry and never holds the others back."""
    with LOCK:
        hosts, agents = [], []
        for hid in HOST_ORDER:
            e = HOST_SNAP.get(hid)
            if e:
                hosts.append(e["entry"]); agents.extend(e["agents"])
        SNAP.update(ts=time.time(), hosts=hosts, agents=agents, polls=SNAP["polls"] + 1)
        fleet_watch(agents, hosts, SNAP["ts"])
        events = PUSH_EVENTS[:]; PUSH_EVENTS.clear()
    if events:
        threading.Thread(target=dispatch_push, args=(events,), daemon=True).start()
    if LIVE.active():   # most installs never set the Lock Screen card up: then this is one dict lookup
        try:
            LIVE.submit(build_live_status())
        except Exception as e:   # the card must never stop the queue
            print(f"shepr: live status failed: {e!r}", file=sys.stderr, flush=True)


def _poll_and_publish(h):
    host, agents, ws, err = poll_host(h)
    entry = {"id": h["id"], "kind": h["kind"], "status": "ok", "error": None, "counts": {}, "asleep": ASLEEP_N.get(h["id"], 0)}
    shaped = []
    if err:
        entry.update(status="down", error=err)
    else:
        for a in agents:
            entry["counts"][a["agent_status"]] = entry["counts"].get(a["agent_status"], 0) + 1
            shaped.append(shape_agent(h, a, ws))
    with LOCK:
        entry["last_ok"] = (STATE["hosts"].get(h["id"]) or {}).get("last_ok")
        was = ((HOST_SNAP.get(h["id"]) or {}).get("entry") or {}).get("status")
        if was in ("ok", "down") and was != entry["status"]:   # seen to change: the first look at a host is not news
            log_event("host_down" if entry["status"] == "down" else "host_up", h["id"], label=h["id"], why=(entry["error"] or "")[:120] or None)
        HOST_SNAP[h["id"]] = {"entry": entry, "agents": shaped}
    _publish()


def poll_all(wait=2.0):
    """Start a poll of every enabled host and wait up to `wait` seconds for the quick ones. A host that is down (an ssh that
    takes 9 s to time out) keeps polling in the background and publishes when it answers: it can no longer make every refresh
    wait for it, or keep the live hosts' data minutes stale."""
    _tls.listed = _tls.sessions_listed = False   # a replaced discover_hosts (tests) never sets them, so they can never count as a real list
    hosts = discover_hosts()
    forget_gone_hosts(hosts, getattr(_tls, "listed", False), getattr(_tls, "sessions_listed", False))
    with LOCK:
        HOST_ORDER[:] = [h["id"] for h in hosts]
        for hid in [k for k in HOST_SNAP if k not in HOST_ORDER]:
            del HOST_SNAP[hid]
        for h in hosts:
            if not h["enabled"]:
                HOST_SNAP[h["id"]] = {"agents": [], "entry": {"id": h["id"], "kind": h["kind"], "status": "disabled", "counts": {}, "asleep": 0, "last_ok": None,
                                                             "error": f"saved machine is disabled (herdr machine enable {h['id']})"}}
    futs = []
    for h in hosts:
        if not h["enabled"]:
            continue
        f = _inflight.get(h["id"])
        if f is None or f.done():
            f = _pool.submit(_poll_and_publish, h); _inflight[h["id"]] = f
        futs.append(f)
    if not futs:
        _publish()
    concurrent.futures.wait(futs, timeout=wait)


def shape_agent(host, a, ws):
    cwd = a.get("cwd") or a.get("foreground_cwd") or ""
    sess = (a.get("agent_session") or {}).get("value") or ""
    return {
        "host": host["id"], "pane": a["pane_id"], "agent": a.get("agent") or "?",
        "status": a["agent_status"], "seq": a.get("state_change_seq"),
        "title": a.get("terminal_title_stripped") or a.get("terminal_title") or a["pane_id"],
        "cwd": cwd, "project": os.path.basename(cwd.rstrip("/")) or cwd or "?",
        "workspace": ws.get(a.get("workspace_id")) or a.get("workspace_id"),
        "wlabel": ws.get(a.get("workspace_id")) or "",   # the name Fred gave the herdr workspace: what the phone calls the session
        "session": sess[:8], "focused": bool(a.get("focused")), "ask": a.get("_ask"),
        "active_at": a.get("_active"),   # a working local Claude session: when it last wrote anything (None: not known)
    }


def poller():
    while True:
        try:
            poll_all()
        except Exception as e:  # never let the loop die; the age field tells the phone it is stale
            print(f"shepr: poll failed: {e!r}", file=sys.stderr)
        time.sleep(POLL_S)


_build_cache = [None, None]


def shell_build():
    """The app version a phone should be running: sw.js VERSION without the "shepr-" prefix. Re-read when sw.js changes."""
    try:
        mt = os.path.getmtime(os.path.join(HERE, "sw.js"))
        if _build_cache[0] != mt:
            m = re.search(r"VERSION = '([^']+)'", open(os.path.join(HERE, "sw.js")).read())
            _build_cache[:] = [mt, m.group(1).removeprefix("shepr-") if m else None]
    except OSError:
        return None
    return _build_cache[1]


BOOT_BUILD = shell_build()   # the app version this server process started with (the live shell_build() follows the files on disk)


def build_queue():
    now = time.time()
    with LOCK:
        agents = [dict(a) for a in SNAP["agents"]]
        hosts = [dict(h) for h in SNAP["hosts"]]
        ts, polls = SNAP["ts"], SNAP["polls"]
        risk_by = {}
        for r in (watch_risks() if WATCH_ON else []):   # newest first: the first one seen for a session is its latest
            ag = SID_AGENT.get(r.get("sid"))
            if ag and now - (r.get("t") or 0) <= 3600:
                risk_by.setdefault((ag["host"], ag["pane"]), {"level": r.get("level"), "what": r.get("what"), "t": r.get("t")})
        for a in agents:
            if a["status"] in ("blocked", "done"):
                s = STATE["seen"].get(f"{a['host']}|{a['pane']}|{a['seq']}|{a['status']}")
                if s:
                    a.update(since=s["t"], exact=s["exact"], basis=s["basis"], for_s=max(0, int(now - s["t"])))
            elif a["status"] == "working":
                w = WORKING.get(f"{a['host']}|{a['pane']}|{a['seq']}")
                if w and w["exact"]:
                    a["work_s"] = max(0, int(now - w["t"]))
                if a.get("active_at"):
                    a["quiet_s"] = max(0, int(now - max(a["active_at"], w["t"] if w and w["exact"] else a["active_at"])))
            if (a["host"], a["pane"]) in risk_by:
                a["risk"] = risk_by[(a["host"], a["pane"])]
            a.pop("active_at", None)
        fleet = {"working": sum(1 for a in agents if a["status"] == "working"), "blocked": sum(1 for a in agents if a["status"] == "blocked"),
                 "done": sum(1 for a in agents if a["status"] == "done"), "stopped_at": FLEET["stopped_at"] if FLEET["had_work"] else None}
    order = {"blocked": 0, "done": 1, "working": 2, "idle": 3}
    # Longest blocked first. Cross-host order needs wall-clock; within a host the seq breaks ties.
    agents.sort(key=lambda a: (order.get(a["status"], 4), a.get("since", 0), a.get("seq") or 0,
                               a["host"], a["project"]))
    return {"now": now, "age_s": int(now - ts) if ts else None, "polls": polls, "hosts": hosts,
            "agents": agents, "writes": not READONLY, "viewer": None, "poll_s": POLL_S, "build": shell_build(), "server_build": BOOT_BUILD,
            "fleet": fleet, "quiet_min": int(QUIET_S // 60), "watch": WATCH_ON}


# --------------------------------------------------------------------------------------------
# reading a dialog, and planning a send
# --------------------------------------------------------------------------------------------
def parse_menu(text):
    """Find the live menu at the bottom of a pane, e.g. Claude Code's AskUserQuestion or a permission dialog.
    Single choice: {"current": n, "options": [{"n","label","approves"}]}.
    Multi-select adds "multi": True, a "checked" flag on each toggle, and "action": the Submit / Next row. In that
    layout the cursor can sit on the unnumbered action row, which is why "idx" (position among everything the
    cursor can land on, top to bottom) is what key counts are made from, never the option number."""
    lines = text.split("\n")
    cur = None
    for i in range(len(lines) - 1, max(-1, len(lines) - 30), -1):
        m = OPT_RE.match(lines[i]) or ACT_RE.match(lines[i])
        if m and m.group(1):
            cur = i
            break
    if cur is None:
        return None
    hits = [(i, int(m.group(2)), m.group(3), bool(m.group(1)))
            for i, l in enumerate(lines) if i >= max(0, cur - 20) and (m := OPT_RE.match(l))]
    starts = [k for k, h in enumerate(hits) if h[1] == 1 and h[0] <= max(cur, 0)]
    if not starts:
        return None
    run = [hits[starts[-1]]]
    for h in hits[starts[-1] + 1:]:
        if h[1] == run[-1][1] + 1:
            run.append(h)
    # everything the cursor can land on, in screen order: the numbered rows plus a Submit / Next row between them
    rows = [("opt", h[0], h[1]) for h in run]
    act = None
    for i in range(run[0][0], min(len(lines), run[-1][0] + 4)):
        m = ACT_RE.match(lines[i])
        if m and not OPT_RE.match(lines[i]):
            act = (i, m.group(2), bool(m.group(1)))
            rows.append(("act", i, 0))
    rows.sort(key=lambda r: r[1])
    cur_idx = next((k for k, r in enumerate(rows) if r[1] == cur), None)
    if cur_idx is None:
        return None
    opts = []
    for h in run:
        label, checked = h[2], None
        c = CHK_RE.match(label)
        if c:
            checked, label = c.group(1) != " ", c.group(2) or "(blank)"
        opts.append({"n": h[1], "label": label, "approves": bool(APPROVE_RE.match(label)), "checked": checked,
                     "free": bool(FREE_RE.match(label)), "idx": next(k for k, r in enumerate(rows) if r[1] == h[0])})
    multi = sum(1 for o in opts if o["checked"] is not None) >= 2 and act is not None
    cur_n = next((h[1] for h in run if h[0] == cur), None)
    if cur_n is None and not multi:
        return None
    question = None
    for i in range(run[0][0] - 1, max(-1, run[0][0] - 8), -1):   # the nearest real line above the options is the question
        l = lines[i].strip().lstrip("\u2502|").strip()
        if not l or set(l) <= set("\u2500\u2501\u2550- \u2502"):
            continue
        if l[0] in "\u2190\u2192\u2610\u2612\u2714" or l.startswith(("Esc", "Enter to")):   # the tab bar of a multi-question form
            continue
        question = l[:200]
        break
    out = {"current": cur_n or 0, "cur_idx": cur_idx, "options": opts, "multi": multi, "question": question}
    # A form with several questions draws a tab bar above its question, and so does its review step. Esc on any step of it cancels the
    # WHOLE form: every answer given so far is thrown away and the agent is told you declined.
    out["form"] = any((_tb := lines[i].strip().lstrip("│|").strip()) and _tb[0] in "←→☐☒✔" for i in range(max(0, run[0][0] - 14), run[0][0]))
    if multi:
        out["action"] = {"label": act[1], "idx": next(k for k, r in enumerate(rows) if r[0] == "act"), "focused": act[2]}
    return out


def pane_view(host, pane, n):
    """What an open session shows: the last n lines, the question on screen when it is blocked, and the agent's state.
    One read of the pane, not two: the menu needs the last 80 lines, the phone asked for n, and on another machine every
    read is an ssh."""
    a = get_agent(host, pane)
    blocked = a["agent_status"] == "blocked"
    got = read_tail(host, pane, max(n, 80) if blocked else n)
    text, queued = clean_tail("\n".join(got.split("\n")[-n:]))
    menu = parse_menu(got) if blocked else None
    if menu:
        menu["risk"] = dialog_risk(got, menu)
    return {"text": text, "queued": queued, "lines": n, "menu": menu, "agent": shape_agent(host, a, {}), "at": time.time()}


def human_keys(keys):
    sym = {"up": "↑", "down": "↓", "left": "←", "right": "→", "enter": "⏎", "esc": "Esc",
           "tab": "Tab", "shift+tab": "⇧Tab", "space": "Space", "backspace": "⌫"}
    return " ".join(sym.get(k, k) for k in keys)


def _menu_fp(menu):
    """Question, cursor and options, so keys planned for one question cannot land on the next."""
    if not menu:
        return None
    return [menu.get("question"), menu["cur_idx"], [[o["n"], o["label"], o["checked"]] for o in menu["options"]]]


def need_host(hid):
    with LOCK:
        known = {h["id"] for h in SNAP["hosts"] if h["status"] == "ok"}
    for h in discover_hosts():
        if h["id"] == hid and h["enabled"] and hid in known:
            return h
    raise HerdrError("bad_host", f"unknown or unreachable host {hid!r}")


CLAUDE_MODELS = ("fable", "opus", "sonnet", "haiku")
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _typed_text(text):
    """Your own words for a "Type something" row: one line, no control characters (a newline would submit early, a tab would move)."""
    if text is None or (isinstance(text, str) and not text.strip()):
        raise HerdrError("needs_text", "that option is for your own words: type them first")
    if not isinstance(text, str) or CTRL_RE.search(text) or "\n" in text or "\r" in text or "\t" in text or len(text) > 1000:
        raise HerdrError("bad_text", "your own words must be one line of up to 1000 characters")
    return text.strip()


def _nav(frm, to):
    return ["down"] * (to - frm) if to > frm else ["up"] * (frm - to)


def build_plan(body):
    if READONLY:
        raise HerdrError("read_only", "this server was started read-only (SHEPR_READONLY=1)")
    if body.get("kind") in HOST_KINDS:
        return plan_host(body)
    if body.get("kind") == "wake":
        return plan_wake(body)
    if body.get("kind") == "start":
        return plan_start(body)
    if body.get("kind") == "new_project":
        return plan_new_project(body)
    if body.get("kind") == "open_folder":
        return plan_open_folder(body)
    kind = body.get("kind")
    if kind not in ("text", "keys", "choose", "multi", "model", "effort", "sendnow"):
        raise HerdrError("bad_kind", f"this host's Shepr does not know the action {str(kind)[:30]!r}. It is older than the app on your "
                                     "phone: restart Shepr on the host so the two match.")
    host = need_host(str(body.get("host", "")))
    pane = str(body.get("pane", ""))
    if not PANE_RE.match(pane):
        raise HerdrError("bad_pane", "not a pane id")
    a = get_agent(host, pane)
    status = a["agent_status"]
    steps, lines, approves = [], [], False
    risky = None        # a permission dialog whose command the watch would flag
    bare_text = False   # words typed at a blocked pane that was not a numbered menu
    keys_menu, menu_fp = False, None
    queued_line = False

    if kind == "text":
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 4000 or "\x00" in text:
            raise HerdrError("bad_text", "text must be 1 to 4000 characters")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if status == "blocked" and body.get("skip") is True:
            # Words cannot be typed into a question (a numbered pick reads them as keystrokes), so the person chose to close the
            # question first. Esc closes it WITHOUT answering it: nothing is approved (a permission request is refused). The words go
            # in only once the agent has left "blocked", as an ordinary prompt, so they may be several lines. Never sent to an agent
            # that is not blocked: Esc would interrupt its work.
            steps = [{"op": "keys", "keys": ["esc"]}, {"op": "await_clear"}, {"op": "prompt", "text": text}]
            in_form = (parse_menu(read_tail(host, pane, 80)) or {}).get("form")
            lines = [("Closes the question", "Esc. Nothing is answered or approved. If it is a permission request, this refuses it."),
                     ("Careful", "This question is a step of a form with several questions. Esc cancels the WHOLE form: every answer you already gave to it is thrown away and the agent is told you declined."
                                 if in_form else
                                 "If this question is one of several in a form, Esc cancels the whole form and throws away the answers you already gave to it."),
                     ("Then sends as a prompt", text)]
        elif status == "blocked":
            if "\n" in text:
                raise HerdrError("blocked_text", "a blocked agent is at a dialog, which takes one line. Your message has more than one.")
            # A numbered menu reads typed characters as keystrokes (a digit picks that option), so words typed at one must never be
            # sent as they are. Fred, 2026-10-10: a sentence sent from the message box picked option 1 of a question.
            menu = parse_menu(read_tail(host, pane, 80))
            if menu:
                free = next((o for o in menu["options"] if o["free"]), None)
                if not free:
                    raise HerdrError("menu_text", "this question takes a numbered pick, so typed words would be read as keystrokes. Tap an option instead.")
                if menu.get("multi"):
                    ticked = [o["n"] for o in menu["options"] if o["checked"] and not o["free"] and o["n"] <= 9]
                    return build_plan(dict(body, kind="multi", picks=ticked, advance=True, text=text, from_box=True))
                return build_plan(dict(body, kind="choose", option=free["n"], text=text, from_box=True))
            steps = [{"op": "text", "text": text}, {"op": "keys", "keys": ["enter"]}]
            lines = [("Types into the dialog", text), ("Then presses", "⏎")]
            bare_text = True
        else:
            steps = [{"op": "prompt", "text": text}]
            lines = [("Sends as a prompt", text)]
            if status == "working":
                lines.append(("Note", "the agent is working right now; Claude queues this until it is free"))
    elif kind == "sendnow":
        # Claude Code shows "ctrl+enter to send now" under a message that is queued behind running work. This is the one ctrl chord the phone
        # may send, and only while that line is on screen, so it can never land as a stray keystroke.
        if a.get("agent") != "claude":
            raise HerdrError("not_claude", "send now only works for Claude Code sessions")
        if not QUEUED_RE.search(read_tail(host, pane, 60)):
            raise HerdrError("nothing_queued", "no message is waiting in the queue of that session right now")
        steps = [{"op": "keys", "keys": ["ctrl+enter"]}]
        lines = [("Presses", "Ctrl+Enter"), ("Does", "Claude Code sends your queued message now instead of waiting for the current work to end")]
        queued_line = True
    elif kind in ("model", "effort"):
        # Claude Code takes /model <name> and /effort <level> as plain arguments (verified on 2.1.293): no menu, no model call.
        if a.get("agent") != "claude":
            raise HerdrError("not_claude", f"changing the {kind} from here only works for Claude Code sessions")
        if status not in ("idle", "done"):
            raise HerdrError("not_idle", f"change the {kind} while the agent is waiting for its next prompt, not while it is {status}")
        value = body.get("value")
        if value not in (CLAUDE_MODELS if kind == "model" else CLAUDE_EFFORTS):
            raise HerdrError("bad_value", f"{str(value)[:20]!r} is not a {kind} Shepr offers")
        steps = [{"op": "prompt", "text": f"/{kind} {value}"}]
        lines = [("Sends", f"/{kind} {value}"), ("Note", f"changes this session's {kind}; Claude Code also makes it the default for new sessions")]
    elif kind == "keys":
        keys = body.get("keys")
        if not isinstance(keys, list) or not 1 <= len(keys) <= 8:
            raise HerdrError("bad_keys", "1 to 8 keys")
        for k in keys:
            if not (isinstance(k, str) and (k in KEY_NAMED or KEY_CHAR.match(k))):   # str first: a list or a dict cannot even be looked up in a set
                raise HerdrError("bad_keys", f"key {k!r} is not allowed from the phone")
        if status == "working" and keys != ["esc"]:
            raise HerdrError("bad_keys", "the agent is working: the only key allowed is Esc (interrupt)")
        steps = [{"op": "keys", "keys": keys}]
        lines = [("Presses", human_keys(keys))]
        if status == "blocked":
            # The same blocked dialog can change question without moving state_change_seq. Remember the screen.
            keys_menu, menu_fp = True, _menu_fp(parse_menu(read_tail(host, pane, 80)))
    elif kind == "choose":
        if status != "blocked":
            raise HerdrError("not_blocked", "choosing an option only applies to a blocked agent")
        try:
            n = int(body.get("option"))
        except (TypeError, ValueError):
            raise HerdrError("bad_option", "option must be a number")
        menu = parse_menu(read_tail(host, pane, 80))
        opt = menu and next((o for o in menu["options"] if o["n"] == n), None)
        if not opt:
            raise HerdrError("no_menu", f"no option {n} on screen right now")
        if menu.get("multi"):
            raise HerdrError("multi_select", "this question lets you tick several options: tick them, then use Submit")
        text_at = None
        if opt["free"]:
            # "Type something" is a text field that is live while the cursor is on it. Enter with nothing typed DECLINES the
            # question, so this option never goes through the plain pick: arrow to it, type, then Enter.
            text = _typed_text(body.get("text"))
            nav = _nav(menu["cur_idx"], opt["idx"])
            steps = ([{"op": "keys", "keys": nav, "wait": 0.35}] if nav else []) + [{"op": "text", "text": text}, {"op": "keys", "keys": ["enter"]}]
            text_at, approves = opt["idx"], False
            lines = [("Picks", f"{n}. {opt['label']}"), ("Types", text), ("By pressing", (human_keys(nav) + " then " if nav else "") + "your words, then ⏎")]
        else:
            if body.get("text") is not None:
                raise HerdrError("bad_text", "that option does not take your own words")
            keys = _nav(menu["cur_idx"], opt["idx"]) + ["enter"]
            steps = [{"op": "keys", "keys": keys}]
            approves = opt["approves"]
            lines = [("Picks", f"{n}. {opt['label']}"), ("By pressing", human_keys(keys))]
            risky = dialog_risk(read_tail(host, pane, 80), menu) if approves else None
            if risky:   # what it is about to be allowed to do, in plain words, before the tap
                lines.append(("Careful", f"what this allows looks like: {risky['what']}"))
        if menu.get("question"):   # which question this answers: a form asks several, and words can land on a different one than you meant
            lines.insert(0, ("Answers", menu["question"]))
    elif kind == "multi":
        if status != "blocked":
            raise HerdrError("not_blocked", "ticking options only applies to a blocked agent")
        menu = parse_menu(read_tail(host, pane, 80))
        if not menu or not menu.get("multi"):
            raise HerdrError("no_menu", "there is no tick-several question on screen right now")
        picks = body.get("picks")
        if not isinstance(picks, list) or not all(isinstance(x, int) and not isinstance(x, bool) for x in picks):
            raise HerdrError("bad_option", "picks must be a list of option numbers")
        tick = {o["n"]: o for o in menu["options"] if o["checked"] is not None and not o["free"] and o["n"] <= 9}
        if any(x not in tick for x in picks):
            raise HerdrError("bad_option", "one of those is not an option you can tick here")
        want = set(picks)
        flip = [n for n in sorted(tick) if (n in want) != tick[n]["checked"]]
        advance = bool(body.get("advance", True))
        free_o = next((o for o in menu["options"] if o["free"]), None)
        text = body.get("text")
        if text is not None:
            if not free_o:
                raise HerdrError("bad_text", "this question has no place to type your own words")
            text = _typed_text(text)
        if not flip and not advance and not text:
            raise HerdrError("nothing", "that is already how it looks on screen")
        # A digit toggles that option in place and leaves the cursor where it was, so the ticks need no navigation.
        # Each digit is its own keypress with a pause after it: sent together, Claude reads "13" as pasted text and ticks nothing.
        keys, steps, cur = [str(n) for n in flip], [], menu["cur_idx"]
        act = menu["action"]
        if text and flip and cur == free_o["idx"]:   # digits typed while the cursor is on the text row would land in the text
            j = free_o["idx"] - 1 if free_o["idx"] > 0 else 1
            steps.append({"op": "keys", "keys": _nav(cur, j), "wait": 0.3}); keys = _nav(cur, j) + keys; cur = j
        steps += [{"op": "keys", "keys": [k], "wait": 0.3} for k in [str(n) for n in flip]]
        if text:
            go = _nav(cur, free_o["idx"])
            if go:
                steps.append({"op": "keys", "keys": go, "wait": 0.35}); keys += go
            steps.append({"op": "text", "text": text}); keys += ["\u2328"]   # the keyboard glyph stands for the typed words
            cur = free_o["idx"]
        if advance:
            nav = _nav(cur, act["idx"]) + ["enter"]
            keys += nav
            steps.append({"op": "keys", "keys": nav})
        # This send approves when it ticks an approving box, or when it submits with one left ticked: an approval that is
        # already ticked is not in `flip`, so judging the flips alone would send "Yes" through Submit without the warning.
        # Unticking an approval never warns. Ticks that cannot be toggled here (a checked "Type something", a row above 9) count.
        final_on = set(want) | {o["n"] for o in menu["options"] if o["checked"] and o["n"] not in tick}
        approving = {o["n"] for o in menu["options"] if o["approves"]}
        approves = bool({n for n in flip if n in want} & approving) or (advance and bool(final_on & approving))
        on = [f"{n}. {tick[n]['label']}" for n in flip if n in want]
        off = [f"{n}. {tick[n]['label']}" for n in flip if n not in want]
        lines = ([("Ticks", ", ".join(on))] if on else []) + ([("Unticks", ", ".join(off))] if off else []) + ([("Types", text)] if text else []) + \
                [("Leaves ticked", ", ".join(f"{o['n']}. {o['label']}" + ("" if o["n"] in tick else " (cannot be changed here)")
                                             for o in menu["options"] if o["checked"] and o["n"] not in flip) or "nothing else")] + \
                ([("Then", f"{act['label']} (⏎)")] if advance else [("Then", "stays on the question")]) + \
                [("By pressing", human_keys(keys))]
        if menu.get("question"):
            lines.insert(0, ("Answers", menu["question"]))
    else:
        raise HerdrError("bad_kind", "kind must be text, keys, choose, multi, model or effort")

    plan = {
        "nonce": secrets.token_urlsafe(18), "host": host["id"], "pane": pane, "kind": kind, "steps": steps, "made": time.time(),
        "snap": {"status": status, "seq": a.get("state_change_seq"),
                 "session": (a.get("agent_session") or {}).get("value")},
        "expires": time.time() + NONCE_TTL_S,
    }
    if kind in ("choose", "multi"):
        # The next question of the same dialog keeps state_change_seq and can have the same options ("1. Yes / 2. No").
        plan["snap"]["question"] = menu.get("question")
    if kind == "choose":
        plan["snap"]["pick"] = [n, opt["label"], menu["current"]]
    if kind == "choose" and text_at is not None:
        plan["snap"]["text_at"] = text_at
    if kind == "multi" and text:
        plan["snap"]["text_at"] = free_o["idx"]
    if kind == "multi":
        plan["snap"]["multi"] = [[o["n"], o["label"], o["checked"]] for o in menu["options"]] + [menu["cur_idx"], menu["action"]["idx"]]
    if bare_text:
        plan["snap"]["bare"] = True
    if keys_menu:
        plan["snap"]["menu_at"] = menu_fp
    if queued_line:
        plan["snap"]["queued"] = True
    with LOCK:
        for k in [k for k, p in PLANS.items() if p["expires"] < time.time()]:
            del PLANS[k]
        PLANS[plan["nonce"]] = plan
    return {
        "nonce": plan["nonce"], "kind": kind, "expires_s": NONCE_TTL_S, "approves": approves, "from_box": bool(body.get("from_box")),
        "risk": risky if kind == "choose" else None,
        "target": {"host": host["id"], "pane": pane,
                   "title": os.path.basename((a.get("cwd") or "").rstrip("/")) or pane, "project": "", "status": status},
        "lines": [{"k": k, "v": v} for k, v in lines],
    }


AWAIT_CLEAR_S = float(os.environ.get("SHEPR_AWAIT_CLEAR_S", "20"))   # how long to wait for a question to close after Esc. Claude Code takes a few seconds to take the Esc in before its state leaves "blocked": 5 was too short (2026-11-04)


def run_step(host, pane, step):
    m = host["machine"]
    if step["op"] == "await_clear":
        # After Esc the question closes and the agent leaves "blocked". Words go in only then, and only if it did.
        end = time.time() + AWAIT_CLEAR_S
        while True:
            if get_agent(host, pane)["agent_status"] != "blocked":
                return None
            if time.time() >= end:
                raise HerdrError("still_blocked", f"Esc was pressed, but the session still showed its question after {AWAIT_CLEAR_S:g} seconds, so your words were not sent. "
                                                  "The question may have closed since: look at the session, then send again")
            time.sleep(0.25)
    if step["op"] == "keys":
        return run_herdr(m, ["pane", "send-keys", pane] + step["keys"], timeout=10, want_json=False)
    if step["op"] == "text":
        # No "--" here: herdr's send-text has no option parsing, so it would type the two dashes into the pane (it did, for every
        # answer typed into a dialog, until v26). Leading dashes in the text are fine without it.
        return run_herdr(m, ["pane", "send-text", pane, step["text"]], timeout=10, want_json=False)
    if step["op"] == "run":
        return run_herdr(m, ["pane", "run", pane, step["cmd"]], timeout=10, want_json=False)
    if step["op"] == "prompt":
        # No "--" here: `agent prompt` takes the text as a hyphen-tolerant positional and treats "--" as text.
        return run_herdr(m, ["agent", "prompt", pane, step["text"]], timeout=15, want_json=False)
    raise HerdrError("bad_step", step["op"])


def _did(step):
    """What a finished step means to a person reading an error, instead of the name the code gives it."""
    op = step["op"]
    if op == "keys":
        return "pressed " + human_keys(step["keys"])
    if op == "text":
        return "typed your words"
    if op == "prompt":
        return "sent your words"
    if op == "await_clear":
        return "waited for the question to close"
    return op


PANE_LOCKS = {}
LAST_SENT = {}   # (host, pane) -> time of the last answer sent to that terminal from here


def pane_lock(host_id, pane):
    with LOCK:
        return PANE_LOCKS.setdefault((host_id, pane), threading.Lock())


def commit_plan(nonce):
    with LOCK:
        plan = PLANS.pop(str(nonce), None)  # one use, even if the commit then fails
    if not plan or plan["expires"] < time.time():
        raise HerdrError("expired", "that preview expired or was already used: look again and re-send")
    if plan["kind"] in HOST_KINDS:
        return commit_host(plan)
    if plan["kind"] == "wake":
        return commit_wake(plan)
    if plan["kind"] == "start":
        return commit_start(plan)
    if plan["kind"] in ("new_project", "open_folder"):   # the same two steps: make the herdr project, type the command
        return commit_new_project(plan)
    host = need_host(plan["host"])
    with pane_lock(plan["host"], plan["pane"]):   # verify and dispatch as one step per pane
        return _commit_pane_plan(host, plan)


def _commit_pane_plan(host, plan):
    a = get_agent(host, plan["pane"])
    snap = plan["snap"]
    key = (plan["host"], plan["pane"])
    if (a["agent_status"], a.get("state_change_seq"), (a.get("agent_session") or {}).get("value")) != \
            (snap["status"], snap["seq"], snap["session"]):
        raise HerdrError("moved_on", "the agent changed state since you looked, so nothing was sent. Read it again.")
    # A preview made BEFORE the last answer to this terminal is dead: its screen is gone. A preview made AFTER it is fine,
    # even though herdr's state counter has not moved (a multi-step dialog, such as ticks then Review then Submit, is ONE
    # blocked state, so the counter cannot be what tells the steps apart).
    if snap["status"] == "blocked" and LAST_SENT.get(key, 0) >= plan.get("made", 0):
        raise HerdrError("moved_on", "another answer was sent to this terminal after you looked at it, so nothing was sent. Look again.")
    if plan["kind"] == "multi":
        menu = parse_menu(read_tail(host, plan["pane"], 80))
        now = menu and menu.get("multi") and [[o["n"], o["label"], o["checked"]] for o in menu["options"]] + [menu["cur_idx"], menu["action"]["idx"]]
        if now != snap["multi"] or ("question" in snap and menu.get("question") != snap["question"]):
            raise HerdrError("moved_on", "the question on screen is not the one you ticked, so nothing was sent.")
    if plan["kind"] == "choose":
        menu = parse_menu(read_tail(host, plan["pane"], 80))
        n, label, cur = snap["pick"]
        o = menu and next((o for o in menu["options"] if o["n"] == n), None)
        if not o or o["label"] != label or menu["current"] != cur or ("question" in snap and menu.get("question") != snap["question"]):
            raise HerdrError("moved_on", "the menu on screen is not the one you chose from, so nothing was sent.")
    # Words typed at a blocked pane that was not a numbered menu. A later step of that same dialog can put a
    # numbered question up without moving state_change_seq, and a digit would pick an option. Only a plan prepare
    # marked bare is checked, so a hand-built plan is left as it was.
    if snap.get("bare") and parse_menu(read_tail(host, plan["pane"], 80)):
        raise HerdrError("moved_on", "a numbered question is on screen now, so your words were not typed")
    # Keys planned while blocked. Absent on a hand-built plan, and on keys sent when the agent was not blocked.
    if "menu_at" in snap and _menu_fp(parse_menu(read_tail(host, plan["pane"], 80))) != snap["menu_at"]:
        raise HerdrError("moved_on", "the question on screen is not the one these keys were for, so nothing was sent")
    # Send now is allowed only while "ctrl+enter to send now" is on screen. That line can go without the state counter moving.
    if snap.get("queued") and not QUEUED_RE.search(read_tail(host, plan["pane"], 60)):
        raise HerdrError("moved_on", "the queued message is no longer on screen, so Ctrl+Enter was not sent")
    done = []
    steps = plan["steps"]
    for i, step in enumerate(steps):
        if plan["kind"] == "multi" and i == len(steps) - 1 and i > 0:
            # The ticks went in one keypress at a time: check the question is still the same one before the
            # final move-and-Enter, so a question that changed meanwhile is never submitted.
            a2 = get_agent(host, plan["pane"])
            m2 = parse_menu(read_tail(host, plan["pane"], 80))
            ta = snap.get("text_at")
            keep = lambda labels: [None if k == ta else l for k, l in enumerate(labels)]   # the row you typed in no longer says "Type something"
            same = m2 and m2.get("multi") and m2["cur_idx"] == (ta if ta is not None else snap["multi"][-2]) and \
                keep([o["label"] for o in m2["options"]]) == keep([x[1] for x in snap["multi"][:-2]]) and \
                ("question" not in snap or m2.get("question") == snap["question"])
            if not same or (a2["agent_status"], (a2.get("agent_session") or {}).get("value")) != (snap["status"], snap["session"]):
                audit(plan, False, "moved_on: question changed between the ticks and the submit")
                raise HerdrError("moved_on", "the question changed while ticking, so Submit was not sent. "
                                             f"Ticks already sent: {', '.join(done) or 'none'}. Read the screen before trying again.")
        if step["op"] == "text" and snap.get("text_at") is not None:
            # Never type your words anywhere but the row meant for them: look at the screen and check where the cursor is.
            m3 = parse_menu(read_tail(host, plan["pane"], 80))
            if not m3 or m3["cur_idx"] != snap["text_at"]:
                audit(plan, False, "moved_on: the cursor was not on the typing row")
                raise HerdrError("moved_on", "the cursor did not land on the row for your own words, so nothing was typed. "
                                             f"Sent before stopping: {', '.join(done) or 'nothing'}. Look at the screen and try again.")
        try:
            run_step(host, plan["pane"], step)
            done.append(_did(step))
            if step["op"] == "await_clear":
                # The question can close because a different session now owns the pane. Do not prompt that one.
                # Still-blocked is already refused inside await_clear; the same session, now idle, is sent.
                a_clear = get_agent(host, plan["pane"])
                if (a_clear.get("agent_session") or {}).get("value") != snap["session"]:
                    raise HerdrError("moved_on", "the session changed after the question closed, so your words were not sent")
            if step.get("wait"):
                time.sleep(step["wait"])
        except HerdrError as e:
            audit(plan, False, f"{e.code}: {e.message}")
            raise HerdrError(e.code, f"{e.message}. Sent before the failure: {', '.join(done) or 'nothing'}. "
                                     "A failed write may still have landed: read the pane before trying again.")
    if snap["status"] == "blocked":
        LAST_SENT[key] = time.time()
        with LOCK:   # any other preview made against this same dialog is dead now
            for k in [k for k, p in PLANS.items() if (p.get("host"), p.get("pane")) == key]:
                del PLANS[k]
    audit(plan, True, "")
    return {"ok": True, "sent": done}


def audit(plan, ok, err):
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        rec = {"t": int(time.time()), "host": plan.get("host"), "pane": plan.get("pane"), "kind": plan["kind"],
               "ok": ok, "err": err, "steps": [dict(s, text=s["text"][:200]) if "text" in s else s for s in plan.get("steps", [])]}
        fd = os.open(state_path("sends.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(rec) + "\n")
        print("shepr: send " + json.dumps(rec, ensure_ascii=False), file=sys.stderr, flush=True)
    except OSError:
        pass


# --------------------------------------------------------------------------------------------
# device keys. The phone holds an ECDSA P-256 key it can never export (WebCrypto, IndexedDB);
# this server holds the matching PUBLIC keys, like an authorized_keys file. Verification is plain
# Python because these hosts have no `cryptography` module and the app must stay copy-and-run.
# --------------------------------------------------------------------------------------------
_P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
_N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551
_B = 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b
_G = (0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296,
      0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5)


def _add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    (x1, y1), (x2, y2) = p, q
    if x1 == x2:
        if (y1 + y2) % _P == 0:
            return None
        lam = (3 * x1 * x1 - 3) * pow(2 * y1, -1, _P) % _P
    else:
        lam = (y2 - y1) * pow(x2 - x1, -1, _P) % _P
    x3 = (lam * lam - x1 - x2) % _P
    return x3, (lam * (x1 - x3) - y1) % _P


def _mul(k, p):
    r = None
    while k:
        if k & 1:
            r = _add(r, p)
        p = _add(p, p)
        k >>= 1
    return r


def on_curve(x, y):
    return 0 <= x < _P and 0 <= y < _P and (y * y - (x * x * x - 3 * x + _B)) % _P == 0


def ecdsa_verify(x, y, msg, sig):
    """sig is WebCrypto's raw r||s (64 bytes), not DER."""
    if len(sig) != 64 or not on_curve(x, y):
        return False
    r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
    if not (1 <= r < _N and 1 <= s < _N):
        return False
    e = int.from_bytes(hashlib.sha256(msg).digest(), "big")
    w = pow(s, -1, _N)
    pt = _add(_mul(e * w % _N, _G), _mul(r * w % _N, (x, y)))
    return pt is not None and pt[0] % _N == r


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64u_dec(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def fingerprint(x, y):
    """Shown like an OpenSSH fingerprint so it reads the same on the phone and in `shepr devices`."""
    raw = b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


DEVICES = {}
_dev_stamp = [None]
NONCES = {}
PAIR_FAILS = []
AUTH_ERR = {"unpaired": 401, "unknown_device": 401, "bad_signature": 401, "stale": 401, "replay": 401,
            "bad_code": 403, "slow_down": 429}
HTTP_STATUS = {"moved_on": 409, "expired": 410, "read_only": 403, "bad_host": 404,
               "agent_not_found": 404, "pane_not_found": 404}   # a pane herdr no longer has is gone, not a bad request
UPSTREAM_ERR = {"timeout", "error", "bad_json", "no_herdr", "agent_blocked", "agent_not_ready"}


def http_status(code):
    """HerdrError code to an HTTP status. Unknown codes stay 400."""
    if code in AUTH_ERR:
        return AUTH_ERR[code]
    if code in HTTP_STATUS:
        return HTTP_STATUS[code]
    if code in UPSTREAM_ERR:
        return 502
    return 400


def _dev_file_stamp():
    """What says devices.json changed. The mtime alone misses a `shepr revoke` that lands in the same clock tick as the
    server's own save; every save is an os.replace, which makes a new inode, so the inode and size are compared too."""
    try:
        st = os.stat(state_path("devices.json"))
        return (st.st_mtime_ns, st.st_ino, st.st_size)
    except OSError:
        return None


def devices():
    """The authorized devices, reloaded whenever the file changes (so `shepr revoke` takes effect now)."""
    stamp = _dev_file_stamp()
    with LOCK:
        if stamp != _dev_stamp[0]:
            try:
                with open(state_path("devices.json")) as f:
                    DEVICES.clear(); DEVICES.update(json.load(f))
            except (OSError, ValueError):
                DEVICES.clear()
            _dev_stamp[0] = stamp
        return DEVICES


@contextlib.contextmanager
def state_lock():
    """One lock for the device registry and the pairing record, shared by this server and the `shepr` CLI
    (separate processes), so a revoke or a redeem is never lost to a stale in-memory copy."""
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    with open(state_path(".lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _mutate_devices_locked(fn):
    _dev_stamp[0] = None          # force a reload from disk: never write back what another process already changed
    r = fn(devices())
    save_devices()
    return r


def mutate_devices(fn):
    with LOCK, state_lock():
        return _mutate_devices_locked(fn)


def save_devices():
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    tmp = state_path("devices.json.tmp")
    with open(tmp, "w") as f:
        json.dump(DEVICES, f, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, state_path("devices.json"))
    _dev_stamp[0] = _dev_file_stamp()


def dev_xy(d):
    return int.from_bytes(b64u_dec(d["x"]), "big"), int.from_bytes(b64u_dec(d["y"]), "big")


def dev_public(did, d):
    x, y = dev_xy(d)
    return {"id": did, "name": d["name"], "fp": fingerprint(x, y), "added": d.get("added"),
            "last_seen": d.get("last_seen")}


def precheck_request(headers):
    """The part of authentication that needs no body: a known device, a fresh timestamp, well-formed fields.
    It runs BEFORE the body is read, so a caller who is not a paired phone is turned away without holding a
    connection open while it sends (or pretends to send) a body. The signature still covers the body, so the
    full check stays in verify_request."""
    dev, ts, nonce, sig = (headers.get(h) for h in ("X-Shepr-Device", "X-Shepr-Ts", "X-Shepr-Nonce", "X-Shepr-Sig"))
    if not (dev and ts and nonce and sig):
        raise HerdrError("unpaired", "this device has not been paired with this host")
    d = devices().get(dev)
    if not d:
        raise HerdrError("unknown_device", "this device is not authorized here (revoked, or never paired)")
    try:
        t = int(ts) / 1000.0
        sigb = b64u_dec(sig)
    except (ValueError, TypeError, OverflowError):   # OverflowError: a timestamp too large for a float
        raise HerdrError("bad_signature", "malformed signature")
    if abs(time.time() - t) > 90:
        raise HerdrError("stale", "request is too old or the phone clock is off")
    if not 8 <= len(nonce) <= 64:
        raise HerdrError("bad_signature", "bad nonce")
    return dev, d, ts, nonce, sigb


def verify_request(headers, method, target, body):
    """Every /api call except hello and pair carries a signature from an authorized device."""
    dev, d, ts, nonce, sigb = precheck_request(headers)
    msg = f"{ts}\n{nonce}\n{method}\n{target}\n{hashlib.sha256(body).hexdigest()}".encode()
    x, y = dev_xy(d)
    if not ecdsa_verify(x, y, msg, sigb):
        raise HerdrError("bad_signature", "signature does not match this device's key")
    now = time.time()
    with LOCK:
        for k in [k for k, e in NONCES.items() if e < now]:
            del NONCES[k]
        if nonce in NONCES:
            raise HerdrError("replay", "that request was already seen")
        NONCES[nonce] = now + 300
    if now - (d.get("last_seen") or 0) > 60:
        def touch(devs):
            if dev not in devs:   # revoked while this request was being verified
                raise HerdrError("unknown_device", "this device is not authorized here (revoked, or never paired)")
            devs[dev]["last_seen"] = int(now)
        try:
            mutate_devices(touch)
        except OSError:
            pass
    return dev


# ---- pairing: `shepr pair` on the host shows a QR, the app scans it, the key is registered
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"   # Crockford: no I L O U, so no misread codes


def norm_code(c):
    return re.sub(r"[^0-9A-Z]", "", str(c).upper().replace("O", "0").replace("I", "1").replace("L", "1"))


def pairing_path():
    return state_path("pairing.json")


def read_pairing():
    try:
        with open(pairing_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_pairing(d):
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    tmp = pairing_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, pairing_path())


def do_pair(body, ua):
    now = time.time()
    with LOCK:
        PAIR_FAILS[:] = [t for t in PAIR_FAILS if now - t < 60]
        if len(PAIR_FAILS) >= 5:
            raise HerdrError("slow_down", "too many wrong codes: wait a minute and run `shepr pair` again")
    code = norm_code(body.get("code", ""))
    jwk = body.get("jwk") or {}
    try:
        if not isinstance(jwk, dict) or jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
            raise ValueError
        x, y = int.from_bytes(b64u_dec(jwk["x"]), "big"), int.from_bytes(b64u_dec(jwk["y"]), "big")
        if not on_curve(x, y):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise HerdrError("bad_key", "that is not a P-256 public key")
    name = re.sub(r"[^\w .@'-]", "", str(body.get("name") or "iPhone"))[:40].strip() or "iPhone"
    did = hashlib.sha256(b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")).hexdigest()[:16]
    # Read, check, consume and register under ONE lock: two phones racing with the same code cannot both win.
    with LOCK, state_lock():
        pend = read_pairing()
        good = bool(pend) and not pend.get("used_by") and pend.get("expires", 0) > now and \
            hmac.compare_digest(pend.get("hash", ""), hashlib.sha256(code.encode()).hexdigest())
        if not good:
            PAIR_FAILS.append(now)
            raise HerdrError("bad_code", "that code is wrong, used, or expired: run `shepr pair` for a fresh QR")
        pend["used_by"] = did
        write_pairing(pend)
        _mutate_devices_locked(lambda devs: devs.__setitem__(did, {
            "name": name, "x": b64u(x.to_bytes(32, "big")), "y": b64u(y.to_bytes(32, "big")),
            "added": int(now), "last_seen": int(now), "ua": (ua or "")[:160]}))
    print(f"shepr: paired {name!r} {fingerprint(x, y)}", file=sys.stderr, flush=True)
    return {"device": did, "fp": fingerprint(x, y), "host": LOCAL, "t": now}


def server_info():
    try:
        with open(state_path("server.json")) as f:
            d = json.load(f)
        os.kill(d["pid"], 0)
        return d
    except (OSError, ValueError, KeyError, ProcessLookupError):
        return None


def public_url(port):
    if os.environ.get("SHEPR_URL"):
        return os.environ["SHEPR_URL"].rstrip("/")
    # The tailnet name, never the loopback aliases that are also in OWNER["names"] (a set sorts 127.0.0.1 first).
    return f"https://{OWNER['dns']}:{port}" if OWNER.get("dns") else f"http://127.0.0.1:{port}"


# --------------------------------------------------------------------------------------------
# hosts: saved herdr machines, plus a look at the rest of YOUR tailnet
# --------------------------------------------------------------------------------------------
HOST_KINDS = {"host_enable", "host_disable", "host_add", "device_revoke"}
SCAN = {"at": 0.0, "running": False, "peers": []}
LABEL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")
# Automatic adding (opt in): a background scan of YOUR tailnet nodes that adds the ones running a herdr server. Never on a read-only server.
AUTOADD = os.environ.get("SHEPR_AUTOADD") == "1" and not READONLY
SCAN_EVERY_S = max(60.0, float(os.environ.get("SHEPR_SCAN_S", "300")))
AUTOADD_MAX = 5                                              # machines one scan may add, so a surprise cannot add a whole tailnet
AUTOADD_RETRY_S = 3600                                       # a failed add is not tried again for this long
RECHECK_S = {"noherdr": 3600, "nokey": 3600, "hostkey": 3600}  # a background scan does not log in again to a node that answered this
SCAN_MEMO = {}                                               # peer name -> (time, state, detail, running)
AUTO_FAILED = {}                                             # peer name -> time of its last failed automatic add


def machine_profiles():
    try:
        return run_herdr(None, ["machine", "list", "--json"], timeout=6)
    except HerdrError:
        return []


def hub_pubkey():
    for n in ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub"):
        try:
            with open(os.path.expanduser("~/.ssh/" + n)) as f:
                return f.read().strip()
        except OSError:
            continue
    return None


# Runs on the remote machine under sh, so the user's own shell (fish, zsh, bash) never has to parse it. It finds herdr on the plain PATH or in
# the login shell (macOS and most installers put herdr's directory in a login profile), prints that path, then asks herdr which sessions are
# running. `session list` only reads: it starts nothing. An older herdr without it prints no JSON, and the answer is then "unknown".
REMOTE_PROBE = ("sh -c 'P=$( { command -v herdr || \"$SHELL\" -lc \"command -v herdr\"; } 2>/dev/null | grep \"^/\" | tail -n 1 ); "
                "[ -n \"$P\" ] || exit 1; echo \"$P\"; \"$P\" session list --json 2>/dev/null; exit 0'")


def ssh_probe(target):
    """(state, detail, running). Can THIS host ssh to target with its key, and is herdr there? `running` is True when its default
    session has a server up, False when it has not, None when that could not be told. Never prompts, never trusts a new host key
    on its own (a first connect is a decision a person makes at a terminal)."""
    try:
        p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", "-o", "StrictHostKeyChecking=yes",
                            "-o", "ControlMaster=no", "-o", "ControlPath=none", target, REMOTE_PROBE],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=14)
    except subprocess.TimeoutExpired:
        return "down", "timed out", None
    # ssh prints advisory banners ("** The server may need to be upgraded", warnings) on stderr; they are
    # not the failure, so keep only the lines that are.
    err_lines = [l.strip() for l in (p.stderr or "").splitlines()
                 if l.strip() and not l.lstrip().startswith(("**", "#", "Warning:", "WARNING"))]
    err = "\n".join(err_lines)
    low = err.lower()
    out_lines = [l.strip() for l in (p.stdout or "").splitlines() if l.strip()]
    found = [l for l in out_lines if l.startswith("/")]
    if p.returncode == 0 and found:
        running = None
        for l in out_lines:
            if l.startswith("{"):
                try:
                    sessions = json.loads(l)["sessions"]
                    if not isinstance(sessions, list):
                        raise TypeError("sessions is not a list")
                    running = any(isinstance(x, dict) and x.get("name") == "default" and x.get("running") is True for x in sessions)
                except (ValueError, KeyError, TypeError):
                    running = None
                break
        return "ready", found[-1], running
    if p.returncode in (0, 1) and not err:
        return "noherdr", "herdr not found on PATH or in the login shell", None
    if "permission denied" in low:
        return "nokey", "this host's key is not authorized there", None
    if "host key verification failed" in low or "no matching host key" in low or "remote host identification" in low:
        return "hostkey", "first connect needed: run `ssh " + target + "` once and check the fingerprint", None
    return "down", (err_lines or ["unreachable"])[-1][:120], None


def tailnet_peers():
    d = json.loads(subprocess.run(["tailscale", "status", "--json"], stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=8).stdout)
    me = d["Self"]
    saved = {m.get("label") for m in machine_profiles()} | {m.get("target") for m in machine_profiles()}
    out = []
    for p in d.get("Peer", {}).values():
        short = p["DNSName"].split(".")[0]
        # Only nodes that belong to this tailnet user. Shared-in nodes are somebody else's machines.
        if p.get("UserID") != me["UserID"] or not p.get("Online") or p.get("OS") not in ("linux", "macOS"):
            continue
        if short in saved or short == me["DNSName"].split(".")[0]:
            continue
        out.append({"name": short, "dns": p["DNSName"].rstrip("."), "os": p["OS"],
                    "ip": (p.get("TailscaleIPs") or [""])[0], "target": short})
    return sorted(out, key=lambda x: x["name"])


def scan_tailnet(force=True):
    """Probe the online nodes that belong to you. `force` (the Hosts tab's Scan button) asks every one afresh; a background scan
    reuses a recent "no herdr / needs key / first connect" so it does not log in to the same node every few minutes for nothing."""
    try:
        peers = tailnet_peers()
        now = time.time()
        todo, answers = [], {}
        for p in peers:
            m = SCAN_MEMO.get(p["name"])
            if not force and m and now - m[0] < RECHECK_S.get(m[1], 0):
                answers[p["name"]] = m
            else:
                todo.append(p)
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            results = list(ex.map(lambda p: ssh_probe(p["target"]), todo))
        for p, (state, detail, running) in zip(todo, results):
            SCAN_MEMO[p["name"]] = answers[p["name"]] = (now, state, detail, running)
        for p in peers:
            _t, p["state"], p["detail"], p["running"] = answers[p["name"]]
        with LOCK:
            SCAN.update(at=now, peers=peers, probed=len(todo))
        if AUTOADD:
            auto_add(peers)
    except Exception as e:
        print(f"shepr: tailnet scan failed: {e!r}", file=sys.stderr)
    finally:
        with LOCK:
            SCAN["running"] = False


def auto_add(peers):
    """Save, without asking, the scanned nodes that are running a herdr server (SHEPR_AUTOADD=1). The same `herdr machine add` the
    Add button runs, so herdr checks the install and installs nothing. Only a node that is yours (tailnet_peers), whose key already
    works and whose host key is already known (ssh_probe), and whose DEFAULT session has a server up: a node that merely has
    herdr installed is left for the Add button, because adding starts a server there. Every add, and every failure, is audited."""
    if READONLY:
        return
    added = 0
    for p in peers:
        label = p["name"]
        if added >= AUTOADD_MAX:
            break
        if p.get("state") != "ready" or p.get("running") is not True:
            continue
        if not (LABEL_RE.match(label) and LABEL_RE.match(p["target"])):   # a name ssh could read as an option, or herdr would refuse
            continue
        if time.time() - AUTO_FAILED.get(label, 0) < AUTOADD_RETRY_S:
            continue
        step = {"op": "machine", "args": ["add", p["target"], "--label", label]}
        plan = {"kind": "host_add_auto", "host": LOCAL, "pane": "", "steps": [step]}
        try:
            run_herdr(None, ["machine"] + step["args"], timeout=60, want_json=False)
        except HerdrError as e:
            AUTO_FAILED[label] = time.time()
            audit(plan, False, f"{e.code}: {e.message}")
            continue
        audit(plan, True, "")
        added += 1
    if added:
        poll_all()


def autoscan_loop():
    """Scan on a slow clock for as long as the server runs. A scan the Hosts tab started is left to finish, not run twice."""
    time.sleep(20)   # the first poll and the tailnet identity settle first
    while True:
        try:
            with LOCK:
                go = not SCAN["running"]
                if go:
                    SCAN["running"] = True
            if go:
                scan_tailnet(force=False)   # clears SCAN["running"] itself
                with LOCK:
                    n, asked, up = len(SCAN["peers"]), SCAN.get("probed", 0), sum(1 for p in SCAN["peers"] if p.get("running") is True)
                print(f"shepr: tailnet scan: {n} unsaved nodes online, {asked} asked, {up} running herdr", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"shepr: automatic scan failed: {e!r}", file=sys.stderr)
        time.sleep(SCAN_EVERY_S)


def start_scan():
    with LOCK:
        if SCAN["running"] or time.time() - SCAN["at"] < 20:
            return False
        SCAN["running"] = True
    threading.Thread(target=scan_tailnet, daemon=True).start()
    return True


def hosts_view():
    profiles = machine_profiles()
    with LOCK:
        status = {h["id"]: h for h in SNAP["hosts"]}
        scan = {"at": SCAN["at"], "running": SCAN["running"], "peers": [dict(p) for p in SCAN["peers"]]}
    saved = [{"id": LOCAL, "kind": "local", "enabled": True, "target": None,
              "status": (status.get(LOCAL) or {}).get("status"), "counts": (status.get(LOCAL) or {}).get("counts", {})}]
    for m in profiles:
        st = status.get(m.get("label")) or {}
        saved.append({"id": m.get("label"), "profile": m.get("id"), "kind": "remote", "enabled": bool(m.get("enabled")),
                      "target": m.get("target"), "status": st.get("status"), "error": st.get("error"), "last_ok": st.get("last_ok"),
                      "counts": st.get("counts", {})})
    for name, _route in local_sessions()[0]:   # the other running herdr sessions on this machine: listed, never switched (they are not saved machines)
        hid = f"{LOCAL}/{name}"
        st = status.get(hid) or {}
        if all(x["id"] != hid for x in saved):
            saved.insert(1 + sum(1 for x in saved if x["kind"] == "session"),
                         {"id": hid, "kind": "session", "enabled": True, "target": None, "session": name, "status": st.get("status"),
                          "error": st.get("error"), "last_ok": st.get("last_ok"), "counts": st.get("counts", {})})
    pk = hub_pubkey()
    # shlex.quote: a key comment is free text ("fred's laptop", or a quote that would close the
    # single quotes). The Copy key button pastes this line onto another machine.
    return {"saved": saved, "scan": scan, "hub": LOCAL, "pubkey": pk,
            "enroll": "mkdir -p ~/.ssh && echo " + shlex.quote(pk) + " >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys" if pk else None}


# --------------------------------------------------------------------------------------------
# slash commands and skills, for the compose box: what exists on this host, and what was used lately
# --------------------------------------------------------------------------------------------
BUILTIN_CMDS = [("clear", "Clear the conversation"), ("compact", "Summarise the conversation to free context"),
                ("context", "Show what is using the context window"), ("cost", "Show token use and cost"),
                ("model", "Switch model"), ("effort", "Set the thinking effort"), ("resume", "Resume an earlier session"),
                ("rename", "Rename this session"), ("memory", "Edit memory files"), ("config", "Open settings"),
                ("status", "Show account and session status"), ("mcp", "Manage MCP servers"), ("agents", "Manage subagents"),
                ("permissions", "Manage tool permissions"), ("review", "Review a pull request"), ("init", "Create a CLAUDE.md"),
                ("help", "Show help"), ("rc", "Remote control"), ("btw", "Ask a side question"),
                ("exit", "Ends the session"), ("quit", "Ends the session")]
NEVER_RECENT = {"exit", "quit", "logout", "login"}   # a chip that ends the session is a trap on a phone
_cmd_cache = {"at": 0.0, "all": []}


def read_frontmatter(path):
    """name / description from a SKILL.md or command .md. Tolerant: quoted values, folded (> or |) descriptions."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(4000)
    except OSError:
        return {}
    if not head.startswith("---"):
        return {}
    block = head.split("---", 2)[1] if head.count("---") >= 2 else ""
    out, key = {}, None
    for line in block.splitlines():
        m = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if m:
            key = m.group(1).lower()
            v = m.group(2).strip()
            out[key] = "" if v in (">", "|", ">-", "|-") else v.strip("\"'")
        elif key and line.startswith((" ", "\t")):
            out[key] = (out.get(key, "") + " " + line.strip()).strip()
    return out


def one_line(t, n=150):
    t = re.sub(r"\s+", " ", t or "").strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def scan_commands():
    found = {}
    def add(name, desc, kind):
        if name and re.match(r"^[A-Za-z][\w:.-]*$", name) and name not in found:
            found[name] = {"name": name, "desc": one_line(desc), "kind": kind}
    for p in sorted(glob.glob(os.path.join(CLAUDE_HOME, "skills", "*", "SKILL.md"))):
        fm = read_frontmatter(p)
        add(fm.get("name") or os.path.basename(os.path.dirname(p)), fm.get("description", ""), "skill")
    for p in sorted(glob.glob(os.path.join(CLAUDE_HOME, "commands", "**", "*.md"), recursive=True)):
        rel = os.path.relpath(p, os.path.join(CLAUDE_HOME, "commands"))[:-3].replace(os.sep, ":")
        add(rel, read_frontmatter(p).get("description", ""), "command")
    # plugins: cache/<marketplace>/<plugin>/<version>/{skills/*/SKILL.md, commands/*.md}; newest version of each plugin
    base = os.path.join(CLAUDE_HOME, "plugins", "cache")
    for plug_dir in sorted(glob.glob(os.path.join(base, "*", "*"))):
        vers = sorted(glob.glob(os.path.join(plug_dir, "*")), key=os.path.getmtime)
        if not vers:
            continue
        v, plug = vers[-1], os.path.basename(plug_dir)
        for p in sorted(glob.glob(os.path.join(v, "skills", "*", "SKILL.md"))):
            fm = read_frontmatter(p)
            add(f"{plug}:{fm.get('name') or os.path.basename(os.path.dirname(p))}", fm.get("description", ""), "plugin")
        for p in sorted(glob.glob(os.path.join(v, "commands", "*.md"))):
            add(f"{plug}:{os.path.basename(p)[:-3]}", read_frontmatter(p).get("description", ""), "plugin")
    for n, d in BUILTIN_CMDS:
        add(n, d, "builtin")
    return sorted(found.values(), key=lambda c: c["name"].lower())


def recent_commands(limit=6, scan=6000):
    """The slash commands actually typed lately, newest first, from Claude Code's own prompt history."""
    names = []
    try:
        with open(os.path.join(CLAUDE_HOME, "history.jsonl"), "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 1_500_000))
            lines = f.read().decode("utf-8", "replace").splitlines()[-scan:]
    except OSError:
        return names
    for line in reversed(lines):
        try:
            disp = json.loads(line).get("display") or ""
        except (ValueError, AttributeError):   # not JSON, or JSON that is not an object
            continue
        if not isinstance(disp, str):
            continue
        # "/name" then whitespace or end: a path like /home/pi/x never matches
        m = re.match(r"^/([A-Za-z][\w:.-]*)(?:\s|$)", disp)
        if m and m.group(1) not in names and m.group(1).lower() not in NEVER_RECENT:
            names.append(m.group(1))
            if len(names) >= limit:
                break
    return names


def commands_view(host):
    local = host["kind"] == "local"
    now = time.time()
    if now - _cmd_cache["at"] > 60:
        _cmd_cache.update(at=now, all=scan_commands())
    allc = _cmd_cache["all"]
    if not local:      # a remote host's skills are not read from here: built-ins only, and say so
        allc = [c for c in allc if c["kind"] == "builtin"]
    by = {c["name"]: c for c in allc}
    recent = [by.get(n) or {"name": n, "desc": "", "kind": "used"} for n in (recent_commands() if local else [])]
    return {"host": host["id"], "local": local, "recent": recent, "all": allc,
            "note": None if local else "Only built-in commands are listed for a remote host."}


def register_plan(plan):
    with LOCK:
        for k in [k for k, p in PLANS.items() if p["expires"] < time.time()]:
            del PLANS[k]
        PLANS[plan["nonce"]] = plan


def plan_host(body):
    kind = body["kind"]
    lines, steps, target = [], [], "Hosts"
    if kind in ("host_enable", "host_disable"):
        label = str(body.get("id", ""))
        m = next((m for m in machine_profiles() if m.get("label") == label), None)
        if not m:
            raise HerdrError("bad_host", f"no saved herdr machine called {label!r}")
        on = kind == "host_enable"
        steps = [{"op": "machine", "args": ["enable" if on else "disable", m["id"]]}]
        lines = [("Does", ("Turns ON" if on else "Turns OFF") + f" herdr access to {label}"),
                 ("Over", f"ssh {m.get('target')} (OpenSSH keeps the credentials)"),
                 ("Note", "remote herdr sessions keep running either way")]
        target = label
    elif kind == "host_add":
        name = str(body.get("name", ""))
        with LOCK:
            peer = next((dict(p) for p in SCAN["peers"] if p["name"] == name and p.get("state") == "ready"), None)
        if not peer:
            raise HerdrError("bad_host", f"{name!r} is not a host the last scan found ready: scan again")
        label = str(body.get("label") or name).lower()
        if not LABEL_RE.match(label):
            raise HerdrError("bad_label", "label: lowercase letters, digits, - and _")
        steps = [{"op": "machine", "args": ["add", peer["target"], "--label", label]}]
        lines = [("Saves", f"herdr machine {label} -> ssh {peer['target']} ({peer['ip']})"),
                 ("Note", "herdr checks the remote install and starts its server; it installs nothing without a terminal")]
        target = label
    elif kind == "device_revoke":
        did = str(body.get("id", ""))
        d = devices().get(did)
        if not d:
            raise HerdrError("bad_device", "no such device")
        steps = [{"op": "revoke", "id": did}]
        lines = [("Removes", f"{d['name']}  {dev_public(did, d)['fp']}"),
                 ("Effect", "that phone is signed out at once and must scan a new QR to come back")]
        target = d["name"]
    else:
        raise HerdrError("bad_kind", kind)
    plan = {"nonce": secrets.token_urlsafe(18), "kind": kind, "steps": steps, "expires": time.time() + NONCE_TTL_S}
    register_plan(plan)
    return {"nonce": plan["nonce"], "kind": kind, "expires_s": NONCE_TTL_S, "approves": False,
            "target": {"host": LOCAL, "pane": "", "title": target, "project": "", "status": ""},
            "lines": [{"k": k, "v": v} for k, v in lines]}


def commit_host(plan):
    step = plan["steps"][0]
    if step["op"] == "revoke":
        mutate_devices(lambda devs: devs.pop(step["id"], None))
        forget_push(step["id"])
        audit(plan, True, "")
        return {"ok": True, "sent": ["revoke"]}
    try:
        run_herdr(None, ["machine"] + step["args"], timeout=60, want_json=False)
    except HerdrError as e:
        audit(plan, False, f"{e.code}: {e.message}")
        raise HerdrError(e.code, f"{e.message}. A failed write may still have landed: check the Hosts tab before trying again.")
    audit(plan, True, "")
    poll_all()
    return {"ok": True, "sent": ["machine"]}


WAKE_MAX = 40
WAKE_GAP_S = 0.5



# --------------------------------------------------------------------------------------------
# waking a PROJECT: a herdr workspace whose terminal has no agent in it. Shepr types `claude --continue` (or a fresh
# `claude`) into that terminal, but ONLY when the terminal is an idle shell prompt, and only after you confirm.
# --------------------------------------------------------------------------------------------
SHELLS = {"bash", "zsh", "fish", "sh", "dash"}
START_CMD = os.environ.get("SHEPR_CLAUDE_CMD", "claude")   # the command typed into the terminal (tests use an echo)
CODEX_CMD = os.environ.get("SHEPR_CODEX_CMD", "codex")
CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
AGENTS = {"claude": "Claude", "codex": "Codex"}
_codex_idx = {"t": 0.0, "map": {}, "cwd_of": {}}


def codex_last_active(cwd):
    """Local only: when the newest Codex session that ran in this directory was last written, or None. Codex records the
    directory in the first line of every session file; the index is rebuilt at most once a minute."""
    ix = _codex_idx
    if time.time() - ix["t"] > 60:
        m = {}
        for root, _dirs, files in os.walk(os.path.join(CODEX_HOME, "sessions")):
            for fn in files:
                if not fn.endswith(".jsonl"):
                    continue
                path = os.path.join(root, fn)
                try:
                    mt = os.path.getmtime(path)
                    if path not in ix["cwd_of"]:
                        with open(path, "rb") as f:
                            first = json.loads(f.readline(400_000))
                        ix["cwd_of"][path] = ((first.get("payload") or {}).get("cwd") or "") if first.get("type") == "session_meta" else ""
                    c = ix["cwd_of"][path]
                    if c:
                        m[c] = max(m.get(c, 0), mt)
                except (OSError, ValueError, AttributeError, TypeError):   # a first line that is JSON but not the object Codex writes
                    ix["cwd_of"].setdefault(path, "")
                    continue
        ix["map"], ix["t"] = m, time.time()
    return ix["map"].get(cwd)


def project_last_active(cwd):
    """Local only: when the newest Claude conversation in this directory was last written, or None if it has none."""
    d = os.path.join(CLAUDE_HOME, "projects", re.sub(r"[^A-Za-z0-9]", "-", cwd or ""))
    try:
        return max((e.stat().st_mtime for e in os.scandir(d) if e.name.endswith(".jsonl")), default=None)
    except OSError:
        return None


def ago(ts):
    s = max(0, int(time.time() - ts))
    for n, unit in ((86400 * 30, "month"), (86400 * 7, "week"), (86400, "day"), (3600, "hour"), (60, "minute")):
        if s >= n:
            k = s // n
            return f"{k} {unit}{'s' if k != 1 else ''} ago"
    return "just now"


def _asleep_host(h):
    """The sleeping projects on one host; raises HerdrError/KeyError/TypeError when the host cannot be read."""
    items = []
    wl = {w["workspace_id"]: w for w in run_herdr(h["machine"], ["workspace", "list"], timeout=9)["result"]["workspaces"]}
    panes = run_herdr(h["machine"], ["pane", "list"], timeout=9)["result"]["panes"]
    done = set()
    for p in panes:
        if not isinstance(p, dict) or p.get("agent_status") != "unknown" or p.get("agent") or not isinstance(p.get("pane_id"), str):
            continue
        wsid = p.get("workspace_id")
        w = wl.get(wsid) or {}
        if wsid in done or w.get("agent_status") != "unknown":   # a workspace with an agent anywhere is not asleep
            continue
        done.add(wsid)
        cwd = p.get("foreground_cwd") or p.get("cwd") or ""
        items.append({"host": h["id"], "pane": p["pane_id"], "label": w.get("label") or os.path.basename(cwd.rstrip("/")) or p["pane_id"],
                      "cwd": cwd})
        if h["kind"] == "local":
            it = items[-1]
            it["claude"], it["codex"] = project_last_active(cwd), codex_last_active(cwd)
            it["last"] = max(it["claude"] or 0, it["codex"] or 0) or None
        else:
            items[-1].update(claude=None, codex=None, last=None)
    return items


def asleep_view():
    """Every project with no agent running, newest conversation first, across all enabled hosts. Hosts are read in parallel,
    and one the poller already knows is down is reported at once instead of costing two 9 s ssh timeouts."""
    items, errors, futs = [], [], {}
    for h in discover_hosts():
        if not h["enabled"]:
            continue
        with LOCK:
            known = (HOST_SNAP.get(h["id"]) or {}).get("entry") or {}
        if known.get("status") == "down":
            errors.append({"host": h["id"], "error": str(known.get("error") or "down")[:160]})
            continue
        futs[_pool.submit(_asleep_host, h)] = h
    for f, h in futs.items():
        try:
            items += f.result()
        except (HerdrError, KeyError, TypeError) as e:
            errors.append({"host": h["id"], "error": str(e)[:160]})
    items.sort(key=lambda i: (-(i["last"] or 0), i["label"].lower()))
    return {"items": items[:400], "errors": errors, "at": time.time()}


def pane_idle_shell(host, pane):
    """The pane, provided no agent is in it and its ONLY foreground process is its own idle shell."""
    m = host["machine"]
    info = run_herdr(m, ["pane", "get", pane], timeout=8)["result"]["pane"]
    if info.get("agent_status") != "unknown" or info.get("agent"):
        raise HerdrError("not_asleep", "an agent is already running in that terminal")
    pi = run_herdr(m, ["pane", "process-info", "--pane", pane], timeout=8)["result"]["process_info"]
    fg = pi.get("foreground_processes") or []
    if len(fg) != 1 or fg[0].get("pid") != pi.get("shell_pid") or fg[0].get("name") not in SHELLS:
        names = ", ".join(str(f.get("name")) for f in fg) or "something"
        raise HerdrError("not_idle_shell", f"that terminal is running {names}, not an idle shell prompt, so nothing was started in it")
    return info, pi


def plan_start(body):
    host = need_host(str(body.get("host", "")))
    pane = str(body.get("pane", ""))
    if not PANE_RE.match(pane):
        raise HerdrError("bad_pane", "not a pane id")
    mode = body.get("mode")
    if mode not in ("continue", "fresh"):
        raise HerdrError("bad_mode", "mode must be continue or fresh")
    agent = body.get("agent", "claude")
    if agent not in AGENTS:
        raise HerdrError("bad_agent", "agent must be claude or codex")
    info, pi = pane_idle_shell(host, pane)
    cwd = info.get("foreground_cwd") or info.get("cwd") or ""
    try:
        label = next((w.get("label") for w in run_herdr(host["machine"], ["workspace", "list"], timeout=8)["result"]["workspaces"]
                      if w.get("workspace_id") == info.get("workspace_id")), None)
    except (HerdrError, KeyError, TypeError, AttributeError):
        label = None
    label = label or os.path.basename(cwd.rstrip("/")) or pane
    if agent == "claude":
        cmd = START_CMD + (" --continue" if mode == "continue" else "")
        last = project_last_active(cwd) if host["kind"] == "local" else None
    else:
        cmd = CODEX_CMD + (" resume --last" if mode == "continue" else "")
        last = codex_last_active(cwd) if host["kind"] == "local" else None
    who = AGENTS[agent]
    plan = {"nonce": secrets.token_urlsafe(18), "host": host["id"], "pane": pane, "kind": "start",
            "steps": [{"op": "run", "cmd": cmd}], "snap": {"pid": pi.get("shell_pid"), "cwd": cwd},
            "expires": time.time() + NONCE_TTL_S}
    register_plan(plan)
    lines = [("Starts", f"{who}: {cmd}"), ("In", cwd), ("Project", f"{label}  ({host['id']}, {pane})")]
    if mode == "continue":
        lines.append(("Resumes", f"the last {who} conversation there, from {ago(last)}" if last else f"the last {who} conversation there, if it has one"))
    else:
        lines.append(("Starts", f"a new {who} conversation"))
    lines.append(("Note", "types that command into the terminal and presses Enter. Nothing else is sent, and nothing is approved."))
    return {"nonce": plan["nonce"], "kind": "start", "expires_s": NONCE_TTL_S, "approves": False,
            "target": {"host": host["id"], "pane": pane, "title": label, "project": "", "status": "asleep"},
            "lines": [{"k": k, "v": v} for k, v in lines]}


def commit_start(plan):
    host = need_host(plan["host"])
    with pane_lock(plan["host"], plan["pane"]):
        info, pi = pane_idle_shell(host, plan["pane"])   # still asleep, still an idle shell?
        cwd = info.get("foreground_cwd") or info.get("cwd") or ""
        if pi.get("shell_pid") != plan["snap"]["pid"] or cwd != plan["snap"]["cwd"]:
            raise HerdrError("moved_on", "that terminal changed since you looked, so nothing was started.")
        try:
            run_step(host, plan["pane"], plan["steps"][0])
        except HerdrError as e:
            audit(plan, False, f"{e.code}: {e.message}")
            raise HerdrError(e.code, f"{e.message}. A failed write may still have landed: look at the terminal before trying again.")
    audit(plan, True, "")
    return {"ok": True, "sent": ["run"]}



# --------------------------------------------------------------------------------------------
# a NEW project: a new herdr workspace (its title-bar name), a folder under ~/Projects, and the start command (SHEPR_NEW_CMD) typed in it
# --------------------------------------------------------------------------------------------
NEW_CMD = os.environ.get("SHEPR_NEW_CMD", "claude")           # what is typed to start the session; a shell alias of your own works too (set SHEPR_NEW_CMD in the service)
PROJECTS_DIR = os.environ.get("SHEPR_PROJECTS", "~/Projects")   # where project folders live (tests point it at /tmp)
DIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")      # one plain folder name: no slash, no leading dot or dash


def shell_path(path):
    """A folder as ONE shell word. The folder name is already one plain name (DIR_RE); the projects folder in front of it
    is whatever SHEPR_PROJECTS says, and a space or an apostrophe in it split the command (mkdir made the wrong folders
    and cd failed) or swallowed the && after it (the start command ran in the wrong place). shlex.quote leaves a plain path
    exactly as it was, and a leading ~/ stays outside the quotes so the remote machine's own home still expands."""
    return "~/" + shlex.quote(path[2:]) if path.startswith("~/") else shlex.quote(path)


def alias_expansion(name):
    """Best effort, local only: what a shell alias expands to, read from the alias files (nothing is executed)."""
    files = [os.path.expanduser("~/.bashrc"), os.path.expanduser("~/.bash_aliases")] + sorted(glob.glob(os.path.expanduser("~/.config/bash/*.sh")))
    found = None
    rx = re.compile(r"^\s*alias\s+" + re.escape(name) + r"=(['\"])(.*?)\1\s*(?:#.*)?$")
    for fp in files:
        try:
            for line in open(fp, errors="replace"):
                m = rx.match(line)
                if m:
                    found = m.group(2)
        except OSError:
            continue
    return found


def plan_new_project(body):
    host = need_host(str(body.get("host", "")))
    name = body.get("name")
    if not isinstance(name, str) or CTRL_RE.search(name) or "\n" in name:
        raise HerdrError("bad_name", "the project name must be plain text on one line")
    name = " ".join(name.split())
    if not 1 <= len(name) <= 60:
        raise HerdrError("bad_name", "the project name must be 1 to 60 characters")
    d = body.get("dir")
    if not isinstance(d, str) or not DIR_RE.match(d) or d.endswith("."):
        raise HerdrError("bad_dir", "the folder must be one plain name: letters, digits, dot, dash or underscore, starting with a letter or digit")
    path = f"{PROJECTS_DIR.rstrip('/')}/{d}"
    msg = body.get("message")
    if msg is not None:
        if not isinstance(msg, str) or CTRL_RE.search(msg) or "\n" in msg or len(msg) > 500:
            raise HerdrError("bad_message", "the first message must be one line of up to 500 characters")
        msg = msg.strip() or None
    # The message and the folder are data for the shell: each quoted as ONE word, so quotes, $, backticks and ; in them can never run anything.
    cmd = f"mkdir -p {shell_path(path)} && cd {shell_path(path)} && {NEW_CMD}" + (f" {shlex.quote(msg)}" if msg else "")
    lines = [("Title-bar name", name), ("Folder", path), ("Host", host["id"])]
    if host["kind"] == "local":
        real = os.path.expanduser(path)
        lines[1] = ("Folder", path + ("   (already exists: it will be reused)" if os.path.isdir(real) else "   (will be created)"))
    try:
        if any(w.get("label") == name for w in run_herdr(host["machine"], ["workspace", "list"], timeout=8)["result"]["workspaces"]):
            lines.append(("Heads up", "a herdr project with this name already exists. A second one will be made."))
    except (HerdrError, KeyError, TypeError, AttributeError):
        pass
    if msg:
        lines.append(("First message", msg))
    lines.append(("Then types", cmd))
    ex = alias_expansion(NEW_CMD) if host["kind"] == "local" else None
    if ex:
        lines.append((f"{NEW_CMD} runs", ex))
    lines.append(("Note", "makes a new herdr project in the background (it will not take over your screen), then types that command into it."))
    plan = {"nonce": secrets.token_urlsafe(18), "host": host["id"], "pane": "", "kind": "new_project",
            "steps": [{"op": "new_project", "label": name, "cmd": cmd, "cwd": None if host["kind"] != "local" else os.path.expanduser(PROJECTS_DIR)}],
            "expires": time.time() + NONCE_TTL_S}
    register_plan(plan)
    return {"nonce": plan["nonce"], "kind": "new_project", "expires_s": NONCE_TTL_S, "approves": False,
            "target": {"host": host["id"], "pane": "", "title": name, "project": "", "status": ""},
            "lines": [{"k": k, "v": v} for k, v in lines]}


def commit_new_project(plan):
    host = need_host(plan["host"])
    st = plan["steps"][0]
    args = ["workspace", "create", "--label", st["label"], "--no-focus"]
    if st.get("cwd") and os.path.isdir(st["cwd"]):
        args += ["--cwd", st["cwd"]]
    try:
        r = run_herdr(host["machine"], args, timeout=15)
        pane = r["result"]["root_pane"]["pane_id"]
        if not PANE_RE.match(pane):
            raise HerdrError("bad_response", "herdr did not say which terminal the new project got")
    except (HerdrError, KeyError, TypeError) as e:
        msg = e.message if isinstance(e, HerdrError) else "herdr's answer was not in the expected shape"
        audit(plan, False, f"create: {msg}")
        raise HerdrError(getattr(e, "code", "bad_response"), f"{msg}. Nothing was typed. A failed write may still have landed: look at herdr before trying again.")
    try:
        run_herdr(host["machine"], ["pane", "run", pane, st["cmd"]], timeout=10, want_json=False)
    except HerdrError as e:
        audit(plan, False, f"run: {e.code}: {e.message}")
        raise HerdrError(e.code, f"The project {st['label']!r} was created ({pane}) but typing the command failed: {e.message}. Look at that terminal before trying again.")
    audit(plan, True, "")
    return {"ok": True, "sent": ["workspace", "run"], "pane": pane}


# --------------------------------------------------------------------------------------------
# an EXISTING folder opened as a herdr project: listed, planned and committed without creating or changing anything in it
# --------------------------------------------------------------------------------------------
FOLDERS_MAX = 600


def _herdr_dirs(host):
    """Every directory a herdr terminal on this host is working in."""
    out = set()
    for p in run_herdr(host["machine"], ["pane", "list"], timeout=9)["result"]["panes"]:
        if isinstance(p, dict):
            for k in ("foreground_cwd", "cwd"):
                c = p.get(k)
                if isinstance(c, str) and c:
                    out.add(c.rstrip("/"))
    return out


def folders_view(host):
    """The folders directly inside the projects folder that herdr has no terminal in, newest conversation first. Read only."""
    if host["kind"] != "local":
        raise HerdrError("local_only", "folders can only be listed on the machine Shepr runs on")
    shown = os.path.expanduser(PROJECTS_DIR).rstrip("/")
    base = os.path.realpath(shown)
    try:
        names = os.listdir(base)
    except OSError:
        names = []
    try:
        busy = _herdr_dirs(host)
    except (HerdrError, KeyError, TypeError):
        busy = set()   # herdr could not be read: every folder is offered rather than none
    items, open_n = [], 0
    for d in names:
        if not DIR_RE.match(d) or d.endswith("."):
            continue   # the same names Start new accepts: nothing hidden, nothing odd
        path = os.path.join(base, d)
        if not os.path.isdir(path):
            continue
        if any(c == p or c.startswith(p + os.sep) for c in busy for p in (path, os.path.join(shown, d))):
            open_n += 1   # herdr already has a terminal in it: it is a project already
            continue
        cl, cx = project_last_active(path), codex_last_active(path)
        items.append({"dir": d, "claude": cl, "codex": cx, "last": max(cl or 0, cx or 0) or None, "git": os.path.isdir(os.path.join(path, ".git"))})
    items.sort(key=lambda i: (-(i["last"] or 0), i["dir"].lower()))
    return {"base": PROJECTS_DIR, "items": items[:FOLDERS_MAX], "total": len(items), "open": open_n, "at": time.time()}


def plan_open_folder(body):
    """Open a folder that already exists as a new herdr project and start Claude or Codex in it. It never creates the folder, and
    nothing in it is touched: a folder that is not there is refused, with no mkdir anywhere in the command."""
    host = need_host(str(body.get("host", "")))
    if host["kind"] != "local":
        raise HerdrError("local_only", "opening an existing folder only works on the machine Shepr runs on")
    d = body.get("dir")
    if not isinstance(d, str) or not DIR_RE.match(d) or d.endswith("."):
        raise HerdrError("bad_dir", "the folder must be one plain name: letters, digits, dot, dash or underscore, starting with a letter or digit")
    agent = body.get("agent", "claude")
    if agent not in AGENTS:
        raise HerdrError("bad_agent", "agent must be claude or codex")
    mode = body.get("mode")
    if mode not in ("continue", "fresh"):
        raise HerdrError("bad_mode", "mode must be continue or fresh")
    name = body.get("name") if body.get("name") is not None else d
    if not isinstance(name, str) or CTRL_RE.search(name) or "\n" in name:
        raise HerdrError("bad_name", "the project name must be plain text on one line")
    name = " ".join(name.split())
    if not 1 <= len(name) <= 60:
        raise HerdrError("bad_name", "the project name must be 1 to 60 characters")
    path = f"{PROJECTS_DIR.rstrip('/')}/{d}"
    real = os.path.expanduser(path)
    if not os.path.isdir(real):
        raise HerdrError("no_folder", f"{path} is not a folder here, so nothing was opened. This never creates one: use New project for that.")
    who = AGENTS[agent]
    last = project_last_active(os.path.realpath(real)) if agent == "claude" else codex_last_active(os.path.realpath(real))
    if mode == "continue" and not last:
        raise HerdrError("nothing_to_resume", f"there is no {who} conversation recorded in that folder to resume. Choose Fresh session.")
    if agent == "claude":
        launcher = NEW_CMD + (" --continue" if mode == "continue" else "")
    else:
        launcher = CODEX_CMD + (" resume --last" if mode == "continue" else "")
    cmd = f"cd {shell_path(path)} && {launcher}"
    lines = [("Title-bar name", name), ("Folder", path + "   (exists, nothing will be created or changed)"), ("Host", host["id"]),
             ("Starts", f"{who} " + (f"and resumes the last conversation there, from {ago(last)}" if mode == "continue" else "with a new conversation"))]
    try:
        if any(w.get("label") == name for w in run_herdr(host["machine"], ["workspace", "list"], timeout=8)["result"]["workspaces"]):
            lines.append(("Heads up", "a herdr project with this name already exists. A second one will be made."))
        shown = os.path.expanduser(PROJECTS_DIR).rstrip("/")
        if any(c == p or c.startswith(p + os.sep) for c in _herdr_dirs(host) for p in (os.path.realpath(real), os.path.join(shown, d))):
            lines.append(("Heads up", "herdr already has a terminal in this folder. A second project will be made."))
    except (HerdrError, KeyError, TypeError, AttributeError):
        pass
    lines.append(("Then types", cmd))
    ex = alias_expansion(NEW_CMD) if agent == "claude" else None
    if ex:
        lines.append((f"{NEW_CMD} runs", ex))
    lines.append(("Note", "makes a new herdr project in the background (it will not take over your screen), then types that command into it."))
    plan = {"nonce": secrets.token_urlsafe(18), "host": host["id"], "pane": "", "kind": "open_folder",
            "steps": [{"op": "new_project", "label": name, "cmd": cmd, "cwd": real}], "expires": time.time() + NONCE_TTL_S}
    register_plan(plan)
    return {"nonce": plan["nonce"], "kind": "open_folder", "expires_s": NONCE_TTL_S, "approves": False,
            "target": {"host": host["id"], "pane": "", "title": name, "project": "", "status": ""},
            "lines": [{"k": k, "v": v} for k, v in lines]}


def plan_wake(body):
    """Send one message to each of several SLEEPING (idle or finished) sessions. A blocked session is a question
    waiting on a person, so it is refused here: this can never answer a dialog, let alone approve one."""
    text = body.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 1000 or "\x00" in text or "\n" in text.strip():
        raise HerdrError("bad_text", "one line, 1 to 1000 characters")
    targets = body.get("targets")
    if not isinstance(targets, list) or not 1 <= len(targets) <= WAKE_MAX:
        raise HerdrError("bad_targets", f"pick 1 to {WAKE_MAX} sessions")
    items, seen = [], set()
    for t in targets:
        if not isinstance(t, dict):
            raise HerdrError("bad_targets", f"pick 1 to {WAKE_MAX} sessions")
        hid, pane = str(t.get("host", "")), str(t.get("pane", ""))
        if not PANE_RE.match(pane):
            raise HerdrError("bad_pane", "not a pane id")
        if (hid, pane) in seen:
            continue
        seen.add((hid, pane))
        host = need_host(hid)
        a = get_agent(host, pane)
        name = os.path.basename((a.get("cwd") or "").rstrip("/")) or pane
        if a["agent_status"] not in ("idle", "done"):
            raise HerdrError("not_sleeping", f"{name} on {hid} is {a['agent_status']}, not sleeping: only idle or "
                                             "finished sessions can be woken. Reopen the list and look again.")
        items.append({"host": hid, "pane": pane, "name": name,
                      "snap": {"status": a["agent_status"], "seq": a.get("state_change_seq"),
                               "session": (a.get("agent_session") or {}).get("value")}})
    plan = {"nonce": secrets.token_urlsafe(18), "kind": "wake", "items": items, "text": text.strip(),
            "expires": time.time() + NONCE_TTL_S}
    register_plan(plan)
    names = "\n".join(f"  {i['name']}  ({i['host']})" for i in items)
    lines = [("Wakes", f"{len(items)} sleeping session{'s' if len(items) != 1 else ''}:\n{names}"),
             ("Each gets", plan["text"]),
              ("Note", "one message each, a moment apart. Any that moved on since you looked are skipped. Nothing is approved.")]
    return {"nonce": plan["nonce"], "kind": "wake", "expires_s": NONCE_TTL_S, "approves": False,
            "target": {"host": LOCAL, "pane": "", "title": f"{len(items)} sleeping", "project": "", "status": ""},
            "lines": [{"k": k, "v": v} for k, v in lines]}


def commit_wake(plan):
    # Law 22: waking many agents starts many model turns at once. Do not do that to a machine that is already busy.
    load = os.getloadavg()[0]
    if load > (os.cpu_count() or 4):
        raise HerdrError("busy", f"this host is busy (load {load:.0f}); nothing was sent. Try again in a minute.")
    sent, skipped, failed = [], [], []
    for n, it in enumerate(plan["items"]):
        try:
            host = need_host(it["host"])
            a = get_agent(host, it["pane"])
            snap = it["snap"]
            if (a["agent_status"], a.get("state_change_seq"), (a.get("agent_session") or {}).get("value")) != \
                    (snap["status"], snap["seq"], snap["session"]):
                skipped.append(it["name"])
                continue
            run_step(host, it["pane"], {"op": "prompt", "text": plan["text"]})
            sent.append(it["name"])
            audit({"host": it["host"], "pane": it["pane"], "kind": "wake",
                   "steps": [{"op": "prompt", "text": plan["text"]}]}, True, "")
        except HerdrError as e:
            failed.append(f"{it['name']}: {e.message}")
            audit({"host": it["host"], "pane": it["pane"], "kind": "wake",
                   "steps": [{"op": "prompt", "text": plan["text"]}]}, False, f"{e.code}: {e.message}")
        if n < len(plan["items"]) - 1:
            time.sleep(WAKE_GAP_S)
    if not sent and failed:
        raise HerdrError("error", "none were woken. " + "; ".join(failed[:3]) +
                         ". A failed write may still have landed: look at the queue before trying again.")
    return {"ok": True, "woke": sent, "skipped": skipped, "failed": failed}




# --------------------------------------------------------------------------------------------
# push notifications. The host sends an EMPTY web push ("something changed") signed with its own VAPID key; the phone's
# service worker then fetches the queue, signed with the phone's key, and decides what to say. So no message content ever
# passes through Apple's servers, and no payload encryption is needed on the host.
# --------------------------------------------------------------------------------------------
VAPID_SUB = os.environ.get("SHEPR_VAPID_SUB", "https://github.com/nixfred/shepr")
PUSH_HOSTS = ("web.push.apple.com", ".push.apple.com", "fcm.googleapis.com", "updates.push.services.mozilla.com", ".notify.windows.com")
PUSH_EXTRA = {h.strip() for h in os.environ.get("SHEPR_PUSH_ALLOW", "").split(",") if h.strip()}   # host:port allowed over plain http (tests)
PUSH_MIN_GAP_S = 20
PUSH = {}            # device id -> {"sub": {...}, "prefs": {"blocked": bool, "done": bool}, "t": epoch}
PUSH_LAST = {}       # device id -> when a push last went out (rate limit)
PUSH_STATUS = {}     # device id -> {at, code, why}: what the push service said to the last push, shown on the Keys screen
PUSH_EVENTS = []     # what the poller saw since the last dispatch: "blocked", "done", "quiet", "stopped", "risk"
# What a phone can ask to be told about. The first two are the original ones; the rest are the watch (off until asked for,
# except the fleet stopping, which is the one moment the whole thing is waiting on you).
PUSH_PREF_DEFAULTS = {"blocked": True, "done": False, "quiet": False, "stopped": False, "risk": False}
_vapid = {}


def ecdsa_sign(d, msg):
    """ES256 signature, raw r||s (what JWTs and WebCrypto use). Random nonce from the OS."""
    z = int.from_bytes(hashlib.sha256(msg).digest(), "big")
    while True:
        k = secrets.randbelow(_N - 1) + 1
        r = _mul(k, _G)[0] % _N
        s = pow(k, -1, _N) * (z + r * d) % _N
        if r and s:
            return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def vapid_keys():
    """This host's push identity, made once and kept 0600. Returns (private int, public key bytes 0x04||x||y)."""
    with LOCK:
        if not _vapid:
            try:
                with open(state_path("vapid.json")) as f:
                    j = json.load(f)
                d = int(j["d"], 16)
                if not 1 <= d < _N:
                    raise ValueError("not a P-256 private key")
            except FileNotFoundError:
                d = None
            except (OSError, ValueError, KeyError, TypeError) as e:
                # A new key silently ends every phone's notifications (a subscription is bound to the key it was made with),
                # so say it: the phones show "Turn on notifications" again and nobody has to guess why.
                print(f"shepr: the push key in vapid.json could not be read ({e!r}); a new one is made, and every phone must turn notifications on again", file=sys.stderr, flush=True)
                d = None
            if d is None:
                d = secrets.randbelow(_N - 1) + 1
                os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
                fd = os.open(state_path("vapid.json"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    json.dump({"d": format(d, "064x")}, f)
            x, y = _mul(d, _G)
            _vapid.update(d=d, pub=b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big"))
        return _vapid["d"], _vapid["pub"]


def vapid_jwt(aud):
    d, _pub = vapid_keys()
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = b64u(json.dumps({"aud": aud, "exp": int(time.time()) + 12 * 3600, "sub": VAPID_SUB}, separators=(",", ":")).encode())
    return f"{head}.{body}.{b64u(ecdsa_sign(d, (head + '.' + body).encode()))}"


def push_endpoint_ok(url):
    """Only the real push services (and, in tests, hosts named in SHEPR_PUSH_ALLOW). The host will POST to this URL, so it
    must never be something a phone can aim at an internal address."""
    try:
        u = urlsplit(url)
    except ValueError:
        return False
    if len(url) > 700 or u.username or u.password or not u.hostname:
        return False
    hostport = u.netloc.lower()
    if u.scheme == "http":
        return hostport in PUSH_EXTRA
    if u.scheme != "https" or (u.port not in (None, 443)):
        return hostport in PUSH_EXTRA
    h = u.hostname.lower()
    return any(h == p or (p.startswith(".") and h.endswith(p)) for p in PUSH_HOSTS) or hostport in PUSH_EXTRA


def load_push():
    try:
        with open(state_path("push.json")) as f:
            PUSH.clear(); PUSH.update(json.load(f))
    except (OSError, ValueError):
        pass


def save_push():
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    fd = os.open(state_path("push.json.tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(PUSH, f)
    os.replace(state_path("push.json.tmp"), state_path("push.json"))


def forget_push(dev_id):
    with LOCK:
        if PUSH.pop(dev_id, None) is not None:
            try:
                save_push()
            except OSError:
                pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def send_push(dev_id, why="test"):
    """One empty push to one device. Returns the HTTP status, or None if nothing was sent. 404/410 mean the phone dropped it."""
    with LOCK:
        ent = PUSH.get(dev_id)
        sub = (ent or {}).get("sub") or {}
    ep = sub.get("endpoint")
    if not ep or not push_endpoint_ok(ep):
        return None
    u = urlsplit(ep)
    _d, pub = vapid_keys()
    req = urllib.request.Request(ep, data=b"", method="POST", headers={
        "Authorization": f"vapid t={vapid_jwt(f'{u.scheme}://{u.netloc}')}, k={b64u(pub)}",
        "TTL": "3600", "Urgency": "high", "Content-Length": "0"})
    try:
        code = urllib.request.build_opener(_NoRedirect).open(req, timeout=10).status
    except urllib.error.HTTPError as e:
        code = e.code
    except OSError as e:
        print(f"shepr: push to {dev_id[:6]} failed: {e}", file=sys.stderr, flush=True)
        return None
    PUSH_LAST[dev_id] = time.time()
    PUSH_STATUS[dev_id] = {"at": int(time.time() * 1000), "code": code, "why": why}
    print(f"shepr: push ({why}) to {dev_id[:6]}: the push service answered {code}", file=sys.stderr, flush=True)
    if code in (404, 410):
        forget_push(dev_id)
    elif code >= 300:
        print(f"shepr: push service answered {code} for {dev_id[:6]}", file=sys.stderr, flush=True)
    return code


def dispatch_push(events):
    """A blocked or finished agent was just seen. Tell every phone that asked for that kind of news, at most once per
    PUSH_MIN_GAP_S each (the phone looks at the whole queue anyway, so one push covers any number of changes)."""
    kinds = set(events)
    devs = devices()
    with LOCK:
        gone = [d for d in PUSH if d not in devs]   # revoked by `shepr revoke`, another process that cannot reach this PUSH
        targets = [(d, e) for d, e in PUSH.items() if d in devs and any((e.get("prefs") or {}).get(k) for k in kinds)]
    for dev_id in gone:
        forget_push(dev_id)
    for dev_id, _e in targets:
        if time.time() - PUSH_LAST.get(dev_id, 0) < PUSH_MIN_GAP_S:
            continue
        send_push(dev_id, why="+".join(sorted(kinds)))


def push_config(dev_id):
    _d, pub = vapid_keys()
    with LOCK:
        ent = PUSH.get(dev_id)
    prefs = dict(PUSH_PREF_DEFAULTS, **((ent or {}).get("prefs") if isinstance((ent or {}).get("prefs"), dict) else {}))
    return {"key": b64u(pub), "subscribed": bool(ent), "prefs": {k: bool(prefs[k]) for k in PUSH_PREF_DEFAULTS},
            "last": PUSH_STATUS.get(dev_id)}


def push_subscribe(dev_id, body):
    sub = body.get("subscription")
    if not isinstance(sub, dict) or not isinstance(sub.get("endpoint"), str) or not push_endpoint_ok(sub["endpoint"]):
        raise HerdrError("bad_endpoint", "that is not a push address Shepr will send to")
    keys = sub.get("keys") if isinstance(sub.get("keys"), dict) else {}
    clean = {"endpoint": sub["endpoint"], "keys": {k: str(keys.get(k, ""))[:200] for k in ("p256dh", "auth")}}
    prefs = body.get("prefs") if isinstance(body.get("prefs"), dict) else {}
    with LOCK:
        was = (PUSH.get(dev_id) or {}).get("prefs")
        old = dict(PUSH_PREF_DEFAULTS, **(was if isinstance(was, dict) else {}))
        PUSH[dev_id] = {"sub": clean, "t": int(time.time()), "prefs": {k: bool(prefs.get(k, old[k])) for k in PUSH_PREF_DEFAULTS}}
        save_push()
    return push_config(dev_id)


def push_set_prefs(dev_id, body):
    with LOCK:
        ent = PUSH.get(dev_id)
        if not ent:
            raise HerdrError("not_subscribed", "this phone is not subscribed to notifications")
        old = dict(PUSH_PREF_DEFAULTS, **(ent.get("prefs") if isinstance(ent.get("prefs"), dict) else {}))   # a subscription saved by an older build has fewer kinds
        ent["prefs"] = {k: bool(body.get(k, old[k])) for k in PUSH_PREF_DEFAULTS}
        save_push()
    return push_config(dev_id)



# --------------------------------------------------------------------------------------------
# the waiting ledger: how long agents sat blocked on a person. Each blocked state the poller WATCHED begin and end (not one
# already true when the host started) becomes one line in waits.jsonl: who, when it began, when it ended.
# --------------------------------------------------------------------------------------------
WAIT_KEEP_DAYS = 30
WAIT_MAX_S = 24 * 3600


def record_wait(host_id, label, t0, t1):
    dur = t1 - t0
    if not 1 <= dur <= WAIT_MAX_S:
        return
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        fd = os.open(state_path("waits.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps({"t0": int(t0), "t1": int(t1), "host": host_id, "label": str(label)[:60]}) + "\n")
    except OSError:
        pass


def read_waits(since):
    out = []
    try:
        with open(state_path("waits.jsonl")) as f:
            for line in f:
                try:
                    w = json.loads(line)
                    if w["t1"] >= since:
                        out.append(w)
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return out


def prune_waits():
    keep = read_waits(time.time() - WAIT_KEEP_DAYS * 86400)
    try:
        with open(state_path("waits.jsonl")) as f:
            before = sum(1 for _ in f)
    except OSError:
        return
    if len(keep) < before:
        fd = os.open(state_path("waits.jsonl.tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            for w in keep:
                f.write(json.dumps(w) + "\n")
        os.replace(state_path("waits.jsonl.tmp"), state_path("waits.jsonl"))


# ---- subscription budget: how much of each plan is used, and whether that is ahead of or behind an even spend. Shepr measures it itself
# (usage.py: Claude Code's own login to Anthropic's usage endpoint, Codex's and Grok's own files, Kimi's endpoint when a key is set), so it works
# without any other app. SHEPR_USAGE=0 switches it off. SHEPR_USAGE_FIXTURE is for tests only: a view in this same format, returned as it is.
USAGE_FIXTURE = os.environ.get("SHEPR_USAGE_FIXTURE")
USAGE_FIXTURE_MAX = 2_000_000
METER = usage.Meter(STATE_DIR, CLAUDE_HOME)

# The Lock Screen Live Activity (the optional native app in ios/). The host builds the status once, here, so the phone and the pushes draw the same thing.
LIVE = shepr_live.Live(STATE_DIR, ecdsa_sign, LOCAL, valid=lambda: set(devices()), locker=state_lock)


def build_live_status(now=None):
    now = now if now is not None else time.time()
    q = build_queue()
    return shepr_live.build_status(q["agents"], q["hosts"], shepr_live.meters_from_view(budget_view()), now)


def live_call(fn):
    try:
        return fn()
    except shepr_live.LiveError as e:
        raise HerdrError(e.code, e.message)


def budget_view(now=None):
    now = now if now is not None else time.time()
    if not USAGE_FIXTURE:
        return METER.view()
    try:
        if os.stat(USAGE_FIXTURE).st_size > USAGE_FIXTURE_MAX:
            raise ValueError("too large")
        with open(USAGE_FIXTURE) as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        d = None
    if not isinstance(d, dict) or not isinstance(d.get("agents"), list):
        return {"available": False, "measuring": False, "reason": "The usage fixture is missing or not readable.", "agents": [], "now": int(now * 1000)}
    return dict(d, now=int(now * 1000))


def ledger_view(now=None, agents=None):
    """Today and the last 7 days of waiting (local calendar days), plus who is waiting right now."""
    now = now or time.time()
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    days = []
    for i in range(6, -1, -1):
        d0 = time.mktime(time.localtime(midnight - i * 86400 + 12 * 3600)[:3] + (0, 0, 0, 0, 0, -1))
        days.append({"d": time.strftime("%Y-%m-%d", time.localtime(d0 + 3600)), "start": d0, "n": 0, "secs": 0})
    waits = read_waits(days[0]["start"])
    today = []
    for w in waits:
        for dd in reversed(days):
            if w["t1"] >= dd["start"]:
                dd["n"] += 1; dd["secs"] += w["t1"] - w["t0"]
                if dd is days[-1]:
                    today.append(w)
                break
    top = sorted(today, key=lambda w: w["t0"] - w["t1"])[:5]
    cur = [a for a in (agents or []) if a.get("status") == "blocked"]
    return {"today": {"n": len(today), "secs": int(sum(w["t1"] - w["t0"] for w in today)),
                      "longest": ({"label": top[0]["label"], "secs": int(top[0]["t1"] - top[0]["t0"])} if top else None),
                      "top": [{"label": w["label"], "secs": int(w["t1"] - w["t0"]), "host": w["host"]} for w in top]},
            "days": [{"d": d["d"], "n": d["n"], "secs": int(d["secs"])} for d in days],
            "now": {"n": len(cur), "secs": int(sum(a.get("for_s", 0) for a in cur))}, "at": now}


# --------------------------------------------------------------------------------------------
# the watch, as the phone sees it: activity (what happened), burn (who used the plan), changes (what a project's git says)
# --------------------------------------------------------------------------------------------
ACTIVITY_MAX = 300
SENT_KINDS = {"text", "choose", "multi", "keys", "sendnow", "model", "effort", "wake", "start", "new_project", "open_folder"}


def _sent_text(rec):
    """What a send was, in a few words, from its audit record."""
    steps = rec.get("steps") if isinstance(rec.get("steps"), list) else []
    words = next((st.get("text") for st in steps if isinstance(st, dict) and isinstance(st.get("text"), str)), None)
    if words:
        return one_line(words, 120)
    keys = [k for st in steps if isinstance(st, dict) and isinstance(st.get("keys"), list) for k in st["keys"] if isinstance(k, str)]
    if keys:
        return "keys " + human_keys(keys[:12])
    cmd = next((st.get("cmd") for st in steps if isinstance(st, dict) and isinstance(st.get("cmd"), str)), None)
    return one_line(cmd, 120) if cmd else ""


def activity_view(since=None, limit=ACTIVITY_MAX, now=None):
    """What the fleet did, newest first: questions asked and answered, sessions finishing, agents going quiet, everything
    stopping, hosts dropping out, risky commands, and what was sent from a phone. `summary` counts each kind since `since`,
    which is what the "since you last looked" card shows."""
    now = now if now is not None else time.time()
    floor = now - EVENT_KEEP_DAYS * 86400
    try:
        since = max(floor, float(since)) if since not in (None, "") else now - 86400
    except (TypeError, ValueError):
        raise HerdrError("bad_request", "since must be a time in seconds")
    limit = max(1, min(ACTIVITY_MAX, int(limit)))
    with LOCK:
        names = {(a["host"], a["pane"]): (a.get("wlabel") or a.get("project") or a["pane"]) for a in SNAP["agents"]}
    items = read_events(since)
    try:
        with open(state_path("sends.jsonl"), "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - (1 << 20)))
            raw = f.read().decode("utf-8", "replace").split("\n")[(1 if size > (1 << 20) else 0):]
    except OSError:
        raw = []
    for line in raw:
        try:
            r = json.loads(line)
            if not isinstance(r, dict) or r.get("kind") not in SENT_KINDS or float(r["t"]) <= since:
                continue
        except (ValueError, KeyError, TypeError):
            continue
        items.append({"t": float(r["t"]), "ev": "sent" if r.get("ok") else "send_failed", "host": r.get("host"), "pane": r.get("pane"),
                      "label": names.get((r.get("host"), r.get("pane"))) or r.get("pane") or "", "kind": r.get("kind"), "text": _sent_text(r)})
    items.sort(key=lambda e: e["t"])
    summary = {}
    for e in items:
        summary[e["ev"]] = summary.get(e["ev"], 0) + 1
        if e["ev"] == "risk" and e.get("level") == "danger":   # "risk" is every risky command; "risk_danger" is how many of them were red
            summary["risk_danger"] = summary.get("risk_danger", 0) + 1
    out = []
    for e in reversed(items[-limit:]):
        row = {"t": e["t"], "ev": e["ev"], "host": e.get("host"), "pane": e.get("pane"), "label": e.get("label") or "",
               "open": bool(e.get("host") and e.get("pane") and (e.get("host"), e.get("pane")) in names)}
        for k in ("q", "wait", "mins", "level", "what", "cmd", "blocked", "done", "kind", "text", "why", "helper"):
            if e.get(k) is not None:
                row[k] = e[k]
        out.append(row)
    return {"since": since, "now": now, "items": out, "summary": summary, "total": len(items), "watch": WATCH_ON}


BURN_SPANS = {"1h": 3600, "5h": 5 * 3600, "24h": 24 * 3600}


def burn_view(span="5h"):
    """Who has been using the plan: each session's share of this host's token use over the span, by Claude Code's own counts."""
    if span not in BURN_SPANS:
        raise HerdrError("bad_request", "span must be 1h, 5h or 24h")
    if not WATCH_ON:
        return {"available": False, "reason": "The watch is switched off on this host (SHEPR_WATCH=0).", "rows": [], "span": span}
    if WATCH_FIXTURE:
        b = dict(_watch_fixture().get("burn") or {}, ready=True)
    else:
        b = WATCH.burn(BURN_SPANS[span])
    rows = []
    with LOCK:
        for r in b.get("rows") or []:
            ag = SID_AGENT.get(r.get("sid")) or {}
            row = {k: r.get(k) for k in ("share", "out", "ctx", "think", "replies", "model", "models", "helpers", "last")}
            row.update(label=ag.get("label") or os.path.basename(str(r.get("cwd") or "").rstrip("/")) or str(r.get("sid") or "")[:8],
                       title=one_line(str(r.get("title") or ""), 80), host=ag.get("host"), pane=ag.get("pane"), status=ag.get("status"),
                       open=bool(ag), sid=str(r.get("sid") or "")[:8])
            rows.append(row)
    out = {k: b.get(k) for k in ("others", "sessions", "replies", "out", "ctx", "series", "models", "now")}
    out.update(available=True, ready=bool(b.get("ready")), span=span, span_s=BURN_SPANS[span], rows=rows, host=LOCAL,
               note="Shares are estimated from Claude Code's own token counts on this host. Sessions on other machines are not counted.")
    return out


# ---- changes: what git says about the project a session runs in. Read only: status, log and diff, nothing else, with the
# hooks git could run on a read switched off. Only for a session on this machine (the files are here).
CHANGES_FILES = 200
DIFF_MAX = 160_000
GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "PAGER": "cat", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1"}


def _git(cwd, args, timeout=8, cap=600_000):
    """One read-only git command in `cwd`. (code, stdout text). GIT_OPTIONAL_LOCKS=0 keeps it from touching the index, so it can
    never collide with the agent's own git; fsmonitor and external diff programs are switched off, so reading runs nobody's code."""
    cmd = ["git", "-C", cwd, "-c", "core.fsmonitor=false", "-c", "core.quotepath=false", "-c", "color.ui=false", "--no-pager"] + list(args)
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, env=dict(os.environ, **GIT_ENV))
    except FileNotFoundError:
        raise HerdrError("no_git", "git is not installed on this host")
    except subprocess.TimeoutExpired:
        raise HerdrError("timeout", f"git took longer than {timeout}s in that folder")
    return r.returncode, r.stdout[:cap].decode("utf-8", "replace")


def _agent_dir(host, pane):
    if host["kind"] != "local":
        raise HerdrError("local_only", "changes can only be read for a session on the machine Shepr runs on: the files are there")
    if not PANE_RE.match(pane):
        raise HerdrError("bad_pane", "not a pane id")
    a = get_agent(host, pane)
    cwd = a.get("cwd") or a.get("foreground_cwd") or ""
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or not os.path.isdir(cwd):
        raise HerdrError("no_folder", "that session's folder is not on this machine")
    return cwd


def _status_rows(cwd):
    """(branch line, rows) from `git status --porcelain`. A row is {"path", "x", "y"} with the two status letters git prints."""
    code, out = _git(cwd, ["status", "--porcelain=v1", "--branch", "--untracked-files=all"])
    if code != 0:
        return None, []
    head, rows = "", []
    for line in out.split("\n"):
        if line.startswith("## "):
            head = line[3:]
        elif len(line) > 3:
            path = line[3:]
            if " -> " in path and line[0] in "RC":
                path = path.split(" -> ", 1)[1]
            if len(path) > 1 and path[0] == '"' and path[-1] == '"':
                path = path[1:-1]
            rows.append({"path": path, "x": line[0], "y": line[1]})
    return head, rows


def changes_view(host, pane):
    cwd = _agent_dir(host, pane)
    code, top = _git(cwd, ["rev-parse", "--show-toplevel"])
    if code != 0:
        return {"git": False, "folder": os.path.basename(cwd.rstrip("/")) or cwd, "at": time.time()}
    top = top.strip()
    head, rows = _status_rows(cwd)
    # "main...origin/main [ahead 1, behind 2]", "main", "HEAD (no branch)", "No commits yet on main"
    h, track = head or "?", ""
    mt = re.search(r" \[([^\]]+)\]$", h)
    if mt:
        track, h = mt.group(1), h[:mt.start()]
    if h.startswith("No commits yet on "):
        h = h[len("No commits yet on "):]
    branch, _, upstream = h.partition("...")
    upstream = upstream or None
    ahead = int((re.search(r"ahead (\d+)", track) or [0, 0])[1])
    behind = int((re.search(r"behind (\d+)", track) or [0, 0])[1])
    stat = {}
    code, out = _git(cwd, ["diff", "--numstat", "--no-ext-diff", "--no-textconv", "HEAD", "--"])
    if code != 0:   # no commit yet: compare with the empty index instead
        code, out = _git(cwd, ["diff", "--numstat", "--no-ext-diff", "--no-textconv", "--"])
    for line in out.split("\n"):
        parts = line.split("\t")
        if len(parts) == 3:
            path = parts[2].split(" => ")[-1].rstrip("}") if " => " in parts[2] and "{" not in parts[2] else parts[2]
            stat[path] = (None if parts[0] == "-" else int(parts[0]), None if parts[1] == "-" else int(parts[1]))
    files = []
    for r in rows[:CHANGES_FILES]:
        add, dele = stat.get(r["path"], (None, None))
        files.append({"path": r["path"], "x": r["x"], "y": r["y"], "add": add, "del": dele, "untracked": r["x"] == "?"})
    commits = []
    code, out = _git(cwd, ["log", "-12", "--format=%h%x1f%ct%x1f%an%x1f%s"])
    if code == 0:
        for i, line in enumerate(l for l in out.split("\n") if l):
            h, _, rest = line.partition("\x1f")
            ct, _, rest = rest.partition("\x1f")
            who, _, subj = rest.partition("\x1f")
            try:
                commits.append({"h": h[:12], "t": int(ct), "who": who[:40], "s": one_line(subj, 140), "unpushed": bool(upstream) and i < ahead})
            except ValueError:
                continue
    return {"git": True, "folder": os.path.basename(top.rstrip("/")), "branch": branch, "upstream": upstream, "ahead": ahead, "behind": behind,
            "detached": branch.startswith("HEAD"), "files": files, "more": max(0, len(rows) - CHANGES_FILES),
            "added": sum(f["add"] or 0 for f in files), "deleted": sum(f["del"] or 0 for f in files), "commits": commits, "at": time.time()}


def changes_diff(host, pane, path):
    """The diff of ONE file that git itself lists as changed in that session's project. A path git does not list is refused, so
    this can never be used to read an arbitrary file."""
    cwd = _agent_dir(host, pane)
    if not isinstance(path, str) or not path or len(path) > 1000 or "\x00" in path:
        raise HerdrError("bad_request", "which file?")
    _head, rows = _status_rows(cwd)
    row = next((r for r in rows if r["path"] == path), None)
    if not row:
        raise HerdrError("not_changed", "git does not list that file as changed any more")
    _code, top = _git(cwd, ["rev-parse", "--show-toplevel"])
    top = top.strip()
    if row["x"] == "?":   # untracked: there is nothing to compare with, so show the file itself (text only, from inside the project)
        full = os.path.realpath(os.path.join(top, path))
        if not (full == os.path.realpath(top) or full.startswith(os.path.realpath(top) + os.sep)) or not os.path.isfile(full):
            raise HerdrError("not_changed", "that file is not a plain file inside the project")
        with open(full, "rb") as f:
            data = f.read(DIFF_MAX + 1)
        if b"\x00" in data[:8000]:
            return {"path": path, "new": True, "binary": True, "text": "", "truncated": False}
        return {"path": path, "new": True, "binary": False, "truncated": len(data) > DIFF_MAX,
                "text": "\n".join("+" + l for l in clean_text(data[:DIFF_MAX].decode("utf-8", "replace")).split("\n"))}
    code, out = _git(top, ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "-U3", "HEAD", "--", path], cap=DIFF_MAX + 1)
    if code != 0:
        code, out = _git(top, ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "-U3", "--", path], cap=DIFF_MAX + 1)
    body = out.split("\n")
    start = next((i for i, l in enumerate(body) if l.startswith("@@")), None)
    binary = any(l.startswith("Binary files ") for l in body[:8])
    text = "" if binary or start is None else clean_text("\n".join(body[start:]))
    return {"path": path, "new": False, "binary": binary, "truncated": len(out) > DIFF_MAX, "text": text[:DIFF_MAX]}


def dialog_risk(text, menu):
    """A permission dialog shows the command it wants to run above its options. When an option approves, look at those lines the
    way the watch looks at commands that already ran, and say what is risky about them: {"level", "what"} or None."""
    if not menu or not any(o.get("approves") for o in menu.get("options") or []):
        return None
    lines = text.split("\n")
    first = next((i for i in range(len(lines) - 1, -1, -1) if OPT_RE.match(lines[i]) and OPT_RE.match(lines[i]).group(2) == "1"), None)
    if first is None:
        return None
    # Only the dialog itself is judged: up to the rule that opens it, or the dozen lines above the options when it has none.
    # What the agent SAID further up the screen ("I will not force push") is talk, not what is about to run.
    rule = set("─━═╭╮╰╯-_ ")
    top = next((i for i in range(first - 1, max(-1, first - 31), -1) if len(lines[i].strip()) >= 8 and set(lines[i].strip()) <= rule), None)
    top = top + 1 if top is not None else max(0, first - 12)
    best = None
    for l in lines[top:first]:
        hit = shepr_watch.risk_scan(l.strip().strip("│|").strip())
        if hit and (best is None or (hit[0] == "danger" and best[0] != "danger")):
            best = hit
    return {"level": best[0], "what": best[1]} if best else None


# --------------------------------------------------------------------------------------------
# photos and files from the phone: stored privately on THIS host, the path comes back, and the person sends it to an agent
# through the normal confirm sheet. Nothing is sent to an agent by uploading.
# --------------------------------------------------------------------------------------------
UPLOAD_MAX = 8 << 20                   # bytes per file
UPLOAD_KEEP_S = 7 * 86400              # uploads are deleted after a week
UPLOAD_DIR_CAP = 300 << 20             # and the folder never grows past this
TEXT_EXT = {"txt", "md", "log", "json", "csv"}


def upload_dir():
    return os.path.join(STATE_DIR, "uploads")


def sniff_upload(raw, ext):
    """What a file really is, from its first bytes (never from what the phone claims). None = not allowed."""
    if raw[:3] == b"\xff\xd8\xff":
        return "jpg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    if raw[:5] == b"%PDF-":
        return "pdf"
    if ext in TEXT_EXT and b"\x00" not in raw:
        try:
            raw.decode("utf-8")
            return ext
        except UnicodeDecodeError:
            return None
    return None


def prune_uploads():
    now, files = time.time(), []
    for root, _d, names in os.walk(upload_dir()):
        for n in names:
            p = os.path.join(root, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if now - st.st_mtime > UPLOAD_KEEP_S:
                try:
                    os.remove(p)
                except OSError:
                    pass
            else:
                files.append((st.st_mtime, st.st_size, p))
    total = sum(f[1] for f in files)
    for _mt, size, p in sorted(files):          # oldest first, until it fits
        if total <= UPLOAD_DIR_CAP:
            break
        try:
            os.remove(p); total -= size
        except OSError:
            pass


def save_upload(raw, ext, device):
    """Validate and store one upload. Returns {"path", "bytes", "kind"}."""
    if not 0 < len(raw) <= UPLOAD_MAX:
        raise HerdrError("too_big", f"files can be up to {UPLOAD_MAX >> 20} MB")
    kind = sniff_upload(raw, ext)
    if not kind:
        raise HerdrError("bad_type", "that is not a photo, a PDF, or a plain text file Shepr will accept")
    day = time.strftime("%Y-%m-%d")
    d = os.path.join(upload_dir(), day)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(upload_dir(), 0o700)
    path = os.path.join(d, secrets.token_hex(8) + "." + kind)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    prune_uploads()
    try:
        with open(state_path("sends.jsonl"), "a") as f:
            f.write(json.dumps({"t": int(time.time()), "kind": "upload", "device": device, "bytes": len(raw), "type": kind}) + "\n")
    except OSError:
        pass
    return {"ok": True, "path": path, "bytes": len(raw), "kind": kind}


# --------------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------------
STATIC_OK = re.compile(r"^/(?:|index\.html|app\.css|app\.js|sw\.js|manifest\.webmanifest|vendor/jsQR\.js|"
                       r"icons/(?:launch/)?[A-Za-z0-9_.-]+\.(?:png|svg))$")
CSP = ("default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
PUBLIC_API = {"/api/hello", "/api/pair"}


def learn_tailnet(quiet=False):
    """Who owns this node and what is its name. Used to refuse other tailnet users and DNS rebinding. True when both are known."""
    try:
        d = json.loads(subprocess.run(["tailscale", "status", "--json"], stdin=subprocess.DEVNULL,
                                      capture_output=True, text=True, timeout=6).stdout)
        me = d["Self"]
        dns = me["DNSName"].rstrip(".").lower()
        login = d["User"][str(me["UserID"])]["LoginName"].lower()
        if not dns or not login:
            raise ValueError("this node has no tailnet name yet")
        OWNER["dns"] = dns
        OWNER["login"] = OWNER["login"] or login   # a login already known (SHEPR_OWNER, an earlier answer) is never replaced
        OWNER["names"].add(dns)
        OWNER["ips"] = [ip for ip in (norm_ip(x) for x in (me.get("TailscaleIPs") or [])) if ip]   # what --tailnet listens on
    except Exception as e:
        if not quiet:
            print(f"shepr: tailscale identity unavailable ({e!r}); tailnet requests are refused until it is known (it is asked again every few seconds)", file=sys.stderr, flush=True)
    OWNER["login"] = (os.environ.get("SHEPR_OWNER") or OWNER["login"] or "").lower() or None
    for n in filter(None, os.environ.get("SHEPR_ALLOW_HOSTS", "").lower().split(",")):
        OWNER["names"].add(n.strip())
    return tailnet_known()


def tailnet_known():
    return bool(OWNER.get("dns") and OWNER.get("login"))


# The identity used to be asked for ONCE, at start. A server that started before Tailscale was connected (a boot race, a slow
# login, tailscaled restarted) then refused every phone with 421 for as long as it ran, and `shepr pair` printed a QR for
# 127.0.0.1. Now a quiet background loop asks again until it is known (never from inside a request: a hung `tailscale` must
# not hold a phone's request for seconds).
_tailnet = {"tried": 0.0, "port": None}
TAILNET_RETRY_S = 5.0


def relearn_tailnet():
    """Ask Tailscale again, at most once per TAILNET_RETRY_S. True when the identity is known afterwards."""
    with LOCK:
        if tailnet_known():
            return True
        now = time.time()
        if now - _tailnet["tried"] < TAILNET_RETRY_S:
            return False
        _tailnet["tried"] = now
    if learn_tailnet(quiet=True):
        print(f"shepr: tailscale identity learned: {OWNER['dns']} (owner={OWNER['login']})", file=sys.stderr, flush=True)
        if _tailnet["port"]:
            try:
                write_server_json(_tailnet["port"])   # `shepr pair` reads the URL from here: it must be the tailnet name now
            except OSError:
                pass
        return True
    return False


def tailnet_retry_loop():
    wait, tries = TAILNET_RETRY_S, 0
    while not tailnet_known():
        time.sleep(wait)
        relearn_tailnet()
        tries += 1
        if tries > 12:   # a minute of quick tries, then less and less often: a machine with Tailscale switched off is not hammered
            wait = min(300.0, wait * 1.5)


def write_server_json(port):
    """What `shepr pair` and `shepr status` read to find this server. Written only once the port is really ours."""
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    tmp = state_path("server.json.tmp")
    d = {"pid": os.getpid(), "port": port, "url": public_url(port), "started": int(time.time())}
    if TAILNET["on"]:   # the server takes tailnet connections itself: which addresses answer, and why not when they do not
        d["front"] = {"mode": "tailnet", "listening": list(TAILNET["listening"]), "dev": TAILNET["dev"], "dead": TAILNET["dead"],
                      "error": TAILNET["error"], "tls_until": TAILNET["tls_until"], "keepalive": KEEPALIVE}
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, state_path("server.json"))


def header_login(v):
    """The login `tailscale serve` put in Tailscale-User-Login. A name that is not plain ASCII arrives RFC 2047 encoded
    ("=?utf-8?q?...?="), and compared as it stands the owner of such a login was refused by their own server."""
    v = v or ""
    if v.startswith("=?"):
        try:
            v = str(email.header.make_header(email.header.decode_header(v)))
        except Exception:
            pass
    return v.strip().lower()


ALLOW_PROXY = os.environ.get("SHEPR_ALLOW_PROXY") == "1"   # your own reverse proxy in front, which decides who may connect


# --------------------------------------------------------------------------------------------
# serving the tailnet itself (`server.py PORT --tailnet`): nothing in front, no `tailscale serve`
# --------------------------------------------------------------------------------------------
# The server then listens on this node's own tailnet addresses as well as on loopback. Three things replace what
# `tailscale serve` used to do:
#   * the listener is tied to the Tailscale interface (SO_BINDTODEVICE), so a packet for that address that arrives over the
#     LAN finds nothing listening;
#   * it speaks TLS itself, with the certificate `tailscale cert` hands out for this node's name;
#   * it asks `tailscale whois` whose device is on the other end of every connection BEFORE it reads a byte from it. The
#     node's owner on one of their own devices gets a TLS handshake. A tagged device, another tailnet user or an address
#     Tailscale does not know is closed without a word.
# A header that names a user (Tailscale-User-Login) is `tailscale serve`'s to set. On a direct connection anyone could type
# it, so it is not read there.
TAILNET = {"on": False, "listening": [], "dev": None, "error": None, "tls_until": None, "dead": True}
KEEPALIVE = os.environ.get("SHEPR_KEEPALIVE", "1") != "0"   # a phone keeps its TLS connection between requests; 0: one request per connection
TAILNET_DEV = os.environ.get("SHEPR_TAILNET_DEV") or None   # name the interface instead of finding the one that holds the address
HANDSHAKE_SECS = 10      # the whole TLS handshake, however the bytes trickle
FIRST_SECS = 10          # a new connection must begin its first request within this
IDLE_SECS = 45           # a kept connection that stays silent this long is closed
KEEP_HINT_S = 30         # what the phone is told (Keep-Alive: timeout=): less than IDLE_SECS, so it lets go first and never sends into a closing connection
LINGER_SECS = 30         # an answer that went out before the request's body was read: the rest of that body is taken in for this long,
LINGER_IDLE = 5          # for this long without a byte of it,
LINGER_MAX = 2 * UPLOAD_MAX   # and for this many bytes at most. Then the connection is closed, whatever is still coming.
IDLE_SLOTS = threading.BoundedSemaphore(24)   # connections that may sit idle at once (of 48 in all); past that an answer says Connection: close
WHO_OK_S, WHO_NO_S, WHO_ERR_S = 120.0, 30.0, 3.0   # how long an answer from `tailscale whois` is believed: the owner, somebody else, no answer
CERT_EVERY_S = 12 * 3600.0   # Tailscale is asked for the certificate again this often; it renews it when it is close to running out
FRONT_TICK_S = 5.0
WHOIS_SECS = 4           # how long `tailscale whois` may take before the connection it is asked about is closed
SIOCGIFADDR = 0x8915     # Linux: the IPv4 address of an interface
WHO, WHO_LOCKS, REFUSED, WHO_LOCK = {}, {}, {}, threading.Lock()


class Refused(ConnectionError):
    """A tailnet connection that is not the owner on one of their own devices. Nothing was read from it, nothing is said to it."""


class ClientGone(ConnectionError):
    """The other end went away while it was being answered. There is no one left to tell."""


class TlsError(Exception):
    """The certificate could not be had, in words a person can act on."""


def norm_ip(s):
    try:
        return ipaddress.ip_address(str(s).split("%", 1)[0]).compressed
    except ValueError:
        return ""


def iface_addrs():
    """{address: interface} for this machine, straight from the kernel: an ioctl per interface for IPv4, /proc for IPv6.
    Linux only. Anywhere else it is empty, and a tailnet listener is then bound by address alone."""
    out = {}
    try:
        names = [n for _, n in socket.if_nameindex()]
    except OSError:
        return out
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for n in names:
            try:
                out[socket.inet_ntoa(fcntl.ioctl(s.fileno(), SIOCGIFADDR, struct.pack("256s", n[:15].encode()))[20:24])] = n
            except OSError:
                continue   # an interface with no IPv4 address
    finally:
        s.close()
    try:
        with open("/proc/net/if_inet6") as f:
            for line in f:
                p = line.split()
                if len(p) == 6:
                    out[socket.inet_ntop(socket.AF_INET6, bytes.fromhex(p[0]))] = p[5]
    except (OSError, ValueError):
        pass
    return out


def tcp_shutdown(sock, how):
    """Shut the TCP connection under a socket, TLS or plain. Not sock.shutdown(): on a TLS socket that drops the TLS state
    first, and anything another thread wrote in that instant would go out unencrypted."""
    socket.socket.shutdown(sock, how)


def ts_whois(ip):
    """Who Tailscale says is at a tailnet address: {"login", "node", "tagged"}. None when it knows no such peer. Raises
    OSError, ValueError or subprocess.TimeoutExpired when it cannot be asked."""
    r = subprocess.run(["tailscale", "whois", "--json", ip], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=WHOIS_SECS)
    if r.returncode != 0:
        return None
    w = json.loads(r.stdout)
    node, prof = (w.get("Node"), w.get("UserProfile")) if isinstance(w, dict) else (None, None)
    if not isinstance(node, dict) or not isinstance(prof, dict):
        raise ValueError("whois answered in a shape Shepr does not know")   # nobody is let in on a guess
    login = str(prof.get("LoginName") or "").strip().lower()
    name = re.sub(r"[^\w.@ -]", "", str(node.get("ComputedName") or node.get("Name") or ""))[:64]   # it goes into the journal: no line breaks, no escapes
    return {"login": login, "node": name, "tagged": bool(node.get("Tags")) or login == "tagged-devices"}


def owner_login():
    """The login this server answers to, or None. "tagged-devices" is what Tailscale calls the user of a node that has none:
    it is never an owner, or every tagged device on the tailnet would be one."""
    o = OWNER.get("login")
    return o if o and o != "tagged-devices" else None


def is_owner(who):
    return bool(who and not who["tagged"] and owner_login() and who["login"] == owner_login())


def whois_cached(ip):
    """ts_whois, remembered for a while, and asked once at a time per address (a phone opens six connections at once)."""
    now = time.monotonic()
    with WHO_LOCK:
        hit = WHO.get(ip)
        if hit and hit[0] > now:
            return hit[1]
        if len(WHO) > 512:   # never more than the tailnet has nodes, in practice
            WHO.clear(); WHO_LOCKS.clear()
        one = WHO_LOCKS.setdefault(ip, threading.Lock())
    with one:
        with WHO_LOCK:
            hit = WHO.get(ip)
            if hit and hit[0] > time.monotonic():
                return hit[1]
        try:
            who = ts_whois(ip)
            ttl = WHO_ERR_S if who is None else (WHO_OK_S if is_owner(who) else WHO_NO_S)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            who, ttl = None, WHO_ERR_S
        with WHO_LOCK:
            WHO[ip] = (time.monotonic() + ttl, who)
        return who


def admit(ip):
    """(who, None) for the owner on one of their own devices. (None, why) for everybody else."""
    if not owner_login():
        return None, "this server does not know its owner yet"
    who = whois_cached(ip)
    if not who:
        return None, "Tailscale knows no such peer"
    if who["tagged"]:
        return None, f"{who['node'] or 'that node'} is a tagged device, which has no user"
    if who["login"] != owner_login():
        return None, f"{who['node'] or 'that node'} belongs to another tailnet user"
    return who, None


def note_refused(ip, why):
    """One journal line per address every ten minutes: enough to see who knocked, never a flood."""
    now = time.monotonic()
    with WHO_LOCK:
        if now - REFUSED.get(ip, -1e9) < 600:
            return
        if len(REFUSED) > 512:
            REFUSED.clear()
        REFUSED[ip] = now
    print(f"shepr: refused a tailnet connection from {ip}: {why}", file=sys.stderr, flush=True)


def serve_in_front(port):
    """True when `tailscale serve` is set up for this port on this node, False when it is not, None when that cannot be told.
    tailscaled takes tailnet traffic for a port it serves before the kernel ever sees it. With such an entry left behind,
    phones would still go through it (and reach the loopback listener), whatever is bound here."""
    try:
        r = subprocess.run(["tailscale", "serve", "status", "--json"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=6)
        d = json.loads(r.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    tcp = d.get("TCP") if isinstance(d, dict) else None
    return str(port) in tcp if isinstance(tcp, dict) else False


def declared_body(headers):
    """The length of a request's body as its headers declare it, or None when they do not declare it plainly (chunked, two
    lengths, not a number). A connection is only reused after a request whose every byte was read, so None means: close."""
    if headers.get("Transfer-Encoding"):
        return None
    cl = headers.get_all("Content-Length") or []
    if not cl:
        return 0
    if len(cl) != 1 or not re.fullmatch(r"[0-9]{1,12}", cl[0].strip()):
        return None
    return int(cl[0])


def cert_not_after(pem):
    """When the first certificate in a PEM text runs out (seconds since 1970), or None. A walk over its DER by hand: the
    standard library only reads that date off a live connection."""
    m = re.search(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", pem or "", re.S)
    if not m:
        return None
    try:
        der = ssl.PEM_cert_to_DER_cert(m.group(0))

        def tlv(i):   # one element at i: its tag, where its value starts, where it ends
            tag, n, i = der[i], der[i + 1], i + 2
            if n & 0x80:
                k = n & 0x7F
                n, i = int.from_bytes(der[i:i + k], "big"), i + k
            return tag, i, i + n
        _, i, _ = tlv(0)             # Certificate
        _, i, _ = tlv(i)             # tbsCertificate
        tag, s, e = tlv(i)
        if tag == 0xA0:              # [0] version; the serial number follows it
            tag, s, e = tlv(e)
        for _ in range(3):           # over the serial number, the signature algorithm and the issuer: now at validity
            tag, s, e = tlv(e)
        _, _, nb_end = tlv(s)        # notBefore
        tag, a, b = tlv(nb_end)      # notAfter
        t = der[a:b].decode("ascii")
        if tag == 0x17:              # UTCTime has a two-digit year
            t = ("20" if t[:2] < "50" else "19") + t
        return calendar.timegm(time.strptime(t[:14], "%Y%m%d%H%M%S"))
    except Exception:
        return None


def tls_files(dns):
    d = state_path("tls")
    return d, os.path.join(d, dns + ".crt"), os.path.join(d, dns + ".key")


def make_tls(crt, key):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(crt, key)   # also proves the key belongs to the certificate
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


def read_text(path):
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def saved_cert(dns):
    """(TLS context, when it runs out) from the certificate an earlier run kept, or None. It lets a server that starts before
    Tailscale is answering (a boot, a tailscaled restart) open its door at once instead of waiting to ask."""
    _, crt, key = tls_files(dns)
    until = cert_not_after(read_text(crt))
    if not until or until < time.time() + 60:
        return None
    try:
        return make_tls(crt, key), until
    except (OSError, ssl.SSLError):
        return None


def fetch_cert(dns):
    """Ask Tailscale for this node's certificate and key and keep them in the state directory, owner-only. Returns
    (TLS context, when it runs out). tailscaled holds the certificate and renews it when it is close to running out, so asking
    again twice a day is how it stays fresh here with nothing restarting. The key is never printed, logged or sent anywhere.
    Raises TlsError."""
    d, crt, key = tls_files(dns)
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
    except OSError as e:
        raise TlsError(f"cannot make {d} ({e.strerror or e})")
    new_c, new_k = crt + ".new", key + ".new"
    for p in (new_c, new_k):
        with contextlib.suppress(OSError):
            os.remove(p)
    try:
        r = subprocess.run(["tailscale", "cert", "--cert-file", new_c, "--key-file", new_k, dns], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=180)
    except FileNotFoundError:
        raise TlsError("the tailscale command is not installed here")
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TlsError(f"`tailscale cert` did not answer ({e.__class__.__name__})")
    if r.returncode != 0 or not (os.path.isfile(new_c) and os.path.isfile(new_k)):
        said = [l.strip() for l in (r.stderr or r.stdout or "").splitlines() if l.strip()]
        msg = CTRL_RE.sub("", said[-1])[:200] if said else f"exit {r.returncode}"
        low, fix = msg.lower(), ""
        if "denied" in low or "must be root" in low:
            fix = " Fix: sudo tailscale set --operator=$USER"
        elif "https" in low:
            fix = " Fix: turn on HTTPS certificates for your tailnet (the DNS page of the Tailscale admin console)"
        raise TlsError(f"`tailscale cert {dns}` failed: {msg.rstrip('.')}.{fix}")
    try:
        os.chmod(new_k, 0o600)
        ctx = make_tls(new_c, new_k)   # before it replaces the pair in use
    except (OSError, ssl.SSLError) as e:
        raise TlsError(f"the certificate Tailscale returned could not be loaded ({e.__class__.__name__})")
    until = cert_not_after(read_text(new_c))
    os.replace(new_k, key)
    os.replace(new_c, crt)
    return ctx, until


# Whole-request deadlines. A socket timeout alone is not enough: it restarts on every receive, so a caller who sends one
# byte every few seconds never trips it and can hold a connection (and one of the 48 slots) for days. These are wall-clock
# limits for the whole of one phase, enforced by closing the socket from a timer, so a blocked read is cut off however
# slowly the bytes arrive. A phone sends its request in one burst; nothing honest comes near them.
HEADER_SECS = 5      # request line and every header, counted from accept
BODY_SECS = 10       # a JSON body (at most 16 KB)
UPLOAD_SECS = 120    # a photo or file (at most UPLOAD_MAX), from a phone on a slow link
BODY_SLOTS = threading.BoundedSemaphore(16)   # bodies being read before their signature is checked, all callers together


class Handler(SimpleHTTPRequestHandler):
    server_version = "shepr"
    timeout = 20   # a client that stops reading our answer, or sits idle, is dropped instead of holding a thread forever
    # An answer is its headers and then its body. Sent as two small writes, the second waits for the phone to acknowledge the
    # first (Nagle), and the phone waits before acknowledging (delayed ACK): measured from a second machine, 110 ms for a
    # request that needed 35. So: no Nagle, and a JSON answer leaves in one write (flush_headers).
    disable_nagle_algorithm = True

    def setup(self):
        self._dog, self._cut, self._unread, self._took, self._parsed, self._n, self._slot = None, False, 0, 0, False, 0, False
        self._who = self._ip = None
        self._tailnet = bool(getattr(self.server, "tailnet", False))
        if self._tailnet:
            # Whose device is this? Tailscale is asked before a byte is read from the connection, and before any TLS.
            ip = self._ip = norm_ip(self.client_address[0])
            who, why = admit(ip)
            ctx = self.server.front.ctx
            if not who or ctx is None:
                note_refused(ip, why or "there is no certificate to answer with yet")
                raise Refused(why or "no certificate")
            self._who = who
            self.request.settimeout(HANDSHAKE_SECS)   # one deadline for the whole handshake, however it trickles
            self.request = ctx.wrap_socket(self.request, server_side=True)
            if KEEPALIVE:
                self.protocol_version = "HTTP/1.1"
        super().setup()
        if not self._tailnet:
            self.arm(HEADER_SECS)   # a tailnet connection's clock starts when its request does (handle_one_request)

    def arm(self, secs):
        """Close this connection if the current phase has not finished in `secs` seconds, however the bytes trickle."""
        self.disarm()
        sock = self.connection

        def fire():
            self._cut = True
            try:
                tcp_shutdown(sock, socket.SHUT_RDWR)   # wakes a read that is blocked in recv
            except OSError:
                pass
        self._dog = threading.Timer(secs, fire)
        self._dog.daemon = True
        self._dog.start()

    def disarm(self):
        if self._dog:
            self._dog.cancel()
            self._dog = None

    def finish(self):
        self.disarm()
        # An answer sent before the body was read (a 401 for an unknown phone, say) leaves those bytes in the socket,
        # and closing on unread data resets the connection, which can swallow the answer. So say we are done writing,
        # then take what the caller already sent, for a couple of seconds at most.
        if self._unread > 0 and not self._cut:
            try:
                self.wfile.flush()
                tcp_shutdown(self.connection, socket.SHUT_WR)
                if self._tailnet:
                    self.linger()
                else:
                    self.arm(2)
                    self.rfile.read(min(self._unread, 16384))
            except (OSError, ValueError):
                pass
            self.disarm()
        if self._slot:   # an answer promised to keep the connection and something then went wrong: the place is given back
            self._slot = False
            IDLE_SLOTS.release()
        super().finish()
        if self._tailnet:   # the TLS socket is this handler's: the server only holds the plain one it accepted
            with contextlib.suppress(OSError):
                self.connection.close()

    def linger(self):
        """An answer went out before the request's body was read (a refusal). Over HTTP/1.1 a browser reads the answer only
        once it has sent the whole request, and a connection closed under an upload is reset, which loses the answer: the
        phone would say it cannot reach the host where the host said "this phone is not paired". So what is left of the
        body is taken in and thrown away, within limits. (`tailscale serve` hid this: it spoke HTTP/2 to the phone, which
        can tell a sender to stop.)"""
        left = min(self._unread, LINGER_MAX)
        self.arm(LINGER_SECS)
        self.connection.settimeout(LINGER_IDLE)
        while left > 0:
            got = self.rfile.read1(min(left, 65536))
            if not got:
                break
            left -= len(got)

    def parse_request(self):
        ok = super().parse_request()
        self.disarm()
        if self._cut:   # the headers were cut off by the deadline: half a request is not answered
            self.close_connection = True
            return False
        self._parsed = bool(ok)
        return ok

    def await_request(self):
        """Wait, saying nothing, for the next request on a tailnet connection. False when there will be none: the phone
        closed it, or it sat silent long enough. No journal line either way: this is how every connection ends."""
        first = self._n == 0
        if first:
            self._slot = IDLE_SLOTS.acquire(blocking=False)
        elif not self._slot:
            return False   # the last answer already said Connection: close
        secs = IDLE_SECS if not first else (FIRST_SECS if self._slot else HEADER_SECS)
        try:
            self.connection.settimeout(secs)
            return bool(self.rfile.peek(1))
        except (OSError, ValueError):
            return False
        finally:
            if self._slot:
                self._slot = False
                IDLE_SLOTS.release()
            with contextlib.suppress(OSError):
                self.connection.settimeout(self.timeout)

    def reusable(self):
        """May this connection carry another request after the one being answered? Only when every byte of this one was
        read. What is left of an unread body would be taken for the next request, and whoever can make the phone's
        browser send a POST here (any web page can) could put a whole request of their own choosing inside it."""
        if not (self._tailnet and self._parsed) or self._cut or self.close_connection:
            return False
        n = declared_body(self.headers)
        return n is not None and self._took == n

    def handle_one_request(self):
        if not self._tailnet:
            return super().handle_one_request()   # loopback: one request per connection, timed from accept, as it always was
        if not self.await_request():
            self.close_connection = True
            return
        if self._n:
            # A kept connection: is it still the owner's device? A phone that keeps asking could otherwise hold one open for
            # hours past the day its node was tagged or handed to somebody else. The answer is remembered for two minutes,
            # so Tailscale itself is rarely asked.
            who, why = admit(self._ip)
            if not who:
                note_refused(self._ip, why)
                self.close_connection = True
                return
        self._unread, self._took, self._parsed = 0, 0, False
        self.arm(HEADER_SECS)
        try:
            super().handle_one_request()
        finally:
            self.disarm()
        self._n += 1
        if not self.reusable():
            self.close_connection = True

    def log_message(self, fmt, *args):
        if len(args) < 2 or str(args[1]) not in ("200", "304"):   # a one-argument message (a timeout) has no status to look at
            super().log_message(fmt, *args)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        # Camera is the one device feature this app asks for (scanning the pairing QR).
        self.send_header("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
        # Static files: always revalidate (they carry Last-Modified, so this is cheap) so Safari's own cache
        # never serves an old app.js that the service worker's network-first rule would have refreshed.
        buf = getattr(self, "_headers_buffer", [])
        if not any(h.lower().startswith(b"cache-control") for h in buf):
            self.send_header("Cache-Control", "no-cache")
        # A tailnet connection is kept for the next request only when this one was read to its last byte and there is room
        # among the idle ones. The answer says which, so the phone never sends its next request into a closing connection.
        if (getattr(self, "_tailnet", False) and self.protocol_version >= "HTTP/1.1" and buf and b" 100 " not in buf[0][:16]
                and not any(h.lower().startswith(b"connection:") for h in buf)):
            if self.reusable() and (self._slot or IDLE_SLOTS.acquire(blocking=False)):
                self._slot = True
                self.send_header("Connection", "keep-alive")
                self.send_header("Keep-Alive", f"timeout={KEEP_HINT_S}")
            else:
                self.send_header("Connection", "close")
        super().end_headers()

    def flush_headers(self):
        if hasattr(self, "_headers_buffer"):
            self.wfile.write(b"".join(self._headers_buffer) + self.__dict__.pop("_tail", b""))   # headers and a waiting body, one write
            self._headers_buffer = []

    def json_out(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._tail = body   # leaves with the headers, in the same write
            self.end_headers()
            rest = self.__dict__.pop("_tail", None)
            if rest:   # an HTTP/0.9 request gets no headers, so nothing carried the body out
                self.wfile.write(rest)
        except OSError as e:   # the phone went away mid-answer (a reset, its TLS torn down, it stopped reading)
            self.close_connection = True
            raise ClientGone(e.__class__.__name__) from None
        finally:
            self.__dict__.pop("_tail", None)

    def network_gate(self, post):
        """Layer 1: is this request even from the right place? Returns the tailnet login or 'local'."""
        raw = self.headers.get("Host") or ""
        host = "[::1]" if raw.startswith("[") else raw.rsplit(":", 1)[0].lower()
        # A connection this server took from the tailnet itself (--tailnet): `tailscale whois` already said, before anything
        # was read, that it is the owner on one of their own devices. It is addressed by the tailnet name, never by a
        # loopback one.
        direct = getattr(self, "_who", None)
        if host not in (OWNER["names"] - LOOPBACK_NAMES if direct else OWNER["names"]):
            self.json_out(421, {"error": "bad_host", "message": "unexpected Host header" if tailnet_known() else
                                "this host does not know its tailnet name yet (is Tailscale connected on it?)"}); return None
        if self.headers.get("Tailscale-Funnel-Request"):
            self.json_out(403, {"error": "funnel", "message": "public funnel traffic is refused"}); return None
        if direct:
            login = direct["login"]   # Tailscale-User-Login is not read here: nothing but the caller could have set it
        else:
            login = header_login(self.headers.get("Tailscale-User-Login"))
            if login and login != OWNER["login"]:
                self.json_out(403, {"error": "not_owner", "message": "this app answers only to the node's owner"}); return None
        # `tailscale serve` names the user of every request it forwards, except for a node that HAS no user: a tagged
        # device. Such a request arrives with the proxy's X-Forwarded headers and no login, and used to be taken for a
        # process on this machine. Only a direct loopback caller has neither. On a direct tailnet connection the same
        # headers mean a proxy on one of the owner's own devices is passing somebody else's request along.
        if (direct or not login) and not ALLOW_PROXY and (self.headers.get("X-Forwarded-For") or self.headers.get("X-Forwarded-Host")):
            self.json_out(403, {"error": "not_owner", "message": "this request was passed along by a proxy on one of your own devices (see SHEPR_ALLOW_PROXY)"
                                if direct else "this request came through a proxy with no tailnet user on it "
                                "(a tagged device, or a reverse proxy of your own: see SHEPR_ALLOW_PROXY)"}); return None
        if post:
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc.lower() != raw.lower():
                self.json_out(403, {"error": "origin", "message": "cross-origin write refused"}); return None
            want = "application/octet-stream" if self.path.startswith("/api/upload") else "application/json"
            if self.headers.get("X-Shepr") != "1" or not (self.headers.get("Content-Type") or "").startswith(want):
                self.json_out(400, {"error": "bad_request", "message": "JSON with X-Shepr: 1 required"}); return None
        return login or "local"

    def api(self, post):
        net = self.network_gate(post)
        if net is None:
            return
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            raw = b""
            if post:
                if u.path != "/api/pair":   # pairing is the one write a not-yet-paired phone may make
                    precheck_request(self.headers)   # anyone else is turned away before a byte of body is read
                n = int(self.headers.get("Content-Length") or 0)
                if not 0 < n <= (UPLOAD_MAX if u.path == "/api/upload" else 16384):
                    raise HerdrError("bad_request", "body missing or too large")
                if not BODY_SLOTS.acquire(blocking=False):
                    raise HerdrError("slow_down", "too many requests are being received at once; try again in a moment")
                self.arm(UPLOAD_SECS if u.path == "/api/upload" else BODY_SECS)
                try:
                    raw = self.rfile.read(n)
                except OSError:   # a reset, a stall past the socket's own timeout, TLS torn down: the same as a short read
                    raw = b""
                finally:
                    self.disarm()
                    BODY_SLOTS.release()
                if self._cut or len(raw) < n:   # too slow, or the sender went away: there is no one to answer
                    self.close_connection = True
                    return
                self._unread, self._took = 0, n
            body = json.loads(raw) if post and u.path != "/api/upload" else {}
            if post and not isinstance(body, dict):
                raise HerdrError("bad_request", "body must be an object")

            if u.path == "/api/hello" and not post:
                # The one request every launch makes first: worth a journal line, it is how you see a phone arrive.
                via = f" on {self._who['node']}" if getattr(self, "_who", None) and self._who["node"] else ""   # which of the owner's devices, when Tailscale said
                print(f"shepr: hello from {net}{via} build={(self.headers.get('X-Shepr-Build') or '?')[:12]} ({(self.headers.get('User-Agent') or '')[:70]})", file=sys.stderr, flush=True)
                return self.json_out(200, {"app": "shepr", "v": 1, "name": LOCAL, "t": time.time(),
                                           "paired": bool(devices()), "writes": not READONLY, "build": shell_build(), "server_build": BOOT_BUILD})
            if u.path == "/api/pair" and post:
                return self.json_out(200, do_pair(body, self.headers.get("User-Agent")))

            viewer = verify_request(self.headers, "POST" if post else "GET", self.path, raw)
            vname = devices().get(viewer, {}).get("name", viewer)

            if not post and u.path == "/api/queue":
                if q.get("fresh") == "1" and time.time() - SNAP["ts"] > 2:
                    poll_all()
                out = build_queue()
                out["viewer"], out["host"] = vname, LOCAL
                last = PUSH_STATUS.get(viewer)
                if last and time.time() * 1000 - last["at"] < 120_000:   # the notification worker asks right after a push: tell it what the push was for
                    out["pushed"] = {"why": last["why"], "at": last["at"]}
                with LOCK:
                    out["push_on"] = viewer in PUSH
                return self.json_out(200, out)
            if not post and u.path == "/api/pane":
                host = need_host(q.get("host", ""))
                pane = q.get("pane", "")
                if not PANE_RE.match(pane):
                    raise HerdrError("bad_pane", "not a pane id")
                return self.json_out(200, pane_view(host, pane, max(5, min(200, int(q.get("lines", "40"))))))
            if not post and u.path == "/api/history":
                host = need_host(q.get("host", ""))
                pane = q.get("pane", "")
                if not PANE_RE.match(pane):
                    raise HerdrError("bad_pane", "not a pane id")
                a = get_agent(host, pane)
                tf = transcript_file(a) if host["kind"] == "local" else None
                if tf:
                    try:
                        before = int(q["before"]) if q.get("before") not in (None, "") else None
                    except ValueError:
                        raise HerdrError("bad_request", "before must be a number")
                    entries, nxt, size, note = history_page(tf, before)
                    return self.json_out(200, {"source": "transcript", "entries": entries, "before": nxt, "size": size,
                                               "note": note, "at": time.time()})
                # No transcript to read (another host, or an agent that is not Claude): the most herdr keeps.
                text = read_tail(host, pane, 1000, max_bytes=600 * 1024)
                return self.json_out(200, {"source": "pane", "text": text, "entries": [], "before": None, "at": time.time(),
                                           "note": "the full history is only available for Claude sessions on this host; "
                                                   "this is the most herdr keeps (about 1,000 lines)"})
            if not post and u.path == "/api/asleep":
                return self.json_out(200, asleep_view())
            if not post and u.path == "/api/folders":
                return self.json_out(200, folders_view(need_host(q.get("host") or LOCAL)))
            if not post and u.path == "/api/commands":
                return self.json_out(200, commands_view(need_host(q.get("host", ""))))
            if not post and u.path == "/api/hosts":
                out = hosts_view()
                out["devices"] = [dict(dev_public(i, d), you=(i == viewer)) for i, d in devices().items()]
                out["writes"] = not READONLY
                return self.json_out(200, out)
            if post and u.path == "/api/hosts/scan":
                return self.json_out(200, {"started": start_scan()})
            if u.path == "/api/push/config" and not post:
                return self.json_out(200, push_config(viewer))
            if post and u.path == "/api/push/subscribe":
                return self.json_out(200, push_subscribe(viewer, body))
            if post and u.path == "/api/push/prefs":
                return self.json_out(200, push_set_prefs(viewer, body))
            if post and u.path == "/api/push/unsubscribe":
                forget_push(viewer)
                return self.json_out(200, push_config(viewer))
            if post and u.path == "/api/push/test":
                code = send_push(viewer)
                if code is None:
                    raise HerdrError("not_subscribed", "this phone is not subscribed, or its push address is not allowed")
                return self.json_out(200, {"ok": 200 <= code < 300, "status": code})
            if not post and u.path == "/api/live/status":
                return self.json_out(200, {"status": build_live_status(), "push": LIVE.push_info(viewer), "host": LOCAL})
            if post and u.path == "/api/live/register":
                return self.json_out(200, live_call(lambda: LIVE.register(viewer, body)))
            if post and u.path == "/api/live/unregister":
                return self.json_out(200, live_call(lambda: LIVE.unregister(viewer, body.get("id") if isinstance(body.get("id"), str) else None)))
            if post and u.path == "/api/live/test":
                res = live_call(lambda: LIVE.test(viewer))
                return self.json_out(200, {"ok": bool(res) and all(r["code"] == 200 for r in res), "results": res})
            if not post and u.path == "/api/budget":
                return self.json_out(200, budget_view())
            if not post and u.path == "/api/activity":
                return self.json_out(200, activity_view(q.get("since"), int(q.get("limit", ACTIVITY_MAX))))
            if not post and u.path == "/api/burn":
                return self.json_out(200, burn_view(q.get("span", "5h")))
            if not post and u.path == "/api/changes":
                return self.json_out(200, changes_view(need_host(q.get("host", "")), q.get("pane", "")))
            if not post and u.path == "/api/changes/diff":
                return self.json_out(200, changes_diff(need_host(q.get("host", "")), q.get("pane", ""), q.get("path", "")))
            if not post and u.path == "/api/ledger":
                return self.json_out(200, ledger_view(agents=build_queue()["agents"]))
            if post and u.path == "/api/upload":
                if need_host(q.get("host", ""))["kind"] != "local":
                    raise HerdrError("remote_unsupported", "photos and files can only be sent to sessions on this host: the file is stored here")
                return self.json_out(200, save_upload(raw, re.sub(r"[^a-z]", "", q.get("ext", "").lower())[:5], viewer))
            if post and u.path == "/api/send/prepare":
                return self.json_out(200, build_plan(body))
            if post and u.path == "/api/send/commit":
                if READONLY:
                    raise HerdrError("read_only", "read-only server")
                return self.json_out(200, commit_plan(body.get("nonce")))
            return self.json_out(404, {"error": "not_found", "message": u.path})
        except HerdrError as e:
            return self.json_out(http_status(e.code), {"error": e.code, "message": e.message})
        except (BrokenPipeError, ConnectionResetError, ClientGone):
            raise   # the phone went away mid-answer: there is no one to tell
        except Exception as e:
            # A commit that fails half way may have typed something: say so, whatever the failure was. Everything else that is
            # bad input stays a 400. Anything unforeseen is answered as a 500: a dropped connection reads as "offline" on the
            # phone, which sent people to check Tailscale for what was a bug on the host.
            committing = post and u.path == "/api/send/commit"
            if isinstance(e, RecursionError):   # a body nested thousands deep: bad input, and not worth a traceback
                return self.json_out(400, {"error": "bad_request", "message": "that body is nested too deeply"})
            if isinstance(e, (ValueError, KeyError)) and not committing:
                return self.json_out(400, {"error": "bad_request", "message": str(e)})
            print(f"shepr: internal error on {'POST' if post else 'GET'} {u.path}: {e!r}", file=sys.stderr, flush=True)
            traceback.print_exc(file=sys.stderr)
            msg = "Shepr hit an internal error on the host (the details are in its journal)."
            if committing:
                msg += " A failed write may still have landed: read the pane before trying again."
            return self.json_out(500, {"error": "internal", "message": msg})

    def do_GET(self):
        if urlsplit(self.path).path.startswith("/api/"):
            return self.api(False)
        super().do_GET()

    def do_HEAD(self):
        super().do_HEAD()

    def do_POST(self):
        try:
            self._unread = max(0, int(self.headers.get("Content-Length") or 0))   # api() zeroes it once the body is read
        except ValueError:
            self._unread = 0
        if urlsplit(self.path).path.startswith("/api/"):
            return self.api(True)
        self.send_error(405)

    def do_OPTIONS(self):
        # Never answer a CORS preflight: a cross-origin page must not be able to reach /api.
        self.send_error(405)

    def send_head(self):
        path = unquote(urlsplit(self.path).path)

        # WHY: app.js asks /ping to find out whether home is reachable. A bare static
        # server 404s it, and the app then shows "can't reach home" on its very first run.
        # no-store because a cached 200 would tell the user a dead host is alive.
        if path == "/ping":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return io.BytesIO(body)

        # WHY: the template 404s dotfiles and itself; this app is stricter and serves an allowlist,
        # because the folder also holds tests, docs and the server source, and /api can send keys.
        if not STATIC_OK.match(path):
            self.send_error(404)
            return None
        return super().send_head()


# --------------------------------------------------------------------------------------------
# command line: `shepr pair | devices | revoke`, and (default) serve
# --------------------------------------------------------------------------------------------
def _service_runs(text, me):
    """True when a unit file starts this file. The path may be a symlink and need not be named server.py."""
    if me in text:
        return True
    for token in text.split():
        if not (token.startswith("/") or token.startswith("~")):
            continue
        try:
            if os.path.realpath(os.path.expanduser(token)) == me:
                return True
        except OSError:
            continue
    return False


def _unit_dir():
    return os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")


def unit_names():
    """The user units on this machine that run THIS server.py (nixweb names one after the session directory, the README writes shepr.service)."""
    me = os.path.realpath(__file__)
    unit_dir = _unit_dir()
    names = []
    try:
        entries = sorted(os.listdir(unit_dir))
    except OSError:
        entries = []
    for name in entries:
        if not name.endswith(".service"):
            continue
        path = os.path.join(unit_dir, name)
        try:
            with open(path, errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        if _service_runs(text, me):
            names.append(name[:-len(".service")])
    return names


def unit_hint():
    """How to start the server on this machine.

    A user unit counts only when it runs this server.py. With none installed, both names are examples.
    """
    names = unit_names()
    if len(names) == 1:
        return f"systemctl --user start {names[0]}"
    if names:
        return " or ".join(f"systemctl --user start {n}" for n in names)
    return ("the unit you installed (the README writes shepr.service; "
            "nixweb names its unit nixweb-<session>)")


# What `tailscale status --json` calls a node that is not connected (checked on tailscale 1.104.7 against a daemon that had never
# logged in: BackendState "NeedsLogin", exit 0; `tailscale serve` then exits 1 with "Logged out."; `tailscale wait` runs out its
# whole timeout and exits 1). "Stopped" is what `tailscale down` leaves behind.
TAILSCALE_STATES = {
    "Stopped": ("Tailscale is turned off on this machine", "tailscale up"),
    "NeedsLogin": ("Tailscale is logged out on this machine", "tailscale up"),
    "NeedsMachineAuth": ("this machine is waiting for approval in the Tailscale admin console", "approve it there"),
    "InUseOtherUser": ("Tailscale is in use by another user on this machine", "tailscale switch, or ask that user"),
    "Starting": ("Tailscale is still connecting", "wait a few seconds and try again"),
    "NoState": ("Tailscale is still connecting", "wait a few seconds and try again"),
}


def tailscale_problem():
    """None when Tailscale is connected, or when that cannot be told. Otherwise (what is wrong, how to fix it) in plain words.
    Shepr only answers over the tailnet, so its unit waits for Tailscale and then fails without it. That failure used to look
    like "the server is not running, start it", which only starts the same wait again. Run only when the server is already
    down: the bar widget polls `shepr status`, and a running server needs no answer from Tailscale."""
    try:
        r = subprocess.run(["tailscale", "status", "--json"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=6)
    except FileNotFoundError:
        return "the tailscale command is not installed here", "install Tailscale, then run: tailscale up"
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        d = json.loads(r.stdout)
    except ValueError:
        # The CLI could not reach the daemon at all (a stopped tailscaled prints its error and exits 1, with no JSON).
        return "the Tailscale service (tailscaled) is not running", "sudo systemctl enable --now tailscaled"
    state = d.get("BackendState") if isinstance(d, dict) else None
    if state == "Running" or not isinstance(state, str):
        return None
    return TAILSCALE_STATES.get(state) or (f"Tailscale is not connected (state {state})", "tailscale up")


# Lines in a unit's journal that say nothing about WHY it is failing: systemd's own bookkeeping, sudo's, and this server's routine news.
UNIT_NOISE = re.compile(r"^(?:Start(?:ed|ing) |Stopp(?:ed|ing) |Consumed |pam_unix|\S+ : .*COMMAND=|.*Scheduled restart job|.*Failed with result|"
                        r".*Control process exited|.*Main process exited|.*Failed to start |.*: Start request repeated|"
                        r"shepr: (?:serving|hello|send|tailnet scan|push|paired|tailscale identity learned|refused a tailnet connection) |"
                        r"[0-9a-f:.]+ - - \[)")


def unit_trouble(names=None):
    """Why a unit that should be running is not, from systemd's own record: {"unit", "state", "restarts", "last", "fix"}, or None
    when no unit is failing (one that is simply stopped is not failing) or systemd cannot be asked. On 2026-11-01 a unit failed
    33,662 times in 49 hours on one line of `tailscale serve` output, and `shepr status` said only "not running. Start it"."""
    for n in (unit_names() if names is None else names):
        try:
            r = subprocess.run(["systemctl", "--user", "show", n + ".service", "-p", "ActiveState,SubState,Result,NRestarts"],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return None
        kv = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
        active, sub, result = kv.get("ActiveState", ""), kv.get("SubState", ""), kv.get("Result", "")
        if r.returncode != 0 or not active or (active == "active" and sub == "running"):
            continue
        if active == "inactive" and result in ("", "success"):
            continue   # stopped by a person, or never started: "start it" is the right thing to say
        try:
            j = subprocess.run(["journalctl", "--user", "-u", n + ".service", "-n", "80", "--no-pager", "-o", "cat"],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=8)
            lines = [l.strip() for l in j.stdout.splitlines() if l.strip() and not UNIT_NOISE.match(l.strip())]
        except (OSError, subprocess.TimeoutExpired):
            lines = []
        last = CTRL_RE.sub("", lines[-1])[:220] if lines else ""
        fix = None
        if "must be root, or be an operator" in last or "access denied" in last.lower():
            fix = "sudo tailscale set --operator=$USER"   # tailscale then lets this user configure it without sudo
        elif "Address already in use" in last:
            fix = "another program has the port: find it with `ss -ltnp`, or give Shepr another port"
        try:
            restarts = int(kv.get("NRestarts") or 0)
        except ValueError:
            restarts = 0
        return {"unit": n, "state": f"{active}/{sub}" if sub else active, "restarts": restarts, "last": last, "fix": fix}
    return None


def unit_trouble_text(t):
    out = f"the unit {t['unit']} is failing to start ({t['state']}" + (f", {t['restarts']} restarts" if t["restarts"] else "") + ")"
    if t["last"]:
        out += f". Its last error: {t['last']}"
    if t["fix"]:
        out += f". Fix: {t['fix']}"
    return out


# At boot the user manager can start Shepr before Tailscale is connected. `tailscale serve` then fails ("unexpected
# state: NoState"), that failure stops the service, and systemd gives up after five quick tries, so the phone finds
# nothing until someone starts it by hand. This drop-in waits for Tailscale first and never stops retrying.
BOOT_DROPIN = "shepr-boot.conf"
BOOT_CONF = """# Written by `shepr fix-boot`: wait for Tailscale at boot, and keep retrying instead of giving up.
[Unit]
StartLimitIntervalSec=0

[Service]
ExecStartPre=-/usr/bin/tailscale wait --timeout=120s
TimeoutStartSec=180
RestartSec=5
"""


def boot_safe(name):
    """True when a reboot brings the unit back. One that serves the tailnet itself (--tailnet) does: the server waits for
    Tailscale inside, and nothing else has to succeed first. One that needs `tailscale serve` does only when it (with its
    drop-ins) waits for Tailscale and has no start limit."""
    paths = [os.path.join(_unit_dir(), name + ".service")]
    paths += sorted(glob.glob(os.path.join(_unit_dir(), name + ".service.d", "*.conf")))
    lines = []
    for p in paths:
        try:
            with open(p, errors="replace") as f:
                lines += [l.strip() for l in f if not l.lstrip().startswith(("#", ";"))]
        except OSError:
            continue
    def setting(key):   # systemd's rule for a list setting: an empty assignment forgets everything before it
        vals = []
        for l in lines:
            k, eq, v = l.partition("=")
            if eq and k.strip() == key:
                vals = vals + [v.strip()] if v.strip() else []
        return vals
    itself = ((any(re.search(r"(?:^|\s)--tailnet(?:\s|$)", v) for v in setting("ExecStart"))
               or any(re.search(r"(?:^|[\s\"'])SHEPR_TAILNET=1(?:$|[\s\"'])", v) for v in setting("Environment")))
              and not any("tailscale serve" in v for v in setting("ExecStartPre") + setting("ExecStartPost")))
    waits = any("tailscale wait" in v for v in setting("ExecStartPre"))
    nolimit = any(re.fullmatch(r"StartLimitIntervalSec\s*=\s*(0|infinity)", l) for l in lines)
    return itself or (waits and nolimit)


def cli_fix_boot(a):
    """Add the boot drop-in to every user unit that runs this server.py, then reload systemd. The running server is
    left alone: the change applies the next time the unit starts."""
    names = unit_names()
    if not names:
        print("shepr: no user unit runs this server.py. Install one first (README, \"Use it\").")
        return 1
    wrote = False
    for n in names:
        if boot_safe(n):
            print(f"  {n}: already waits for Tailscale and keeps retrying")
            continue
        d = os.path.join(_unit_dir(), n + ".service.d")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, BOOT_DROPIN)
        with open(path + ".tmp", "w") as f:
            f.write(BOOT_CONF)
        os.replace(path + ".tmp", path)
        print(f"  {n}: wrote {path}")
        wrote = True
    if wrote:
        try:
            r = subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"shepr: could not reload systemd ({e}). Run: systemctl --user daemon-reload")
            return 1
        if r.returncode != 0:
            print("shepr: systemctl --user daemon-reload failed: " + (r.stderr or r.stdout).strip())
            return 1
        print("  systemd reloaded. Shepr will come back after a reboot, even when Tailscale is slow to connect.")
    return 0


QR_NAME = "pair-qr.png"


def _own_private_dir(path, create=True):
    """True when path is a real directory (never a symlink) that belongs to this user and that nobody else can enter.

    An existing directory of ours with looser permissions is tightened to 0700 when create is set; one that is not
    ours is never trusted, so the pairing picture never lands where another user could read it."""
    if create:
        try:
            os.makedirs(path, mode=0o700, exist_ok=True)
        except OSError:
            return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        return False
    if st.st_mode & 0o077:
        if not create:
            return False
        try:
            os.chmod(path, 0o700)
            st = os.lstat(path)
        except OSError:
            return False
    return not (st.st_mode & 0o077)


def _qr_dirs():
    """Where the pairing picture may live, best first, as (parent, subfolder, may_create_parent). Never /tmp.

    $XDG_RUNTIME_DIR is the session's own directory (systemd makes it 0700): used as it is, never created or
    loosened. Shepr's state directory is the fallback, for a machine with no XDG_RUNTIME_DIR at all."""
    out = []
    xdg = os.environ.get("XDG_RUNTIME_DIR") or ""
    if os.path.isabs(xdg):
        out.append((os.path.realpath(xdg), "shepr", False))
    out.append((os.path.realpath(STATE_DIR), "run", True))
    return out


def private_qr_path():
    """A path for the pairing picture inside a verified private directory, or None when no such directory can be had."""
    for parent, sub, make in _qr_dirs():
        d = os.path.join(parent, sub)
        if _own_private_dir(parent, create=make) and _own_private_dir(d):
            return os.path.join(d, QR_NAME)
    return None


def forget_qr_pictures():
    """Remove the pairing picture from every place private_qr_path might have put it (looks only, creates nothing)."""
    for parent, sub, _ in _qr_dirs():
        try:
            os.remove(os.path.join(parent, sub, QR_NAME))
        except OSError:
            pass


def write_qr_png(url, path):
    """Draw url as a PNG at path. The URL carries the one-time pairing code, so it reaches qrencode over stdin (an
    argument would show in the process list) and the picture is born owner-only (0600), replacing whatever was at
    path and never following a symlink. Nothing secret exists on disk before that."""
    try:
        png = subprocess.run(["qrencode", "-o", "-", "-s", "10", "-m", "4", "-l", "M"], input=url.encode(),
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout
    except (subprocess.CalledProcessError, OSError):
        sys.exit("shepr: qrencode could not draw the QR")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        sys.exit(f"shepr: cannot replace {path}: {e.strerror}")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as e:
        sys.exit(f"shepr: cannot write the QR picture to {path}: {e.strerror}")
    with os.fdopen(fd, "wb") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(png)


def cli_pair(a):
    if a.cancel:   # drop a code that is still waiting to be used (the bar widget's Cancel button); needs no server
        forget_qr_pictures()   # the QR holds that code: it goes with it
        try:
            with state_lock():   # not halfway through a phone redeeming it
                os.remove(pairing_path())
            print("pairing code cancelled")
        except OSError:
            print("no pairing code was waiting")
        return 0
    info = server_info()
    if not info:
        ts = tailscale_problem()
        if ts:   # most likely why it is down: the unit waits for Tailscale, then fails without it, so "start it" alone would just wait again
            sys.exit(f"shepr: the server is not running here, and {ts[0]}. Shepr only answers over your tailnet, so its unit cannot "
                     f"come up without Tailscale.\nFix that first ({ts[1]}), then start Shepr: " + unit_hint())
        ut = unit_trouble()
        if ut:   # systemd is already trying to start it and failing: "start it" would be no help at all
            sys.exit("shepr: the server is not running here: " + unit_trouble_text(ut) + f".\nSee it all: journalctl --user -u {ut['unit']} -n 40")
        sys.exit("shepr: the server is not running here. Start it first: " + unit_hint())
    front = info.get("front") if isinstance(info.get("front"), dict) else None
    if front and (front.get("dead") or not front.get("listening")):   # the server is up, but no phone can reach it: say so before showing a code that cannot work
        print("shepr: warning: phones cannot reach this server right now: "
              + str(front.get("error") or "its tailnet listener is not up yet"), file=sys.stderr, flush=True)
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(12))
    shown = "-".join(code[i:i + 4] for i in range(0, 12, 4))
    ttl = max(30, min(900, a.ttl))
    with state_lock():   # the server redeems under this lock and writes the same temp file: never both at once
        write_pairing({"hash": hashlib.sha256(code.encode()).hexdigest(), "expires": time.time() + ttl,
                       "created": int(time.time()), "used_by": None})
    url = f"{info['url']}/#shepr-pair={shown}"
    png = a.png
    own_png = png is True   # the picture is ours (the private directory), so wait mode removes it when the code is done
    if png:
        if not shutil.which("qrencode"):
            sys.exit("shepr: --png needs qrencode (not installed)")
        if png is True:   # --png with no file name: a picture in a private directory (the bar widget uses this)
            png = private_qr_path()
            if not png:
                sys.exit("shepr: no private directory for the QR picture (neither $XDG_RUNTIME_DIR nor " +
                         os.path.join(STATE_DIR, "run") + " is owned by you and closed to others)")
        write_qr_png(url, png)
    if a.json:
        print(json.dumps({"url": url, "code": shown, "expires": int(time.time() + ttl), "png": png or None}), flush=True)
    else:
        print(f"\n  \033[1;36mSHEPR\033[0m  pair a phone with \033[1m{LOCAL}\033[0m\n")
        if shutil.which("qrencode"):
            subprocess.run(["qrencode", "-t", "ANSIUTF8", "-m", "2", "-l", "M"], input=url.encode(), check=False)   # stdin, not argv
        else:
            print("  (qrencode is not installed, so there is no QR here: install it for a scannable code)")
        print(f"\n  1. Open \033[1mShepr\033[0m on the iPhone and tap \033[1mScan\033[0m")
        print(f"  2. Point it at the code above")
        print(f"  or type the code by hand: \033[1;33m{shown}\033[0m")
        print(f"\n  {url}\n  Valid for {ttl}s, works once. Ctrl-C cancels.\n", flush=True)
    if a.no_wait:
        return 0
    t_end = time.time() + ttl
    try:
        while time.time() < t_end:
            p = read_pairing()
            if p and p.get("used_by"):
                d = devices().get(p["used_by"]) or {}
                x, y = dev_xy(d) if d else (0, 0)
                print(f"  \033[1;32m✓ paired\033[0m  {d.get('name', '?')}  {fingerprint(x, y)}\n")
                os.remove(pairing_path())
                return 0
            if not a.json:
                print(f"\r  waiting… {int(t_end - time.time()):>3}s ", end="", flush=True)
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if own_png:   # paired, expired or cancelled: the code is dead, so is the picture of it (--no-wait leaves it for the caller)
            forget_qr_pictures()
    try:
        os.remove(pairing_path())
    except OSError:
        pass
    print("\n  pairing code expired or cancelled; nothing was paired.")
    return 1


def cli_status(a):
    """One look at this machine, for the Omarchy bar widget and for you: is the server up, how many phones are
    paired, and which agents are waiting on you. It only reads, and it never needs the server or a phone."""
    info = server_info()
    out = {"build": BOOT_BUILD, "running": bool(info), "url": (info or {}).get("url"), "devices": len(devices()),
           "units": unit_names(), "herdr": False, "counts": {}, "waiting": []}
    out["boot_unsafe"] = [n for n in out["units"] if not boot_safe(n)]
    front = (info or {}).get("front")
    out["front"] = front if isinstance(front, dict) else None   # a server that takes tailnet connections itself (--tailnet): what answers, or why not
    ts = None if info else tailscale_problem()
    out["tailscale"] = f"{ts[0]}. Fix: {ts[1]}" if ts else None   # only asked when the server is down
    out["unit_error"] = None if info else unit_trouble(out["units"])   # likewise: systemd is asked only when there is something to explain
    def look(route, session):
        agents = run_herdr(route, ["agent", "list"], timeout=6)["result"]["agents"]
        labels = {w["workspace_id"]: w.get("label") for w in run_herdr(route, ["workspace", "list"], timeout=6)["result"]["workspaces"]}
        for ag in agents:
            st = ag.get("agent_status") or "unknown"
            out["counts"][st] = out["counts"].get(st, 0) + 1
            if st == "blocked" and len(out["waiting"]) < 12:
                w = {"project": labels.get(ag.get("workspace_id")) or os.path.basename(ag.get("cwd") or "") or "?",
                     "title": ag.get("terminal_title_stripped") or "", "agent": ag.get("agent") or "", "pane": ag.get("pane_id")}
                if session:
                    w["session"] = session   # a pane id repeats across sessions, so the session says which one
                out["waiting"].append(w)
    try:
        look(None, None)
        out["herdr"] = True
    except HerdrError as e:
        out["herdr_error"] = e.message
    except (KeyError, TypeError, AttributeError):
        out["herdr_error"] = "herdr answered in a shape Shepr does not know"
    if out["herdr"]:   # the other running sessions count too: an agent blocked in "work" is as blocked as one in the default session
        for name, route in local_sessions()[0]:
            try:
                look(route, name)
            except (HerdrError, KeyError, TypeError, AttributeError):
                continue   # a session that just stopped must not hide the ones that are up
    if a.json:
        print(json.dumps(out), flush=True)
        return 0
    print(f"shepr {BOOT_BUILD}: " + (f"running at {out['url']}" if out["running"] else f"not running. Start it: {unit_hint()}"))
    if out["front"]:
        f = out["front"]
        if f.get("listening"):
            print("  serves the tailnet itself on " + ", ".join(str(x) for x in f["listening"]) + (f" ({f['dev']})" if f.get("dev") else "")
                  + (f", certificate good until {day(f['tls_until'])}" if f.get("tls_until") else ""))
        if f.get("error"):
            print("  warning: " + ("phones cannot reach it, the tailnet listener is not up: " if not f.get("listening") else
                                   "phones cannot reach it: " if f.get("dead") else "") + str(f["error"]))
        elif not f.get("listening"):
            print("  warning: phones cannot reach it yet, the tailnet listener is not up")
    if out["tailscale"]:
        print(f"  warning: {out['tailscale']}. Shepr needs it to start")
    if out["unit_error"]:
        print("  why: " + unit_trouble_text(out["unit_error"]))
    print(f"  {out['devices']} phone(s) paired" + ("" if out["devices"] else ". Pair one: shepr pair"))
    for n in out["boot_unsafe"]:
        print(f"  warning: {n} may stay down after a reboot (it does not wait for Tailscale). Fix once: shepr fix-boot")
    if out["herdr"]:
        print("  agents: " + (", ".join(f"{n} {k}" for k, n in sorted(out["counts"].items())) or "none"))
        for w in out["waiting"]:
            print(f"  waiting: {w['project']}" + (f" - {w['title']}" if w["title"] else ""))
    else:
        print("  herdr: " + out.get("herdr_error", "not available"))
    return 0


def cli_devices(a):
    devs = devices()
    if not devs:
        print("no devices paired. Run: shepr pair")
    for did, d in devs.items():
        seen = time.strftime("%Y-%m-%d %H:%M", time.localtime(d["last_seen"])) if d.get("last_seen") else "never"
        print(f"{did}  {d['name']:<20} {dev_public(did, d)['fp']}  last seen {seen}")
    return 0


def cli_revoke(a):
    res = {}

    def drop(devs):
        hit = [i for i, d in devs.items() if a.who in (i, d["name"]) or i.startswith(a.who)]
        if len(hit) != 1:
            res["err"] = f"shepr: {'no' if not hit else 'more than one'} device matches {a.who!r}; see: shepr devices"
            return
        res["name"], res["id"] = devs[hit[0]]["name"], hit[0]
        del devs[hit[0]]
    mutate_devices(drop)
    if "err" in res:
        sys.exit(res["err"])
    load_push(); forget_push(res["id"])
    LIVE.forget(res["id"])
    print(f"revoked {res['name']} ({res['id']}). That phone is signed out now.")
    return 0


def cli_live(a):
    """The Lock Screen Live Activity (the optional native app in ios/). Pushing while the phone is locked needs a paid Apple Developer key."""
    if a.live_cmd == "setup":
        try:
            LIVE.setup(a.key, a.key_id, a.team_id, a.bundle)
        except (shepr_live.LiveError, OSError) as e:
            sys.exit(f"shepr: {getattr(e, 'message', e)}")
        print(f"Apple push key saved ({state_path('AuthKey.p8')}, mode 0600).")
        print("The host can now update the Lock Screen card while the phone is locked. In the app use the Push build, open it once, and tap Start.")
        return 0
    if a.live_cmd == "status":
        cfg = LIVE.cfg()
        if LIVE.configured():
            print(f"Apple push: set up (key {cfg['key_id']}, team {cfg['team_id']}, app {cfg['bundle']})")
        else:
            print("Apple push: not set up. It needs a key from a paid Apple Developer Program account: shepr live setup --help")
        names = {i: d["name"] for i, d in devices().items()}
        rows = LIVE.summary()
        for r in rows:
            print(f"  {names.get(r['device'], r['device'][:6]):<18} push-to-start {'yes' if r['start'] else 'no'}, cards running {r['activities']}, auto-start {'on' if r['auto'] else 'off'}")
        if not rows:
            print("  no phone has registered a Live Activity yet")
        return 0
    if a.live_cmd == "test":
        try:
            res = LIVE.test()
        except shepr_live.LiveError as e:
            sys.exit(f"shepr: {e.message}")
        for r in res:
            print(f"  {r['device'][:6]}: Apple answered {r['code']}" + (f" {r['reason']}" if r["reason"] else ""))
        if not res:
            print("  no card is running on any phone. Tap Start in the app first.")
        return 0 if res and all(r["code"] == 200 for r in res) else 1
    gone = LIVE.forget(a.who)
    print(f"forgot {len(gone)} phone(s)" if gone else "no phone matches that")
    return 0 if gone else 1


class CappedServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a ceiling on concurrent connections (the rest are closed at once)."""
    daemon_threads = True
    _slots = threading.BoundedSemaphore(48)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        # The other end went away, stalled, never finished its TLS handshake, or was not let in: the ordinary life of a
        # phone on a mobile network, and nothing to fix here. Anything else is a mistake of ours and keeps its traceback.
        if isinstance(sys.exc_info()[1], (ConnectionError, ssl.SSLError, TimeoutError, socket.timeout)):   # (socket.timeout: its own class before Python 3.10)
            return
        super().handle_error(request, client_address)


class TailnetServer(CappedServer):
    """One of this node's tailnet addresses. The TLS handshake is done in the connection's own thread (Handler.setup), so a
    slow one holds that thread and never the accept loop."""
    tailnet = True
    request_queue_size = 64

    def __init__(self, addr, handler, dev=None, front=None):
        self.address_family = socket.AF_INET6 if ":" in addr[0] else socket.AF_INET
        self.dev, self.front = dev, front
        super().__init__(addr, handler)

    def server_bind(self):
        if self.dev:
            opt = getattr(socket, "SO_BINDTODEVICE", None)
            if opt is None:
                raise OSError("this system cannot tie a socket to an interface")
            self.socket.setsockopt(socket.SOL_SOCKET, opt, self.dev.encode() + b"\0")
        if self.address_family == socket.AF_INET6:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        # TCPServer's bind, not HTTPServer's: that one looks the address up in DNS for a name nobody reads (getfqdn), and
        # a reverse lookup of a tailnet address can hang for seconds while Tailscale is half up.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[0], self.server_address[1]


def bind_words(ip, port, dev, e):
    if e.errno == errno.EADDRINUSE:
        return (f"port {port} is already taken on {ip}. If `tailscale serve` is still set up for it, take that down: "
                f"tailscale serve --https={port} off")
    if e.errno in (errno.EPERM, errno.EACCES):
        return f"this user may not tie a socket to {dev} (that needs Linux 5.7 or newer)"
    return f"cannot listen on {ip} port {port}: {e.strerror or e}"


def day(t):
    return time.strftime("%Y-%m-%d", time.gmtime(t)) if t else "an unknown date"


class Front:
    """Keeps a TLS listener on each of this node's tailnet addresses, whatever Tailscale does underneath. It waits for the
    node's name and its certificate, so a boot where Tailscale is slow no longer fails the unit. It binds again when the
    interface is made anew (tailscaled restarted: a socket tied to the old interface would listen for nothing, for ever).
    And it asks for the certificate again twice a day. One thread, one pass every few seconds; a pass that finds nothing
    changed is a few system calls and starts no process."""

    def __init__(self, port, handler):
        self.port, self.handler = port, handler
        self.ctx, self.until = None, None
        self.srv = {}                    # tailnet address -> (its server, (interface, that interface's index))
        self.cert_next = self.learn_next = 0.0
        self.learn_tries = 0
        self.ts_said = self.cert_err = self.bind_err = None
        self.binds = 0                   # listeners made so far: a new one is news for the journal even when nothing else changed
        self.proxied, self.serve_next = None, 0.0   # is `tailscale serve` still set up for this port, and when to look again
        self.told = None

    def ask_cert(self, now):
        try:
            self.ctx, self.until = fetch_cert(OWNER["dns"])
            self.cert_err, self.cert_next = None, now + CERT_EVERY_S
        except TlsError as e:
            self.cert_err = str(e)
            self.cert_next = now + (30.0 if self.ctx is None else 900.0)   # with nothing to serve on, soon; else in a quarter of an hour

    def close(self, ip):
        s, _ = self.srv.pop(ip)
        try:
            s.shutdown()
            s.server_close()
        except OSError:
            pass

    def listen(self, addrs, up):
        want = {}
        if self.ctx is not None:
            for ip in up:
                dev = TAILNET_DEV or addrs.get(ip)
                try:
                    want[ip] = (dev, socket.if_nametoindex(dev) if dev else 0)
                except OSError:
                    continue   # the interface went away between two looks: the next pass sees it
        for ip in [ip for ip, (_, key) in self.srv.items() if want.get(ip) != key]:
            self.close(ip)     # its address or its interface is gone, or is not the one it was bound to
        self.bind_err = None
        for ip, key in want.items():
            if ip in self.srv:
                continue
            try:
                s = TailnetServer((ip, self.port), self.handler, dev=key[0], front=self)
            except OSError as e:
                self.bind_err = bind_words(ip, self.port, key[0], e)
                continue
            threading.Thread(target=s.serve_forever, daemon=True).start()
            self.srv[ip] = (s, key)
            self.binds += 1

    def report(self, why):
        listening = sorted(self.srv)
        dev = next((key[0] for _, key in self.srv.values() if key[0]), None)
        err = why or (self.cert_err if self.ctx is None else None) or self.bind_err
        note = self.cert_err if self.ctx is not None else None   # the door is open on the certificate in hand; a fresh one was refused
        expired = bool(self.ctx is not None and self.until and self.until < time.time())
        if expired:   # it listens, but no phone will accept what it shows
            err = err or f"the certificate ran out on {day(self.until)} and Tailscale has not given a new one" + (f" ({note})" if note else "")
        if listening and self.proxied and not err:
            err = (f"`tailscale serve` is still set up for port {self.port} on this node, so tailnet traffic goes through it and "
                   f"never reaches this listener. Take it down: tailscale serve --https={self.port} off")
        state = (tuple(listening), err, note, self.until, OWNER.get("dns"), dev, self.binds)
        if state == self.told:
            return
        self.told = state
        TAILNET.update(listening=listening, dev=dev, error=err or note, tls_until=self.until, dead=not listening or expired)
        with contextlib.suppress(OSError):
            write_server_json(self.port)
        if listening:
            print(f"shepr: serving the tailnet itself at {public_url(self.port)} ({' and '.join(listening)}"
                  f"{' on ' + dev if dev else ', bound by address only'}; certificate good until {day(self.until)}; "
                  "callers checked with tailscale whois)", file=sys.stderr, flush=True)
        if err:
            print(f"shepr: the tailnet listener is {'only partly' if listening else 'not'} up: {err}", file=sys.stderr, flush=True)
        elif note:
            print(f"shepr: tailnet certificate not refreshed, still serving the one in hand: {note}", file=sys.stderr, flush=True)

    def tick(self):
        now = time.monotonic()
        addrs = iface_addrs()

        def present():   # this node's tailnet addresses that are on the machine right now
            return [ip for ip in OWNER.get("ips") or [] if TAILNET_DEV or not addrs or ip in addrs]
        up = present()
        if (not tailnet_known() or not up) and now >= self.learn_next:
            # Not known yet, or the addresses that were known are gone from the machine: ask Tailscale. Every few seconds
            # for the first minute, every half minute after that (a machine with Tailscale switched off is not hammered).
            self.learn_tries += 1
            self.learn_next = now + (TAILNET_RETRY_S if self.learn_tries <= 12 else 30.0)
            learn_tailnet(quiet=True)
            up = present()
            tp = None if (tailnet_known() and up) else tailscale_problem()
            self.ts_said = f"{tp[0]}. Fix: {tp[1]}" if tp else None
        why = None
        if not tailnet_known():
            why = self.ts_said or "Tailscale has not said who this node is yet"
        elif not owner_login():
            why = "this node is a tagged device, which has no user: set SHEPR_OWNER to your Tailscale login"
        elif not up:
            why = self.ts_said or "this node's tailnet address is not on any interface here (Tailscale is off, or runs without one)"
        else:
            self.learn_tries = 0
        ready = why is None
        if ready and self.ctx is None:
            kept = saved_cert(OWNER["dns"])   # the one an earlier run kept opens the door at once
            if kept:
                self.ctx, self.until = kept
            elif now >= self.cert_next:
                self.ask_cert(now)            # nothing to serve with yet: asking is all there is to do
        made = self.binds
        self.listen(addrs, up if ready else [])
        if self.srv and (self.binds != made or now >= self.serve_next):   # at every new listener, and four times an hour
            self.serve_next = now + 900.0
            self.proxied = serve_in_front(self.port)
        self.report(why)
        if ready and self.ctx is not None and now >= self.cert_next:
            self.ask_cert(now)                # the door is open meanwhile; a change is reported by the next pass

    def run(self):
        while True:
            try:
                self.tick()
            except Exception:   # a mistake here must not end the thread that keeps the door open
                traceback.print_exc(file=sys.stderr)
            time.sleep(FRONT_TICK_S)


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("pair", "devices", "revoke", "status", "fix-boot", "live"):
        ap = argparse.ArgumentParser(prog="shepr")
        sub = ap.add_subparsers(dest="cmd", required=True)
        p = sub.add_parser("pair", help="show a QR that pairs one phone, then wait for it")
        p.add_argument("--ttl", type=int, default=180); p.add_argument("--png", nargs="?", const=True, metavar="FILE", help="also write the QR as a picture, owner-only (no FILE: a private directory)"); p.add_argument("--json", action="store_true")
        p.add_argument("--no-wait", action="store_true")
        p.add_argument("--cancel", action="store_true", help="drop a code that is still waiting to be used")
        sub.add_parser("devices", help="list paired phones")
        st = sub.add_parser("status", help="is the server up, how many phones, what is waiting on you")
        st.add_argument("--json", action="store_true")
        r = sub.add_parser("revoke", help="sign a phone out"); r.add_argument("who")
        sub.add_parser("fix-boot", help="make the installed unit wait for Tailscale at boot and keep retrying")
        lv = sub.add_parser("live", help="the Lock Screen Live Activity: Apple push setup, status, a test card")
        ls = lv.add_subparsers(dest="live_cmd", required=True)
        s1 = ls.add_parser("setup", help="give the host your Apple push key (paid Developer Program) so the card updates while the phone is locked")
        s1.add_argument("--key", required=True, metavar="AuthKey_XXXXXXXXXX.p8"); s1.add_argument("--key-id", required=True, metavar="XXXXXXXXXX")
        s1.add_argument("--team-id", required=True, metavar="XXXXXXXXXX"); s1.add_argument("--bundle", default="com.nixfred.shepr.live")
        ls.add_parser("status", help="is Apple push set up, and which phones have a card")
        ls.add_parser("test", help="send a test card to every running card, to prove the Apple path works")
        s4 = ls.add_parser("forget", help="drop a phone's Live Activity tokens"); s4.add_argument("who")
        a = ap.parse_args()
        if a.cmd not in ("status", "fix-boot", "live", "pair"):
            learn_tailnet()   # status only reads, and a bar widget polls it, so it skips the tailscale call. pair takes the URL from the running
                              # server's server.json and never checks a request, so with Tailscale off it would only print the server's own
                              # "tailnet requests will be refused" ahead of the real answer (tailscale_problem)
        sys.exit({"pair": cli_pair, "devices": cli_devices, "revoke": cli_revoke, "status": cli_status,
                  "fix-boot": cli_fix_boot, "live": cli_live}[a.cmd](a))

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("port", nargs="?", type=int, default=8742)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--tailnet", action="store_true", default=os.environ.get("SHEPR_TAILNET") == "1",
                    help="serve this node's tailnet addresses itself: own TLS (tailscale cert), every caller checked with "
                         "tailscale whois, no `tailscale serve` in front")
    a = ap.parse_args()
    if a.tailnet and a.bind not in ("127.0.0.1", "localhost"):
        sys.exit("shepr: --tailnet serves the tailnet addresses itself, with TLS. Leave --bind at 127.0.0.1.")
    TAILNET["on"] = bool(a.tailnet)
    try:
        os.nice(5)  # Law 22: background work never competes with Fred's typing
    except OSError:
        pass
    learn_tailnet()
    handler = functools.partial(Handler, directory=HERE)
    try:
        srv = CappedServer((a.bind, a.port), handler)
    except OSError as e:
        # Nothing has been written yet. server.json used to be written BEFORE the port was taken, so a second Shepr started
        # by mistake replaced it with its own (dead) pid, and `shepr pair` then said the running server was not running.
        sys.exit(f"shepr: cannot listen on {a.bind}:{a.port}: {e.strerror or e}. Is another Shepr already running here? (shepr status)")
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        os.chmod(STATE_DIR, 0o700)   # an older or hand-made state directory must not be readable by other users
    except OSError:
        pass
    _tailnet["port"] = a.port
    write_server_json(a.port)
    if TAILNET["on"]:
        threading.Thread(target=Front(a.port, handler).run, daemon=True).start()   # learns the identity too, and keeps the listeners up
    elif not tailnet_known() and shutil.which("tailscale"):
        threading.Thread(target=tailnet_retry_loop, daemon=True).start()
    devices()
    load_push()
    prune_waits()
    prune_events()
    if WATCH_ON:
        threading.Thread(target=watch_loop, daemon=True).start()   # reads Claude Code's session files on its own slow clock
    threading.Thread(target=poller, daemon=True).start()
    if AUTOADD and not os.environ.get("SHEPR_LOCAL_ONLY"):
        threading.Thread(target=autoscan_loop, daemon=True).start()
    threading.Thread(target=LIVE.run, daemon=True).start()   # sends Lock Screen card updates; idle until a phone registers
    if not USAGE_FIXTURE:
        threading.Thread(target=METER.loop, daemon=True).start()   # the subscription meter, on its own slow clock
    print(f"shepr: serving {HERE} on http://{a.bind}:{a.port} as {LOCAL} "
          f"(owner={OWNER['login']}, devices={len(DEVICES)}, writes={'off' if READONLY else 'on, confirm-first'}"
          f"{', tailnet auto-add every ' + format(SCAN_EVERY_S, 'g') + ' s' if AUTOADD else ''})",
          file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
