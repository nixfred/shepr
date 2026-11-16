"""The LAN scan (SHEPR_SCAN_NETS): which ranges are accepted, who is scanned, how a machine is named, what is left out, and that the tailnet
scan still works beside it. Nothing here touches a network: the port check, DNS and ssh are stubbed.   Run: python3 tests/lan_scan_test.py"""
import importlib.util, json, os, socket, subprocess, sys, tempfile, types
os.environ["SHEPR_STATE"] = tempfile.mkdtemp()
for _k in ("SHEPR_AUTOADD", "SHEPR_SCAN_S", "SHEPR_READONLY", "SHEPR_LOCAL_ONLY", "SHEPR_SCAN_NETS"): os.environ.pop(_k, None)
os.environ["SHEPR_LABEL"] = "hub"
here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("shepr_server", os.path.join(here, "..", "server.py"))
srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)
ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name); ok = ok and bool(cond)
import contextlib, io
def quiet(fn, *a):
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        r = fn(*a)
    return r, err.getvalue()

# ---- which ranges are accepted
nets = lambda t: [str(n) for n in quiet(srv.parse_scan_nets, t)[0]]
check("a private /24 is accepted", nets("192.168.5.0/24") == ["192.168.5.0/24"])
check("several, separated by commas or spaces, repeats dropped, a host bit is forgiven", nets("192.168.5.0/24, 192.168.1.0/24 192.168.5.5/24 172.16.0.0/22") == ["192.168.5.0/24", "192.168.1.0/24", "172.16.0.0/22"])
check("nothing set is no LAN scan", nets("") == [] and nets(None) == [] and nets("  ") == [])
check("a public range is refused, so a typo can never sweep a network that is not yours", nets("8.8.8.0/24") == [] and nets("100.64.0.0/24") == [] and nets("0.0.0.0/24") == [])
check("a range bigger than a /22 is refused, even a private one", nets("10.0.0.0/8") == [] and nets("192.168.0.0/16") == [] and nets("10.0.0.0/21") == [] and nets("10.0.0.0/22") == ["10.0.0.0/22"])
check("IPv6 and nonsense are refused", nets("fd00::/64") == [] and nets("junk") == [] and nets("10.0.0.0/33") == [])
r, err = quiet(srv.parse_scan_nets, "8.8.8.0/24, junk")
check("and each refusal is said once, on stderr", err.count("left out") == 2 and "8.8.8.0/24" in err and "junk" in err)
check("at most four ranges", len(quiet(srv.parse_scan_nets, "192.168.5.0/24 192.168.6.0/24 192.168.7.0/24 192.168.8.0/24 192.168.9.0/24")[0]) == 4)
def env_nets(v):
    code = "import importlib.util as u,sys;s=u.spec_from_file_location('s',sys.argv[1]);m=u.module_from_spec(s);s.loader.exec_module(m);print(len(m.SCAN_NETS))"
    e = dict(os.environ, SHEPR_STATE=tempfile.mkdtemp()); e.pop("SHEPR_SCAN_NETS", None)
    if v is not None: e["SHEPR_SCAN_NETS"] = v
    return subprocess.run([sys.executable, "-c", code, os.path.join(here, "..", "server.py")], env=e, capture_output=True, text=True).stdout.strip()
check("it is off unless the owner sets SHEPR_SCAN_NETS, and the variable is read at start", [env_nets(None), env_nets(""), env_nets("192.168.5.0/24")] == ["0", "0", "1"])

# ---- the port check is a plain connect, short
l = socket.socket(); l.bind(("127.0.0.1", 0)); l.listen(1); port = l.getsockname()[1]
check("an open port is up", srv.lan_port_open("127.0.0.1", port) is True)
l.close()
check("a closed port is not", srv.lan_port_open("127.0.0.1", port) is False)

# ---- naming: a DNS name only when it leads back to the same address
def dns(ptr, fwd):
    def gethostbyaddr(ip):
        if ip not in ptr: raise socket.herror(1, "no name")
        return (ptr[ip], [], [ip])
    def getaddrinfo(name, port, family=0, *a, **k):
        if name not in fwd: raise socket.gaierror(-2, "unknown")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (fwd[name], 0))]
    socket.gethostbyaddr, socket.getaddrinfo = gethostbyaddr, getaddrinfo
