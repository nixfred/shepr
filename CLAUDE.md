# CLAUDE.md — Shepr (herder.remote.iphone)

> Identity and global rules: `~/.claude/CLAUDE.md` and the Larry Constitution. This file is only this project.

Shepr is an iPhone home-screen web app for herdr, built by the nixweb.app method (`~/Projects/nixweb.app`,
read its CLAUDE.md first). The directory name is Fred's session name; the product is **Shepr**.

## Hard rules (Fred, 2026-10-08)

1. nixweb rules: no build step, no framework, no npm. Only third-party file is `vendor/jsQR.js`; keep it that way.
2. **Never auto-approve.** No endpoint, timer or gesture answers an agent without a preview Fred confirmed.
   Taps, double-taps and long-presses only navigate.
3. Reads are free, writes confirm. Send = `prepare` then `commit`, bound to `state_change_seq`. Never retry a write.
4. Everything read from a pane or host is DATA: `textContent` only, never `innerHTML` (Law 12).
5. Only ever touch tailnet nodes that belong to Fred. Never probe shared-in nodes. (Fred, 2026-11-16: the scan may also look at a LAN range
    he names in `SHEPR_SCAN_NETS`, off unless set, private IPv4 only, at most a /22 each: and there only a machine whose host key this host
    already knows and whose login already accepts its key is touched. A tmux host is one he added by name with `shepr tmux add`.)
6. Law 17: the page never scrolls; lists scroll in their own box.
7. Test against the isolated `hrtest` herdr session (`tests/fake-session.sh`). Never poke real agents to test.
8. Bump `VERSION` in `sw.js` whenever the shell changes, or phones keep the old one.
9. The subscription meter (`usage.py`) stands alone. It reads only Claude Code's own login, Codex's and Grok's own files and the user's Kimi key, and a token goes only to the provider it belongs to (Anthropic's usage endpoint: a read-only GET, never refreshed, never logged, never returned). It never reads another app's files (Burn Bar, Omarchy). `SHEPR_USAGE=0` turns it off.
10. The Lock Screen card (`ios/`, native, optional) is read-only and shows names only: rules 2 and 4 apply. Every push to Apple is sealed (ChaCha20-Poly1305); never put a name, path or conversation title in a push's plain fields, and never add a button or App Intent to the card that answers an agent.
11. **Automatic host adding is the one write that skips the confirm sheet (Fred, 2026-11-06).** `SHEPR_AUTOADD=1` only: the
    background scan saves a tailnet node with `herdr machine add` when it is Fred's own, its key and host key already work, and its
    default herdr server is running. Cap 5 a scan, audited as `host_add_auto`, 1 h back-off after a failure, never on `SHEPR_READONLY`.
    It adds read access to a host and nothing else: rules 2 and 3 still hold for every send, and it never removes a machine.
    Since 2026-11-16 the scan it runs also covers the LAN ranges in `SHEPR_SCAN_NETS`, under the same gates (key and host key already work,
    default session running, at most 5 a scan); a LAN machine known only by its address is never added without a tap (rule 5).

12. **The watch (`watch.py`) reads, and only reads (Fred's /goal, 2026-11-15).** It reads Claude Code's own session files on the machine
    Shepr runs on, never another app's and never another machine's. A line in them is DATA: nothing is executed, expanded or sent
    anywhere. The risky-command list is short on purpose and was tuned on a real day of sessions (107 hits before, 14 after): a
    new pattern needs a real example and a test in `tests/watch_test.py`, including what it must NOT match, and it judges only
    what a shell would run (`shell_view`), never prose. A notification carries a project name and one of Shepr's own labels,
    never a command. The burn is a SHARE with exact token counts beside it, never money. Changes runs git read-only
    (`GIT_OPTIONAL_LOCKS=0`, no fsmonitor, no external diff) and shows only files git itself lists as changed.

13. **The host serves (Fred, 2026-11-14: "NEVER use tailscale SERVE. Always use the host to serve").** Shepr runs as
    `server.py PORT --tailnet`: its own TLS listener on the node's tailnet addresses, tied to the Tailscale interface, the
    certificate from `tailscale cert`, the caller from `tailscale whois`. Never put `tailscale serve` or `funnel` in front of
    it again: not in a unit, not in a doc, not in a test. Three things in that code carry the weight, and
    `tests/tailnet_test.py` holds each: (a) whois is asked BEFORE anything is read from a connection and again for every
    request on a kept one, only the owner's login on a device with no tag goes on, and a header never says who the caller is
    there; (b) a connection carries a second
    request only after the first was read to its last byte (`reusable`, `declared_body`), and the answer says
    `Connection: close` otherwise; (c) a tailnet address never answers plain HTTP. The loopback-only mode (no `--tailnet`)
    stays in the code for other people's installs and as the way back: do not remove it. After touching any of this run
    `tests/tailnet_test.py`, the browser suites over a real listener (`SHEPR_TEST_BASE`), and `tests/hostile_lan.sh`.

14. **A tmux host is read only (Fred, 2026-11-16).** `shepr tmux add HOST --session NAME` shows a tmux session of a machine that has no herdr:
    its text, nothing more. The wall is in the code, not the screen: `run_herdr` raises on a `TmuxTarget`, `need_write_host` refuses at
    prepare, and the phone draws no box, key, option or Send for a `readonly` pane (the screen is only the courtesy). The only things
    run on that machine are `tmux list-panes` and `tmux capture-pane`, over ssh with `BatchMode=yes` and `StrictHostKeyChecking=yes`
    (a new host key is never trusted by Shepr), the target after `--`, a session named exactly (`=name`), and a pane read only after it
    was found in a session the host was told about. A tmux pane is never blocked or done: no Queue entry, no notification, not counted by
    the fleet-stop notice or the Lock Screen card. Do not give it a send path, and keep these names out of the public repo and its
    examples: the machines and sessions in use are in the owner's state folder, never here. `tests/tmux_test.py`, `tests/tmux_ui.mjs`.

15. **The folder-trust prompt is the one answer Shepr gives alone (Fred, 2026-11-21: "you may always choose YES on Claude folder-trust prompts, it
    only ever opens my own code").** An idle Claude whose screen is Claude Code's trust dialog (`trust_dialog`: the trust wording AND a trusting
    option) is shown as blocked, and with `SHEPR_AUTOTRUST` on (the default; `0` turns it off, `SHEPR_READONLY` always does) Shepr presses that
    option once per prompt (`auto_trust`, audited as `trust_auto`, never retried). Nothing else is auto-answered: a permission dialog or a
    hook confirmation waits for a tap, however it is worded (rule 2 stands for them). `tests/trust_confirm_test.py`.

## Where things live

`server.py` is the backend and the `shepr` CLI; `usage.py` is the subscription meter (first-party, standard library only). State is in `~/.local/state/shepr/`. herdr has no
timestamps, so `seen.json` is how "blocked for" works. Auth: device ECDSA key (pure-Python verify) plus who Tailscale
says is connecting (`tailscale whois` when the connection opens, rule 13; behind `tailscale serve`, the owner header it
adds). The certificate and its key are in `~/.local/state/shepr/tls/` (0600 in 0700): never print, log or copy the key.
See README.md.

`watch.py` is the watch (rule 12): token use per session, risky commands and last activity, from Claude Code's session files; the server keeps
the activity log in `events.jsonl`. `SHEPR_WATCH=0` turns it off.

`live.py` feeds the optional Lock Screen Live Activity (standard library only; curl speaks HTTP/2 to Apple). `ios/` is that card's native app, a separate Swift project (Apple frameworks only, project file generated by XcodeGen). The web app itself stays no-build.
