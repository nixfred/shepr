#!/usr/bin/env bash
# Can a machine that is NOT on your tailnet reach a Shepr that serves the tailnet itself (server.py PORT --tailnet)?
#
# It should not: the listener is tied to the Tailscale interface (SO_BINDTODEVICE), so a packet for the node's tailnet
# address that arrives over any other interface finds nothing listening. This script plays that other machine. It makes a
# network namespace joined to this host by a virtual cable (a "LAN" of two), routes this node's tailnet addresses down the
# cable, and from inside tries two ports on each address (IPv4, and IPv6 when the node has one):
#
#   a control port, opened here on that same address but tied to no interface   must ANSWER  (so a refusal means something)
#   the Shepr port                                                               must be REFUSED
#
#   tests/hostile_lan.sh [PORT]        PORT defaults to 8742. Needs root for the namespace (it asks sudo), Linux, iproute2.
#
# A host firewall that drops everything from an unknown interface would make both fail and prove nothing about Shepr, so
# when a control port does not answer the script lets the cable's own interface through the firewall and tries again.
# That rule names that one interface, which only this test can reach. It is removed with everything else when the script
# ends, however it ends, short of kill -9 (then: ip netns del sheprlan<pid>; ip link del vsh<pid>a; and the iptables and
# ip6tables rule for that interface). Python runs isolated (-I: nothing is imported from the folder you stand in) and, for
# everything that does not need root, as the user who called sudo. The control listeners answer one word, are ended with
# the script, and end by themselves after 90 seconds whatever happens. Tailscale and Shepr are never touched.
set -euo pipefail
PORT=${1:-8742}
if [ "$(id -u)" != 0 ]; then exec sudo -n "$0" "$@"; fi
NS=sheprlan$$
A=vsh${$}a
B=vsh${$}b
NET4=10.213.77
NET6=fd00:213:77
OPEN4=0
OPEN6=0
PIDS=()
AS=()
if [ -n "${SUDO_UID:-}" ] && command -v setpriv >/dev/null; then AS=(setpriv --reuid "$SUDO_UID" --regid "${SUDO_GID:-$SUDO_UID}" --clear-groups); fi
IP4=$(tailscale ip -4 2>/dev/null | head -1 || true)
IP6=$(tailscale ip -6 2>/dev/null | head -1 || true)
[ -n "$IP4" ] || { echo "no tailnet address on this machine (is Tailscale connected?)"; exit 2; }
if ip -o addr | grep -q " $NET4\."; then echo "$NET4.0/30 is in use here: edit NET4 in this script"; exit 2; fi

read -r -d '' PY_CONTROL <<'PY' || true
import signal, socket, sys
signal.alarm(90)   # it ends by itself, whatever becomes of the script that started it
addr = sys.argv[1]
s = socket.socket(socket.AF_INET6 if ":" in addr else socket.AF_INET)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind((addr, 0))
s.listen(8)
print(s.getsockname()[1], flush=True)
while True:
    c, _ = s.accept()
    c.sendall(b"control\n")
    c.close()
PY
read -r -d '' PY_TRY <<'PY' || true
import socket, sys
try:
    c = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=4)
except ConnectionRefusedError:
    print("refused"); sys.exit()
except socket.timeout:
    print("timeout"); sys.exit()
except OSError as e:
    print("error", e); sys.exit()
c.settimeout(3)
try:
    d = c.recv(24)
except OSError:
    d = b""
print("open", d)
PY