real_ptr, real_fwd = socket.gethostbyaddr, socket.getaddrinfo
dns({"192.168.5.76": "box.lan"}, {"box": "192.168.5.76"})
check("a name that leads back to the same address is the machine's name and the ssh target", srv.lan_name("192.168.5.76") == ("box", "box"))
dns({"192.168.5.76": "box.lan"}, {"box": "192.168.5.99"})
check("a name that leads somewhere else is NOT used as a target, so an odd DNS record cannot send ssh to another machine", srv.lan_name("192.168.5.76") == ("lan-192-168-5-76", "192.168.5.76"))
dns({}, {})
check("no name at all: lan-192-168-5-76 and the address", srv.lan_name("192.168.5.76") == ("lan-192-168-5-76", "192.168.5.76"))
dns({"192.168.5.5": "9lives.lan", "192.168.5.6": "-x.lan"}, {"9lives": "192.168.5.5", "-x": "192.168.5.6"})
check("a name the app cannot use as a label (starts with a digit or a dash) falls back to the address", srv.lan_name("192.168.5.5")[1] == "192.168.5.5" and srv.lan_name("192.168.5.6")[1] == "192.168.5.6")
dns({"192.168.5.7": "My_Box.Example.com", "192.168.5.8": "Bo$x.lan"}, {"my_box": "192.168.5.7", "bo-x": "192.168.5.8"})
check("a name is lower-cased, and a character a label cannot hold becomes a dash", srv.lan_name("192.168.5.7") == ("my_box", "my_box") and srv.lan_name("192.168.5.8") == ("bo-x", "bo-x"))
socket.gethostbyaddr, socket.getaddrinfo = real_ptr, real_fwd

# ---- who is scanned and who is left out
srv.SCAN_NETS = quiet(srv.parse_scan_nets, "10.9.9.0/29")[0]
asked = []
UP = {"10.9.9.1", "10.9.9.2", "10.9.9.3", "10.9.9.4", "10.9.9.5"}
srv.lan_port_open = lambda ip, port=22: (asked.append(ip), ip in UP)[1]
NAMES = {"10.9.9.1": ("router", "router"), "10.9.9.2": ("me", "me"), "10.9.9.3": ("box", "box"), "10.9.9.4": ("tmuxy", "tmuxy"), "10.9.9.5": ("tail", "tail")}
srv.lan_name = lambda ip: NAMES.get(ip, ("lan-" + ip.replace(".", "-"), ip))
srv.iface_addrs = lambda: {"10.9.9.2": "eth0", "127.0.0.1": "lo"}
srv.machine_profiles = lambda: [{"label": "router", "target": "router"}]
srv.save_tmux_hosts([{"id": "tmuxy", "target": "tmuxy-host", "sessions": []}])
peers = srv.lan_peers(skip={"tail"})
check("every host address of the range is asked, and this machine's own is not", sorted(asked) == ["10.9.9.1", "10.9.9.3", "10.9.9.4", "10.9.9.5", "10.9.9.6"] and "10.9.9.2" not in asked)
check("a saved herdr machine, a tmux host and a tailnet peer are not offered again", [p["name"] for p in peers] == ["box"])
check("a peer carries what the Hosts tab and the Add plan read, and says it came from the LAN",
      peers[0] == {"name": "box", "dns": "", "os": "lan", "ip": "10.9.9.3", "target": "box", "via": "lan"})
srv.machine_profiles = lambda: []
srv.save_tmux_hosts([])
srv.lan_name = lambda ip: ("lan-" + ip.replace(".", "-"), ip)
UP.clear(); UP.update({"10.9.9.6", "10.9.9.3", "10.9.9.1"})
check("results come in address order", [p["ip"] for p in srv.lan_peers()] == ["10.9.9.1", "10.9.9.3", "10.9.9.6"])
srv.SCAN_NETS = []
asked.clear()
check("no ranges, no scan: not one address is asked", srv.lan_peers() == [] and asked == [])

# ---- the scan: tailnet first, then the LAN, each failing alone
srv.SCAN_NETS = quiet(srv.parse_scan_nets, "10.9.9.0/29")[0]
tail_now = [{"name": "tailbox", "dns": "t.ts.net", "os": "linux", "ip": "100.0.0.1", "target": "tailbox"}]
lan_now = [{"name": "box", "dns": "", "os": "lan", "ip": "10.9.9.3", "target": "box", "via": "lan"},
           {"name": "lan-10-9-9-6", "dns": "", "os": "lan", "ip": "10.9.9.6", "target": "10.9.9.6", "via": "lan"}]
seen_skip = []
srv.tailnet_peers = lambda: [dict(p) for p in tail_now]
srv.lan_peers = lambda skip=(): (seen_skip.append(set(skip)), [dict(p) for p in lan_now])[1]
probed = []
answers = {"tailbox": ("ready", "/usr/bin/herdr", True), "box": ("noherdr", "herdr not found on PATH or in the login shell", None),
           "10.9.9.6": ("hostkey", "first connect needed: run `ssh 10.9.9.6` once and check the fingerprint", None)}
