"""Shepr Live: the status board on the iPhone Lock Screen, as an Apple Live Activity.

A Home Screen web app cannot show a Live Activity, so the optional native app in ios/ draws the card and this file feeds it.
Standard library only, like the rest of the host. Two ways the card stays current:

  * the app, while it is open, asks /api/live/status and updates the card itself (works with a free Apple ID);
  * with a paid Apple Developer Program key (`shepr live setup`), the host pushes updates through APNs, so the card stays current
    while the phone is locked and the app is closed.

Nothing readable passes through Apple. Each push carries the status sealed with ChaCha20-Poly1305 under a key only this host and the
phone's Lock Screen extension hold, so Apple sees ciphertext and timing, never a session name. The one plain-text part is the
"Live status is on your Lock Screen" banner Apple requires when a push starts a card.
"""
import base64
import contextlib
import hmac
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import sys
import threading
import time

N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551   # the P-256 group order
P256_OID = b"\x2a\x86\x48\xce\x3d\x03\x01\x07"
AAD = b"shepr-live-v1"                      # binds a sealed status to this use; the Swift side opens with the same bytes
ATTRIBUTES_TYPE = "ShepActivityAttributes"  # the Swift type name Apple matches a push-to-start payload against

MIN_GAP_S = 10                 # at most one update per card this often
HEARTBEAT_S = 20 * 60          # an unchanged status is still re-sent this often, so "stale" on the phone means the host is gone
STALE_S = 45 * 60              # the card marks itself stale this long after the last push
ROLL_S = 7 * 3600 + 45 * 60    # Apple ends a card after 8 hours: end it and start a fresh one a little before
START_COOLDOWN_S = 30 * 60
BACKOFF_S = 60
WAKE_S = 5                     # how often the worker looks again (gaps and heartbeats)
RECENT_DONE_S = 2 * 3600       # a finished session stays on the card this long
MAX_ROWS = 8
MAX_PAYLOAD = 4096 - 128       # Apple's limit is 4096 bytes for the whole push; keep a margin
HOSTS = {"production": "https://api.push.apple.com", "sandbox": "https://api.sandbox.push.apple.com"}
TOKEN_RE = re.compile(r"^[0-9a-f]{32,400}$")
ACT_RE = re.compile(r"^[0-9A-Za-z-]{8,64}$")


class LiveError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


# ---------------------------------------------------------------------------------------------------- ChaCha20-Poly1305 (RFC 8439)
def _rotl(v, n):
    return ((v << n) & 0xFFFFFFFF) | (v >> (32 - n))


def _quarter(x, a, b, c, d):
    x[a] = (x[a] + x[b]) & 0xFFFFFFFF; x[d] = _rotl(x[d] ^ x[a], 16)
    x[c] = (x[c] + x[d]) & 0xFFFFFFFF; x[b] = _rotl(x[b] ^ x[c], 12)
    x[a] = (x[a] + x[b]) & 0xFFFFFFFF; x[d] = _rotl(x[d] ^ x[a], 8)
    x[c] = (x[c] + x[d]) & 0xFFFFFFFF; x[b] = _rotl(x[b] ^ x[c], 7)


def chacha20_block(key, counter, nonce):
    state = [0x61707865, 0x3320646E, 0x79622D32, 0x6B206574, *struct.unpack("<8I", key), counter & 0xFFFFFFFF, *struct.unpack("<3I", nonce)]
    x = state[:]
    for _ in range(10):
        _quarter(x, 0, 4, 8, 12); _quarter(x, 1, 5, 9, 13); _quarter(x, 2, 6, 10, 14); _quarter(x, 3, 7, 11, 15)
        _quarter(x, 0, 5, 10, 15); _quarter(x, 1, 6, 11, 12); _quarter(x, 2, 7, 8, 13); _quarter(x, 3, 4, 9, 14)
    return struct.pack("<16I", *[(x[i] + state[i]) & 0xFFFFFFFF for i in range(16)])