WORK=$(mktemp -d)
cleanup() {
  for p in ${PIDS[@]+"${PIDS[@]}"}; do kill "$p" 2>/dev/null || true; done
  [ "$OPEN4" = 1 ] && iptables -D INPUT -i "$A" -j ACCEPT 2>/dev/null || true
  [ "$OPEN6" = 1 ] && ip6tables -D INPUT -i "$A" -j ACCEPT 2>/dev/null || true
  ip netns del "$NS" 2>/dev/null || true
  ip link del "$A" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT
control() { # address file: opens a control listener on that address, tied to no interface; its port lands in the file.
  # Called plainly, never inside $( ): the listener's PID has to reach PIDS in THIS shell, or cleanup cannot end it.
  ${AS[@]+"${AS[@]}"} python3 -I -c "$PY_CONTROL" "$1" > "$2" 2>/dev/null &
  PIDS+=("$!")
  for _ in 1 2 3 4 5 6 7 8 9 10; do [ -s "$2" ] && break; sleep 0.2; done
}
try() { ip netns exec "$NS" ${AS[@]+"${AS[@]}"} python3 -I -c "$PY_TRY" "$1" "$2"; }
own() { ${AS[@]+"${AS[@]}"} python3 -I -c "$PY_TRY" "$1" "$2"; }

ip netns add "$NS"
ip link add "$A" type veth peer name "$B"
ip link set "$B" netns "$NS"
ip addr add "$NET4.1/30" dev "$A"
ip link set "$A" up
ip netns exec "$NS" ip addr add "$NET4.2/30" dev "$B"
ip netns exec "$NS" ip link set "$B" up
ip netns exec "$NS" ip link set lo up
ip netns exec "$NS" ip route add "$IP4/32" via "$NET4.1"
if [ -n "$IP6" ]; then
  ip -6 addr add "$NET6::1/126" dev "$A" nodad 2>/dev/null || IP6=""
fi
if [ -n "$IP6" ]; then
  ip netns exec "$NS" ip -6 addr add "$NET6::2/126" dev "$B" nodad
  ip netns exec "$NS" ip -6 route add "$IP6/128" via "$NET6::1"
fi
sleep 0.7

verdict=0
proven=0
one() { # family address
  local fam=$1 ip=$2 ctl c s o tool=iptables
  [ "$fam" = 6 ] && tool=ip6tables
  control "$ip" "$WORK/port.$fam"
  ctl=$(head -1 "$WORK/port.$fam" 2>/dev/null || true)
  [ -n "$ctl" ] || { echo "  IPv$fam: the control listener did not start"; return; }
  c=$(try "$ip" "$ctl")
  if [ "${c%% *}" != open ] && command -v "$tool" >/dev/null; then
    "$tool" -I INPUT 1 -i "$A" -j ACCEPT
    if [ "$fam" = 6 ]; then OPEN6=1; else OPEN4=1; fi
    echo "  IPv$fam: this host's firewall drops what comes over the cable, so the cable's interface was let through for this test"
    c=$(try "$ip" "$ctl")
  fi
  s=$(try "$ip" "$PORT")
  o=$(own "$ip" "$PORT")
  echo "  IPv$fam $ip   control port $ctl: $c   |   the Shepr port $PORT: $s   |   the Shepr port from this machine itself: ${o%% *}"
  if [ "${c%% *}" != open ]; then echo "  IPv$fam INCONCLUSIVE: the control port did not answer either, so something here stops the packet before any socket is asked."; return; fi
  if [ "${o%% *}" != open ]; then echo "  IPv$fam INCONCLUSIVE: nothing listens on that address and port even from this machine. Is Shepr running with --tailnet on port $PORT?"; return; fi
  case "$s" in
    refused|timeout) echo "  IPv$fam PASS: the packet that reached the control port found no Shepr."; proven=$((proven + 1)) ;;
    *) echo "  IPv$fam FAIL: the Shepr port answered a machine that is not on the tailnet."; verdict=1 ;;
  esac
}
echo "From a namespace on a private cable ($NET4.2), which is not on the tailnet:"
one 4 "$IP4"
if [ -n "$IP6" ]; then one 6 "$IP6"; else echo "  IPv6: this node has no tailnet IPv6 address (or this machine no IPv6): not tried"; fi
if [ "$verdict" = 1 ]; then echo "FAIL"; exit 1; fi
if [ "$proven" = 0 ]; then echo "INCONCLUSIVE: nothing was proven."; exit 2; fi
echo "PASS: Shepr answers on the Tailscale interface only ($proven address(es) proven)."
