import QtQuick
import Quickshell
import Quickshell.Io

// Shepr for the Omarchy bar. This service owns every fact the bar badge and the panel show, so the two can never
// disagree. It asks the Shepr CLI that ships in this same folder: `server.py status --json` every few seconds (a
// read-only look at herdr, the paired phones and the Shepr service), and `server.py pair --json --png
// --no-wait` when you ask for a phone to be paired. Everything in the answers is DATA: the panel draws it as plain
// text and never as markup.
Item {
  id: root

  property var settings: ({})

  readonly property string pluginDir: decodeURIComponent(String(Qt.resolvedUrl(".")).replace(/^file:\/\/(localhost)?/, ""))
  readonly property string readmeUrl: "https://github.com/nixfred/shepr#use-it"

  readonly property int intervalSec: {
    var n = parseInt(String(settings && settings.intervalSec !== undefined ? settings.intervalSec : 8), 10)
    return isFinite(n) ? Math.max(3, Math.min(60, n)) : 8
  }

  // ---- what `shepr status` last said
  property bool ready: false
  property bool running: false
  property string url: ""
  property string build: ""
  property int devices: 0
  property var units: []
  property bool herdr: false
  property string herdrError: ""
  property var counts: ({})
  property var waiting: []
  property string lastError: ""
  property double updatedAt: 0
  readonly property int blocked: counts && counts.blocked ? counts.blocked : 0

  function count(state) { return counts && counts[state] ? counts[state] : 0 }

  function apply(text) {
    var d
    try { d = JSON.parse(text) } catch (e) { lastError = "status was not JSON"; return }
    if (!d || typeof d !== "object") return
    lastError = ""
    running = d.running === true
    url = typeof d.url === "string" ? d.url : ""
    build = typeof d.build === "string" ? d.build : ""
    herdr = d.herdr === true
    herdrError = typeof d.herdr_error === "string" ? d.herdr_error : ""
    counts = d.counts || ({})
    waiting = Array.isArray(d.waiting) ? d.waiting : []
    units = Array.isArray(d.units) ? d.units : []
    var n = parseInt(d.devices, 10)
    // A phone that paired while a code was on screen ends the pairing and says so.
    if (pairing && isFinite(n) && n > devicesWhenPairing) { pairedNote = "Phone paired"; endPairing() }
    devices = isFinite(n) ? n : 0
    updatedAt = Date.now()
    ready = true
  }

  function refresh() { if (!statusProc.running) statusProc.running = true }

  Timer { interval: root.intervalSec * 1000; running: true; repeat: true; triggeredOnStart: true; onTriggered: root.refresh() }

  Process {
    id: statusProc
    command: ["python3", root.pluginDir + "server.py", "status", "--json"]
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: root.apply(text) }
    stderr: StdioCollector { waitForEnd: true; onStreamFinished: if (text.trim() !== "") root.lastError = text.trim().split("\n").slice(-1)[0] }
  }

  // ---- pairing a phone
  property bool pairing: false
  property bool pairBusy: false
  property string pairCode: ""
  property string pairImage: ""
  property double pairExpires: 0
  property double now: Date.now() / 1000
  property string pairError: ""
  property string pairStderr: ""
  property int pairExit: 0
  property string pairedNote: ""
  property int devicesWhenPairing: 0
  property int pairSerial: 0

  readonly property int pairSecondsLeft: pairing ? Math.max(0, Math.round(pairExpires - now)) : 0

  function startPairing() {
    if (pairBusy || pairing || !running) return
    pairError = ""; pairedNote = ""; pairStderr = ""; pairExit = 0; pairBusy = true; devicesWhenPairing = devices
    pairProc.running = true
  }

  function endPairing() {
    pairing = false; pairCode = ""; pairImage = ""; pairExpires = 0
    cancelCode.running = true   // drops the code, so a cancelled code cannot be redeemed, and removes the QR picture that holds it
  }

  function applyPair(text) {
    var d
    try { d = JSON.parse(text) } catch (e) { pairError = "pairing printed something unexpected"; return }
    if (!d || typeof d.code !== "string" || typeof d.expires !== "number") { pairError = "pairing printed something unexpected"; return }
    pairCode = d.code
    pairExpires = d.expires
    now = Date.now() / 1000
    pairSerial += 1
    pairImage = d.png ? "file://" + String(d.png).split("/").map(encodeURIComponent).join("/") + "?n=" + pairSerial : ""
    pairing = true
  }

  Process {
    id: pairProc
    // --png with no file name: the CLI draws the picture itself, owner-only, in a private directory it has verified
    // (never /tmp), and the one-time code travels to qrencode over stdin, never in an argument.
    command: ["python3", root.pluginDir + "server.py", "pair", "--png", "--json", "--no-wait", "--ttl", "180"]
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: root.applyPair(text) }
    // stderr also carries harmless notes (for example that Tailscale is absent), so it only counts as THE reason
    // when the command failed.
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.pairStderr = text.trim().split("\n").slice(-1)[0].replace(/^shepr: /, "")
        if (root.pairExit !== 0 && !root.pairing && root.pairStderr !== "") root.pairError = root.pairStderr   // stderr can arrive after the exit
      }
    }
    onExited: function(code) {
      root.pairExit = code
      root.pairBusy = false
      if (code !== 0 && !root.pairing) root.pairError = root.pairStderr !== "" ? root.pairStderr : "pairing failed"
    }
  }

  Process { id: cancelCode; command: ["python3", root.pluginDir + "server.py", "pair", "--cancel"] }

  Timer {
    interval: 1000; running: root.pairing; repeat: true
    onTriggered: { root.now = Date.now() / 1000; if (root.now >= root.pairExpires) root.endPairing() }
  }

  // ---- small actions, each limited to the one thing it says
  function openUrl(u) { if (/^https?:\/\/[^\s]+$/.test(String(u))) Quickshell.execDetached(["xdg-open", String(u)]) }
  function startUnit(name) {
    if (!/^[A-Za-z0-9_.@-]+$/.test(String(name))) return
    Quickshell.execDetached(["systemctl", "--user", "start", String(name) + ".service"])
    afterStart.restart()
  }
  Timer { id: afterStart; interval: 2500; onTriggered: root.refresh() }
}
