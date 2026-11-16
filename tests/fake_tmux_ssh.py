#!/usr/bin/env python3
"""A stand-in for `ssh HOST tmux ...` for the tmux tests: answers `tmux list-panes` and `tmux capture-pane` from
$FAKE_TMUX_DIR/state.json (the remote clock, the sessions, their windows and panes) and logs every call to calls.log.
Used through SHEPR_SSH by tests/tmux_test.py and tests/tmux_ui.mjs. It talks to no machine."""
import json, os, shlex, sys

d = os.environ["FAKE_TMUX_DIR"]
argv = sys.argv[1:]
with open(os.path.join(d, "calls.log"), "a") as f:
    f.write(json.dumps(argv) + "\n")
st = json.load(open(os.path.join(d, "state.json")))
if st.get("fail"):
    sys.stderr.write(st["fail"] + "\n")
    sys.exit(255)
i = argv.index("--")                     # Shepr always puts the target after --, so a name can never be an option
target, remote = argv[i + 1], argv[i + 2]
words = shlex.split(remote)
if "&&" in words:
    words = words[:words.index("&&")]    # the `&& printf NOW` tail is answered below
S = st["sessions"]
if words[:2] == ["tmux", "list-panes"]:
    if "-a" in words:
        names = list(S)
    else:
        n = words[words.index("-t") + 1]
        assert n.startswith("="), n      # an exact session name, never a prefix match
        names = [n[1:]]
        if names[0] not in S:
            sys.stderr.write("can't find session: %s\n" % names[0])
            sys.exit(1)
    assert words[words.index("-F") + 1].startswith("#{session_name}\t")
    for n in names:
        for w in S[n]["windows"]:
            for p in w["panes"]:
                print("\t".join([n, "@%d" % w["id"], w["name"], "%%%d" % p["id"], p["path"], p["cmd"], str(w["activity"])]))
    print("NOW %d" % st["now"])
elif words[:2] == ["tmux", "capture-pane"]:
    pid = int(words[words.index("-t") + 1][1:])
    n = int(words[words.index("-S") + 1])
    for s in S.values():
        for w in s["windows"]:
            for p in w["panes"]:
                if p["id"] == pid:
                    lines = p["text"].split("\n")
                    print("\n".join(lines[n:] if n < 0 else lines))
                    sys.exit(0)
    sys.exit(1)
else:
    sys.stderr.write("unexpected: %s\n" % remote)
    sys.exit(127)
