#!/usr/bin/env bash
# Builds an ISOLATED herdr session ("hrtest") with fake blocked/working/idle agents so the app can be
# exercised without ever touching a real agent. Nothing here talks to the default session.
#
#   tests/fake-session.sh up        start the isolated server and populate it
#   tests/fake-session.sh down      stop ONLY the hrtest session
#   tests/fake-session.sh env       print the env to run server.py against it
#
# Then:  eval "$(tests/fake-session.sh env)"; python3 server.py 8743 &
#        node tests/ui.mjs            (needs playwright; paths at the top of the file)
set -euo pipefail
SOCK="$HOME/.config/herdr/sessions/hrtest/herdr.sock"
H(){ env -u HERDR_PANE_ID -u HERDR_WORKSPACE_ID -u HERDR_TAB_ID HERDR_SOCKET_PATH="$SOCK" herdr "$@" < /dev/null; }
mk(){ # label cwd state [agent] [title] [file-to-show]
  local p; p=$(H workspace create --label "$1" --cwd "$2" | python3 -c 'import json,sys;print(json.load(sys.stdin)["result"]["root_pane"]["pane_id"])')
  local show
  # herdr reads the TERMINAL title (what Claude Code sets to a summary of the session), so set it the same way
  local ttl="printf '\\033]0;✳ %s\\007' '${5:-$1}'"
  if [ -n "${6:-}" ]; then show="clear; cat '$6'"
  else show="clear; printf '%s\n' 'Working on $1' 'Do you want to proceed?' '❯ 1. Yes' '  2. Yes, and do not ask again' '  3. No' '' 'Esc to cancel'"; fi
  H pane run "$p" "cd $2; $ttl; $show; sleep 100000" >/dev/null
  sleep .6
  # report-agent fakes the state herdr would detect; "done" is not a state you can fake.
  H pane report-agent "$p" --source hrtest:fake --agent "${4:-claude}" --state "$3" >/dev/null
  echo "$1 $p $3"
}
case "${1:-}" in
  up)
    (setsid nohup herdr --session hrtest server >/dev/null 2>&1 < /dev/null &); sleep 3
    # a saved session comes back with every workspace earlier runs made: start from none, so tests are deterministic
    for w in $(H workspace list | python3 -c 'import json,sys;[print(w["workspace_id"]) for w in json.load(sys.stdin)["result"]["workspaces"]]'); do H workspace close "$w" >/dev/null 2>&1 || true; done
    HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    mk burnbar "$HOME/Projects/burnbar" blocked claude "PR Hunter burnbar"
    mk clarity "$HOME/Projects/clarity" blocked claude "Bug fixes and pull requests"
    mk blip "$HOME/Projects/blip" working codex "Release notes draft"
    mk infomarchy "$HOME/Projects/infomarchy" idle claude "Herdr project list sort order" "$HERE/sample-pane.txt"
    mk clarity "$HOME/Projects/clarity" blocked claude "Herder workflow automation" ;;
  multi) H workspace list | python3 -c 'import json,sys;[print(w["workspace_id"]) for w in json.load(sys.stdin)["result"]["workspaces"] if w["label"]=="multi"]' | while read w; do H workspace close "$w" >/dev/null; done
    HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    mk multi "$HOME/Projects/whats-next" blocked claude "Project customization walkthrough" "$HERE/sample-multi.txt" ;;
  queued) for w in $(H workspace list | python3 -c 'import json,sys;[print(w["workspace_id"]) for w in json.load(sys.stdin)["result"]["workspaces"] if w["label"]=="queued"]'); do H workspace close "$w" >/dev/null; done
    HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    mk queued "$HOME/Projects/whats-next" working claude "A long task with a message queued" "$HERE/sample-queued.txt" ;;
  gone) for w in $(H workspace list | python3 -c 'import json,sys;[print(w["workspace_id"]) for w in json.load(sys.stdin)["result"]["workspaces"] if w["label"]=="gone"]'); do H workspace close "$w" >/dev/null; done
    HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    mk gone "$HOME/Projects/whats-next" working claude "A session that is about to vanish" "$HERE/sample-pane.txt" ;;
  gone-rm) for w in $(H workspace list | python3 -c 'import json,sys;[print(w["workspace_id"]) for w in json.load(sys.stdin)["result"]["workspaces"] if w["label"]=="gone"]'); do H workspace close "$w" >/dev/null; done ;;
  queued-rm) for w in $(H workspace list | python3 -c 'import json,sys;[print(w["workspace_id"]) for w in json.load(sys.stdin)["result"]["workspaces"] if w["label"]=="queued"]'); do H workspace close "$w" >/dev/null; done ;;
  asleep) mkdir -p /tmp/shepr-dormant /tmp/shepr-busy
    for w in $(H workspace list | python3 -c 'import json,sys;[print(w["workspace_id"]) for w in json.load(sys.stdin)["result"]["workspaces"] if w["label"] in ("dormant","busy")]'); do H workspace close "$w" >/dev/null; done
    H workspace create --label dormant --cwd /tmp/shepr-dormant >/dev/null     # a bare shell prompt, and a conversation on file for it
    b=$(H workspace create --label busy --cwd /tmp/shepr-busy | python3 -c 'import json,sys;print(json.load(sys.stdin)["result"]["root_pane"]["pane_id"])')
    H pane run "$b" "sleep 100000" >/dev/null; sleep 1; echo "dormant + busy created" ;;
  asleep-rm) for w in $(H workspace list | python3 -c 'import json,sys;[print(w["workspace_id"]) for w in json.load(sys.stdin)["result"]["workspaces"] if w["label"] in ("dormant","busy")]'); do H workspace close "$w" >/dev/null; done ;;
  free) for w in $(H workspace list | python3 -c 'import json,sys;[print(w["workspace_id"]) for w in json.load(sys.stdin)["result"]["workspaces"] if w["label"]=="freetext"]'); do H workspace close "$w" >/dev/null; done
    HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    mk freetext "$HOME/Projects/whats-next" blocked claude "A question with a typing row" "$HERE/sample-free.txt" ;;
  nomenu) for w in $(H workspace list | python3 -c 'import json,sys;[print(w["workspace_id"]) for w in json.load(sys.stdin)["result"]["workspaces"] if w["label"]=="nomenu"]'); do H workspace close "$w" >/dev/null; done
    HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    mk nomenu "$HOME/Projects/whats-next" blocked claude "Blocked with nothing to answer" "$HERE/sample-nomenu.txt" ;;
  free-rm|nomenu-rm) for w in $(H workspace list | python3 -c 'import json,sys;[print(w["workspace_id"]) for w in json.load(sys.stdin)["result"]["workspaces"] if w["label"] in ("freetext","nomenu")]'); do H workspace close "$w" >/dev/null; done ;;
  multi-rm) H workspace list | python3 -c 'import json,sys;[print(w["workspace_id"]) for w in json.load(sys.stdin)["result"]["workspaces"] if w["label"]=="multi"]' | while read w; do H workspace close "$w" >/dev/null; done ;;
  down) herdr session stop hrtest < /dev/null ;;
  env) echo "export HERDR_SOCKET_PATH=$SOCK SHEPR_STATE=/tmp/shepr-test-state SHEPR_POLL=3" ;;
  *) sed -n '2,12p' "$0"; exit 1 ;;
esac