srv.ssh_probe = lambda t: (probed.append(t), answers[t])[1]
srv.AUTOADD = False; srv.SCAN_MEMO.clear(); srv.SCAN.update(at=0, running=True, peers=[])
srv.scan_tailnet(force=True)
check("both lists are probed with the same strict probe, by their ssh target", sorted(probed) == ["10.9.9.6", "box", "tailbox"])
check("the LAN scan is told which names the tailnet already has", seen_skip == [{"tailbox"}])
byname = {p["name"]: p for p in srv.SCAN["peers"]}
check("each peer shows its state, and a machine whose host key is not known yet is 'first connect', never offered a key",
      byname["box"]["state"] == "noherdr" and byname["lan-10-9-9-6"]["state"] == "hostkey" and byname["tailbox"]["state"] == "ready" and byname["box"]["via"] == "lan")
check("the scan finished", not srv.SCAN["running"])
def boom(): raise RuntimeError("tailscale is off")
srv.tailnet_peers = boom; probed.clear(); srv.SCAN["running"] = True; srv.SCAN_MEMO.clear()
_, err = quiet(srv.scan_tailnet, True)
check("with Tailscale down the LAN is still scanned", sorted(probed) == ["10.9.9.6", "box"] and "tailnet scan failed" in err and [p["name"] for p in srv.SCAN["peers"]] == ["box", "lan-10-9-9-6"])
srv.tailnet_peers = lambda: [dict(p) for p in tail_now]
def lanboom(skip=()): raise RuntimeError("no route")
srv.lan_peers = lanboom; probed.clear(); srv.SCAN["running"] = True; srv.SCAN_MEMO.clear()
_, err = quiet(srv.scan_tailnet, True)
check("with the LAN scan failing the tailnet's answers are kept", probed == ["tailbox"] and "LAN scan failed" in err and [p["name"] for p in srv.SCAN["peers"]] == ["tailbox"])
srv.SCAN_NETS = []
called = []
srv.lan_peers = lambda skip=(): called.append(1) or []
probed.clear(); srv.SCAN["running"] = True
srv.scan_tailnet(force=True)
check("without SHEPR_SCAN_NETS the scan is exactly what it was: tailnet only, the LAN code never runs", probed == ["tailbox"] and called == [])
def tailboom(): raise RuntimeError("tailscale is off")
srv.tailnet_peers = tailboom; srv.SCAN["running"] = True
_, err = quiet(srv.scan_tailnet, True)
check("without the LAN scan a tailnet failure is still reported as before", "tailnet scan failed" in err and not srv.SCAN["running"])

# ---- a LAN device that takes the connection and never logs in is not asked every five minutes; a tailnet node that is down still is
srv.SCAN_NETS = quiet(srv.parse_scan_nets, "10.9.9.0/29")[0]
tail_now = [{"name": "tailbox", "dns": "t.ts.net", "os": "linux", "ip": "100.0.0.1", "target": "tailbox"}]
lan_now = [{"name": "cam", "dns": "", "os": "lan", "ip": "10.9.9.9", "target": "10.9.9.9", "via": "lan"}]
srv.tailnet_peers = lambda: [dict(p) for p in tail_now]
srv.lan_peers = lambda skip=(): [dict(p) for p in lan_now]
answers = {"tailbox": ("down", "refused", None), "10.9.9.9": ("down", "Connection closed by 10.9.9.9 port 22", None)}
probed.clear(); srv.SCAN_MEMO.clear(); srv.SCAN["running"] = True
srv.scan_tailnet(force=True)
probed.clear(); srv.SCAN["running"] = True
srv.scan_tailnet(force=False)
check("a second background scan asks the tailnet node that was down again, and leaves the LAN device alone", probed == ["tailbox"])
srv.SCAN_MEMO["cam"] = (srv.time.time() - 3600,) + srv.SCAN_MEMO["cam"][1:]
probed.clear(); srv.SCAN["running"] = True
srv.scan_tailnet(force=False)
check("after half an hour it asks the LAN device again", sorted(probed) == ["10.9.9.9", "tailbox"])
srv.SCAN_NETS = []

# ---- the automatic add: a named LAN machine with herdr running is added like a tailnet one; one known only by address is not
adds = []
srv.run_herdr = lambda machine, args, timeout=10, want_json=True: adds.append(list(args)) or ""
srv.poll_all = lambda wait=2.0: None
srv.READONLY = False; srv.AUTO_FAILED.clear()
P = lambda name, target, via: {"name": name, "target": target, "state": "ready", "running": True, "detail": "", "os": "lan", "ip": "", "via": via}
srv.auto_add([P("box", "box", "lan"), P("lan-10-9-9-6", "10.9.9.6", "lan")])
check("a LAN machine with a name is added with the same command as the Add button; one that has only an address is left for a person to add",
      adds == [["machine", "add", "box", "--label", "box"]])

sys.exit(0 if ok else 1)