def chacha20_xor(key, counter, nonce, data):
    out = bytearray()
    for i in range(0, len(data), 64):
        ks = chacha20_block(key, counter + i // 64, nonce)
        out += bytes(a ^ b for a, b in zip(data[i:i + 64], ks))
    return bytes(out)


def poly1305(key, msg):
    r = int.from_bytes(key[:16], "little") & 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
    s = int.from_bytes(key[16:32], "little")
    p = (1 << 130) - 5
    acc = 0
    for i in range(0, len(msg), 16):
        acc = (acc + int.from_bytes(msg[i:i + 16] + b"\x01", "little")) * r % p
    return ((acc + s) & ((1 << 128) - 1)).to_bytes(16, "little")


def _mac(otk, aad, ct):
    pad = lambda b: b + b"\x00" * (-len(b) % 16)
    return poly1305(otk, pad(aad) + pad(ct) + struct.pack("<QQ", len(aad), len(ct)))


def aead_seal(key, nonce, plaintext, aad=AAD):
    otk = chacha20_block(key, 0, nonce)[:32]
    ct = chacha20_xor(key, 1, nonce, plaintext)
    return ct + _mac(otk, aad, ct)


def aead_open(key, nonce, data, aad=AAD):
    if len(data) < 16:
        raise ValueError("too short")
    ct, tag = data[:-16], data[-16:]
    if not hmac.compare_digest(tag, _mac(chacha20_block(key, 0, nonce)[:32], aad, ct)):
        raise ValueError("bad tag")
    return chacha20_xor(key, 1, nonce, ct)


def seal_status(status, key, nonce=None):
    """base64 of nonce(12) + ciphertext + tag(16): CryptoKit's ChaChaPoly "combined" form."""
    nonce = nonce or secrets.token_bytes(12)
    plain = json.dumps(status, separators=(",", ":"), ensure_ascii=False).encode()
    return base64.b64encode(nonce + aead_seal(key, nonce, plain)).decode()


def open_status(b64, key):
    raw = base64.b64decode(b64)
    return json.loads(aead_open(key, raw[:12], raw[12:]))


# ---------------------------------------------------------------------------------------------------- what the card shows
def short_label(label):
    """"Session (5-hour)" -> "5h", "Weekly (Opus)" -> "week": the card has room for a word, not a title."""
    t = str(label or "").lower()
    if "session" in t or ("5" in t and "h" in t):
        return "5h"
    if "week" in t or "7" in t:
        return "week"
    if "month" in t:
        return "month"
    if "day" in t:
        return "day"
    return (str(label or "").split() or ["?"])[0][:6]


def meters_from_view(view):
    """The one most-used window of each plan that has a live reading, at most three, from the subscription meter's view."""
    out = []
    for ag in (view or {}).get("agents") or []:
        rows = [r for r in ag.get("rows") or [] if isinstance(r, dict)]
        if not rows:
            continue
        r = max(rows, key=lambda r: r.get("used") or 0)
        out.append({"n": str(ag.get("name") or ag.get("id") or "?")[:8], "l": short_label(r.get("label")),
                    "p": max(0, min(100, int(round(float(r.get("used") or 0) * 100)))), "e": int((r.get("resets_ms") or 0) // 1000)})
    return out[:3]


def build_status(agents, hosts, meters, now):
    """The queue boiled down to what fits a Lock Screen card. Names only, never a conversation title (the same rule as a notification).
    Blocked first (longest waiting first), then finished within the last two hours, then working."""
    b = d = w = i = 0
    blocked, done, working = [], [], []
    for a in agents:
        st = a.get("status")
        name = str(a.get("wlabel") or a.get("project") or "session")[:32]
        host = str(a.get("host") or "")[:12]
        since = int(a.get("since") or 0)
        if st == "blocked":
            b += 1; blocked.append((since or int(now), name, host, since))
        elif st == "done" and (not since or now - since <= RECENT_DONE_S):
            d += 1; done.append((since, name, host))
        elif st == "working":
            w += 1; working.append((name, host))
        else:
            i += 1
    blocked.sort(); done.sort(reverse=True); working.sort()
    rows = [{"n": n, "h": h, "s": "b", "a": s} for _k, n, h, s in blocked]
    rows += [{"n": n, "h": h, "s": "d", "a": s} for s, n, h in done]
    rows += [{"n": n, "h": h, "s": "w", "a": 0} for n, h in working]
    return {"v": 1, "t": int(now), "b": b, "d": d, "w": w, "i": i, "z": sum(int(h.get("asleep") or 0) for h in hosts),
            "h": [{"n": str(h.get("id"))[:12], "o": h.get("status") == "ok"} for h in hosts][:4],
            "r": rows[:MAX_ROWS], "m": list(meters or [])[:3]}


def status_sig(status):
    """What changed, ignoring the build time: two statuses with the same signature need only one push."""
    return json.dumps({k: v for k, v in status.items() if k != "t"}, sort_keys=True, separators=(",", ":"))


def build_payload(event, status, key, now, host, stale_in=STALE_S):
    s = dict(status)
    while True:
        aps = {"timestamp": int(now), "event": event, "content-state": {"sealed": seal_status(s, key)}}
        if event == "start":
            aps["attributes-type"] = ATTRIBUTES_TYPE
            aps["attributes"] = {"host": str(host)[:24]}
            aps["alert"] = {"title": "Shepr", "body": "Live status is on your Lock Screen."}   # Apple requires an alert to start; it names nothing
        if event == "end":
            aps["dismissal-date"] = int(now)
        else:
            aps["stale-date"] = int(now + stale_in)
        body = json.dumps({"aps": aps}, separators=(",", ":"))
        if len(body.encode()) <= MAX_PAYLOAD:
            return body
        if s.get("r"):
            s["r"] = s["r"][:-1]
        elif s.get("m"):
            s["m"] = s["m"][:-1]
        else:
            raise LiveError("too_big", "the status does not fit one Live Activity push")


def load_p8(path):
    """The private scalar of an Apple .p8 key (PKCS#8, P-256)."""
    try:
        with open(path, "r") as f:
            pem = f.read()
        der = base64.b64decode("".join(l.strip() for l in pem.splitlines() if l.strip() and not l.startswith("-----")))
    except (OSError, ValueError):
        raise LiveError("bad_key", "cannot read that key file")
    i = der.find(b"\x02\x01\x01\x04\x20")
    if P256_OID not in der or i < 0 or i + 37 > len(der):
        raise LiveError("bad_key", "that is not a P-256 private key, the kind in an Apple AuthKey .p8 file")
    d = int.from_bytes(der[i + 5:i + 37], "big")
    if not 0 < d < N:
        raise LiveError("bad_key", "that private key is out of range")
    return d


# ---------------------------------------------------------------------------------------------------- the host side
class Live:
    def __init__(self, state_dir, sign, host, valid=None, locker=None, apns_url=None, clock=None, log=None):
        self.dir, self.sign, self.host = state_dir, sign, host
        self.valid = valid or (lambda: None)                  # the paired device ids, or None to skip pruning
        self.locker = locker or contextlib.nullcontext        # the state lock shared with the shepr CLI
        self.apns_url = apns_url or os.environ.get("SHEPR_APNS_URL") or None   # tests aim this at a local stand-in
        self.clock = clock or time.time
        self.log = log or (lambda m: print("shepr: " + m, file=sys.stderr, flush=True))
        self.lock = threading.RLock()
        self.cv = threading.Condition()
        self._devs, self._stamp = {}, "unset"
        self._jwt = None
        self._latest = None
        self.last = {}                                        # device id -> what Apple answered to the last push

    # -- files
    def _path(self, name):
        return os.path.join(self.dir, name)

    def _stat(self, name):
        try:
            st = os.stat(self._path(name))
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _load(self):
        with self.lock:
            s = self._stat("live.json")
            if s != self._stamp:
                try:
                    with open(self._path("live.json")) as f:
                        d = json.load(f)
                    self._devs = d if isinstance(d, dict) else {}
                except (OSError, ValueError):
                    self._devs = {}
                self._stamp = s
            return self._devs

    def _save(self):
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        fd = os.open(self._path("live.json.tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self._devs, f)
        os.replace(self._path("live.json.tmp"), self._path("live.json"))
        self._stamp = self._stat("live.json")

    def _mutate(self, fn):
        with self.lock, self.locker():
            self._stamp = "unset"       # never write back what another process (the CLI) already changed
            self._load()
            r = fn(self._devs)
            self._save()
            return r

    # -- Apple key
    def cfg(self):
        try:
            with open(self._path("apns.json")) as f:
                c = json.load(f)
            if isinstance(c, dict) and all(isinstance(c.get(k), str) for k in ("key_id", "team_id", "bundle")):
                return c
        except (OSError, ValueError):
            pass
        return None

    def configured(self):
        return bool(self.cfg()) and os.path.exists(self._path("AuthKey.p8")) and bool(shutil.which("curl"))

    def setup(self, p8_path, key_id, team_id, bundle):
        if not re.fullmatch(r"[A-Z0-9]{10}", key_id or ""):
            raise LiveError("bad_request", "the key id is the 10 letters and digits in the .p8 file name (AuthKey_<KEYID>.p8)")
        if not re.fullmatch(r"[A-Z0-9]{10}", team_id or ""):
            raise LiveError("bad_request", "the team id is the 10 letters and digits shown under your name at developer.apple.com")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{2,150}", bundle or ""):
            raise LiveError("bad_request", "that is not a bundle id")
        load_p8(p8_path)
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        with open(p8_path, "rb") as src:
            data = src.read()
        fd = os.open(self._path("AuthKey.p8"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        fd = os.open(self._path("apns.json"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"key_id": key_id, "team_id": team_id, "bundle": bundle}, f)
        self._jwt = None

    def _token(self, now):
        """The provider token Apple wants: ES256 over {iss: team, iat}, reused for 40 minutes (Apple refuses a fresher one inside 20)."""
        if self._jwt and now - self._jwt[1] < 2400:
            return self._jwt[0]
        c = self.cfg()
        if not c:
            raise LiveError("not_configured", "the host has no Apple push key yet: run `shepr live setup`")
        d = load_p8(self._path("AuthKey.p8"))
        head = b64u(json.dumps({"alg": "ES256", "kid": c["key_id"]}, separators=(",", ":")).encode())
        body = b64u(json.dumps({"iss": c["team_id"], "iat": int(now)}, separators=(",", ":")).encode())
        tok = head + "." + body + "." + b64u(self.sign(d, (head + "." + body).encode()))
        self._jwt = (tok, now)
        return tok

    # -- phones
    def push_info(self, dev):
        d = self._load().get(dev) or {}
        acts = d.get("acts") or {}
        env = (d.get("start") or next(iter(acts.values()), None) or {}).get("env")
        return {"configured": self.configured(), "env": env, "start": bool(d.get("start")), "activities": len(acts), "last": self.last.get(dev)}

    def register(self, dev, body):
        kind, token, env, aid = body.get("kind"), str(body.get("token") or "").lower(), body.get("env"), body.get("id")
        if kind not in ("start", "update") or not TOKEN_RE.match(token) or env not in HOSTS:
            raise LiveError("bad_request", "kind is start or update, token is hex, env is sandbox or production")
        try:
            key = base64.b64decode(str(body.get("key") or ""), validate=True)
        except ValueError:
            key = b""
        if len(key) != 32:
            raise LiveError("bad_request", "key must be 32 bytes, base64")
        if kind == "update" and not (isinstance(aid, str) and ACT_RE.match(aid)):
            raise LiveError("bad_request", "an update token needs the activity id")
        now = int(self.clock())

        def put(devs):
            d = devs.setdefault(dev, {"acts": {}})
            d["key"] = base64.b64encode(key).decode()
            acts = d.setdefault("acts", {})
            if kind == "start":
                d["start"] = {"token": token, "env": env, "at": now}
                return
            if aid in acts:
                acts[aid].update(token=token, env=env)      # Apple rotated the token: same card, keep its age
            else:
                acts[aid] = {"token": token, "env": env, "at": now, "sent": 0, "sig": None, "b": 0}
                for old in sorted(acts, key=lambda k: acts[k]["at"])[:-4]:   # never more than four cards per phone
                    acts.pop(old, None)
            d["auto"] = True                                # the user started a card: the host may keep it going
        self._mutate(put)
        with self.cv:
            self.cv.notify()
        return self.push_info(dev)

    def unregister(self, dev, aid=None):
        def drop(devs):
            d = devs.get(dev)
            if not d:
                return
            if aid:
                d.setdefault("acts", {}).pop(aid, None)
            else:
                d["acts"] = {}
            d["auto"] = False       # the user ended or dismissed the card: do not bring it back until they start one again
        self._mutate(drop)
        return self.push_info(dev)

    def forget(self, who):
        gone = []

        def drop(devs):
            for x in [x for x in devs if x.startswith(who)]:
                devs.pop(x); gone.append(x)
        self._mutate(drop)
        return gone

    def summary(self):
        return [{"device": dev, "start": bool(d.get("start")), "activities": len(d.get("acts") or {}), "auto": d.get("auto", True),
                 "last": self.last.get(dev)} for dev, d in self._load().items()]

    def active(self):
        """Is there any phone this host could push to? On most installs nobody set the card up, and this is one dict lookup."""
        return any((d.get("start") or d.get("acts")) for d in self._load().values()) and self.configured()

    # -- talking to Apple
    def _headers(self, prio, now):
        return {"authorization": "bearer " + self._token(now), "apns-push-type": "liveactivity",
                "apns-topic": self.cfg()["bundle"] + ".push-type.liveactivity", "apns-priority": str(prio),
                "apns-expiration": str(int(now) + 600), "content-type": "application/json"}

    def _http(self, env, token, headers, body):
        """One POST to APNs. The standard library has no HTTP/2, which APNs requires, so this is curl, with the token and body
        on stdin (a curl config) and never on the command line where `ps` could show them."""
        url = "%s/3/device/%s" % ((self.apns_url or HOSTS[env]).rstrip("/"), token)
        q = lambda s: '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
        lines = ["silent", "show-error", "http2", "max-time = 15", 'request = "POST"', "url = " + q(url),
                 'write-out = "\\n%{http_code}\\n%header{apns-id}"']
        lines += ["header = " + q("%s: %s" % kv) for kv in headers.items()]
        lines.append("data-binary = " + q(body))
        try:
            r = subprocess.run(["curl", "-K", "-"], input="\n".join(lines).encode(), capture_output=True, timeout=25)
        except (OSError, subprocess.SubprocessError) as e:
            return 0, "curl: " + type(e).__name__, None
        parts = r.stdout.decode("utf-8", "replace").rsplit("\n", 2)
        if len(parts) != 3 or not parts[1].strip().isdigit():
            return 0, (r.stderr.decode("utf-8", "replace").strip() or "no answer")[:120], None
        reason = None
        if parts[0].strip():
            try:
                reason = (json.loads(parts[0]) or {}).get("reason")
            except (ValueError, AttributeError):
                pass
        return int(parts[1]), reason, parts[2].strip() or None

    def _push(self, ev, dev, token, env, key, status, prio, now):
        try:
            body = build_payload(ev, status, key, now, self.host)
        except LiveError as e:
            self.log("live (%s) to %s: %s" % (ev, dev[:6], e.message))
            return 0, e.code
        code, reason = 0, None
        for attempt in (0, 1):
            try:
                headers = self._headers(prio, now)
            except (LiveError, OSError) as e:
                self.log("live: cannot sign for Apple: %s" % getattr(e, "message", e))
                return 0, "no_key"
            code, reason, _apns_id = self._http(env, token, headers, body)
            if code == 403 and attempt == 0 and reason in ("ExpiredProviderToken", "InvalidProviderToken"):
                self._jwt = None
                continue
            break
        self.last[dev] = {"at": int(now * 1000), "code": code, "why": ev + ((" " + reason) if reason else "")}
        self.log("live (%s) to %s: APNs answered %s%s" % (ev, dev[:6], code, (" " + reason) if reason else ""))
        return code, reason

    def _after(self, ev, dev, aid, code, reason, sig, status, now):
        dead = code == 410 or (code == 400 and reason in ("BadDeviceToken", "DeviceTokenNotForTopic")) or reason in ("Unregistered", "ExpiredToken")

        def apply(devs):
            d = devs.get(dev)
            if not d:
                return
            if ev == "start":
                d["started"] = int(now)
                if dead:
                    d["start"] = None
                return
            a = (d.get("acts") or {}).get(aid)
            if a is None:
                return
            if code == 200:
                if ev == "end":
                    d["acts"].pop(aid, None)
                else:
                    a.update(sent=int(now), sig=sig, b=status["b"], hold=0)
            elif dead:
                d["acts"].pop(aid, None)
                if ev == "update":
                    d["auto"] = False       # Apple says the card is gone: dismissed or expired. Do not fight the person.
            elif code == 429:
                a["hold"] = now + BACKOFF_S
            else:
                a["hold"] = now + 30
        self._mutate(apply)

    # -- deciding what to send
    def submit(self, status):
        with self.cv:
            self._latest = status
            self.cv.notify()

    def run(self):
        while True:
            with self.cv:
                self.cv.wait(timeout=WAKE_S)
                status = self._latest
            if status is None:
                continue
            try:
                self.process(status)
            except Exception as e:   # the card must never take the host down
                self.log("live: %s: %s" % (type(e).__name__, e))

    def process(self, status, now=None):
        """Look at every phone with a card (or a push-to-start token) and send what is due. Returns what was sent, for tests."""
        now = self.clock() if now is None else now
        if not self.configured():
            return []
        sig = status_sig(status)
        keep = self.valid()
        devs = self._load()
        gone = [x for x in devs if keep is not None and x not in keep]
        if gone:                                       # a phone revoked by `shepr revoke`
            self._mutate(lambda ds: [ds.pop(x, None) for x in gone])
            devs = self._load()
        plan = []
        with self.lock:
            for dev, d in devs.items():
                try:
                    key = base64.b64decode(d["key"])
                except (KeyError, ValueError):
                    continue
                acts, st, auto = d.get("acts") or {}, d.get("start"), d.get("auto", True)
                for aid, a in acts.items():
                    if now < a.get("hold", 0):
                        continue
                    if now - a.get("at", now) >= ROLL_S and st and auto:     # Apple's 8 hour limit is near: hand over to a fresh card
                        plan.append(("end", dev, aid, a["token"], a["env"], key, 10))
                        plan.append(("start", dev, None, st["token"], st["env"], key, 10))
                        continue
                    last = a.get("sent", 0)
                    if (a.get("sig") != sig and now - last >= MIN_GAP_S) or now - last >= HEARTBEAT_S:
                        pb = a.get("b", 0)
                        # 10 is immediate and counts toward Apple's hourly budget; 5 does not. Spend 10 only on "someone needs you" changes.
                        plan.append(("update", dev, aid, a["token"], a["env"], key, 10 if (status["b"] > pb or (status["b"] == 0 and pb > 0)) else 5))
                if (st and not acts and auto and d.get("last_b") is not None and status["b"] > d["last_b"]
                        and now - d.get("started", 0) >= START_COOLDOWN_S):
                    plan.append(("start", dev, None, st["token"], st["env"], key, 10))   # a session just blocked and no card is up
        sent = []
        for ev, dev, aid, token, env, key, prio in plan:
            code, reason = self._push(ev, dev, token, env, key, status, prio, now)
            self._after(ev, dev, aid, code, reason, sig, status, now)
            sent.append({"event": ev, "device": dev, "id": aid, "code": code, "reason": reason, "priority": prio})
        b = status["b"]
        if any(d.get("last_b") != b for d in self._load().values()):
            self._mutate(lambda ds: [d.__setitem__("last_b", b) for d in ds.values()])
        return sent

    def test(self, dev=None):
        """Send a plain "everything is connected" card to a phone's running cards, to prove the Apple path works."""
        if not self.configured():
            raise LiveError("not_configured", "the host has no Apple push key yet: run `shepr live setup`")
        now = self.clock()
        status = {"v": 1, "t": int(now), "b": 0, "d": 0, "w": 0, "i": 0, "z": 0, "h": [], "r": [], "m": [],
                  "x": "Test from the host. Everything is connected."}
        out = []
        for dv, d in list(self._load().items()):
            if dev and dv != dev:
                continue
            try:
                key = base64.b64decode(d["key"])
            except (KeyError, ValueError):
                continue
            for aid, a in list((d.get("acts") or {}).items()):
                code, reason = self._push("update", dv, a["token"], a["env"], key, status, 10, now)
                self._after("update", dv, aid, code, reason, None, status, now)   # no signature: the next real status replaces it
                out.append({"device": dv, "id": aid, "code": code, "reason": reason})
        return out
