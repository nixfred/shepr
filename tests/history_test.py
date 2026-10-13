"""history_page: reading a transcript BACKWARDS in pages must give exactly what a forward read gives, however the
lines fall across the 256 KB read windows. Pure Python.  Run: python3 tests/history_test.py"""
import importlib.util, json, os, sys, tempfile
os.environ["SHEPR_STATE"] = tempfile.mkdtemp()
for _k in ("SHEPR_NEW_CMD", "SHEPR_CLAUDE_CMD", "SHEPR_CODEX_CMD", "SHEPR_PROJECTS", "SHEPR_PUSH_ALLOW", "SHEPR_READONLY"): os.environ.pop(_k, None)   # a test pins its own settings, not the shell's
os.environ["SHEPR_POLL"] = "15"
here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("shepr_server", os.path.join(here, "..", "server.py"))
srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)
ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name); ok = ok and bool(cond)

def line(kind, i, pad=0):
    ts = f"2026-10-09T10:{i % 60:02d}:00Z"
    if kind == "user": return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": f"question {i} " + "q" * pad}}
    if kind == "asst": return {"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": f"answer {i} " + "a" * pad},
        {"type": "tool_use", "name": "Bash", "input": {"command": f"echo {i}"}}]}}
    if kind == "res": return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "x", "content": f"out {i} " + "r" * pad}]}}
    return {"type": "attachment", "attachment": {"blob": "z" * pad}}   # noise the reader must skip

rows = []
for i in range(400):
    rows += [line("user", i), line("noise", i, 3000), line("asst", i), line("res", i, 200), line("noise", i, 50)]
rows.insert(500, line("asst", 9001, 600_000))        # one line bigger than two read windows
rows.insert(900, {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-10-09T10:00:00Z"})
rows.insert(1200, {"type": "user", "isMeta": True, "message": {"role": "user", "content": "meta noise"}})
rows.insert(1300, {"type": "user", "timestamp": "2026-10-09T10:00:00Z", "message": {"role": "user", "content": "<system-reminder>skip me</system-reminder>"}})
d = tempfile.mkdtemp(); path = os.path.join(d, "t.jsonl")
with open(path, "wb") as f:
    for r in rows: f.write(json.dumps(r).encode() + b"\n")
    f.write(b"\n\n")                                  # blank lines at the end
    f.write(json.dumps(line("user", 7777)).encode())  # and a final line with no newline

fwd = []
with open(path, "rb") as f:
    for ln in f:
        if ln.strip(): fwd.extend(srv.render_line(ln))
pages, before = [], None
while True:
    entries, before, size, note = srv.history_page(path, before)
    pages.append(entries)
    if before is None: break
    assert len(pages) < 1000
rev = [e for p in reversed(pages) for e in p]
check("every entry comes back, in order, once (%d entries, %d pages)" % (len(rev), len(pages)), rev == fwd)
check("pages are bounded", max(len(p) for p in pages) <= srv.HIST_ENTRIES + 3)
check("the first entry is the first real message, the last is the unterminated final line",
      rev[0]["x"].startswith("question 0") and rev[-1]["x"].startswith("question 7777"))
check("noise is skipped: thinking, attachments, meta, system reminders", all("meta noise" not in e["x"] and "skip me" not in e["x"] and "hmm" not in e["x"] for e in rev))
check("a compaction boundary shows as a divider", sum(1 for e in rev if e["r"] == "divider") == 1)
check("a 600 KB line is clipped, not dropped", any(e["x"].startswith("answer 9001") and "more characters" in e["x"] and len(e["x"]) < 13000 for e in rev))
check("tool calls and results are shown briefly", any(e["r"] == "tool" and e["x"] == "Bash(echo 5)" for e in rev) and any(e["r"] == "result" and e["x"].startswith("out 5 ") for e in rev))
e, nxt, size, note = srv.history_page(path, 0)
check("asking from offset 0 returns nothing and says it is done", e == [] and nxt is None)
e, nxt, size, note = srv.history_page(path, 10 ** 12)
check("an offset past the end is treated as the end", e == pages[0] and nxt == (None if len(pages) == 1 else nxt))
# one line bigger than the cap sits between two small ones: paging walks past it and still reaches the oldest message
_cap = srv.HIST_LINE; srv.HIST_LINE = 300_000      # above one 256 KB read window, below the 600 KB line
try:
    big = os.path.join(d, "big.jsonl")
    with open(big, "wb") as f:
        for r in (line("user", 1), line("asst", 2, 600_000), line("user", 3)): f.write(json.dumps(r).encode() + b"\n")
    seen, offs, before = [], [], None
    while len(offs) < 50:
        entries, nxt, size, note = srv.history_page(big, before)
        seen += [e["x"] for e in entries]; offs.append(nxt)
        if nxt is None: break
        if before is not None and nxt >= before: break      # paging must always move backwards
        before = nxt
    check("a line bigger than the cap does not hide the messages before it",
          any(x.startswith("question 1 ") for x in seen) and any(x.startswith("question 3 ") for x in seen) and offs[-1] is None)
    check("paging past that line moves strictly backwards and ends", len(offs) < 50 and all(a > b for a, b in zip(offs[:-2], offs[1:-1])))
finally:
    srv.HIST_LINE = _cap
# a transcript path must never be built from a hostile session id
for bad in ("../x", "a/b", "", None, "x" * 5 + "\x00"):
    check(f"session id {bad!r} is refused", srv.transcript_file({"agent_session": {"agent": "claude", "kind": "id", "value": bad}}) is None)
sys.exit(0 if ok else 1)
