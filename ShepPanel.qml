import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import qs.Commons
import qs.Ui

// LAW 17: everything on one screen. The only thing that can scroll is the waiting list, in place, inside its own
// bounded box, and only when more sessions are waiting than there are rows. Everything shown that came from herdr
// (project names, titles) is drawn as plain text.
Panel {
  id: panel
  moduleName: "nixfred.shepr"
  // IPC target so scripts can open it: omarchy-shell nixfred.shepr toggle
  ipcTarget: "nixfred.shepr"

  required property var widget
  readonly property var svc: widget.svc

  readonly property color foreground: widget.bar ? widget.bar.foreground : Color.foreground
  readonly property color dim: Util.alpha(foreground, 0.62)
  readonly property color faint: Util.alpha(foreground, 0.10)
  readonly property color warnColor: "#f5a623"
  readonly property color downColor: "#ff4d5e"
  readonly property string fontFamily: widget.bar ? widget.bar.fontFamily : Style.font.family
  readonly property string mono: "monospace"

  readonly property int panelWidth: Style.space(560)
  readonly property int rowH: Style.space(40)
  readonly property int maxRows: 4
  readonly property int qrSize: Style.space(176)

  readonly property bool up: svc && svc.ready && svc.running
  readonly property bool pairing: svc && svc.pairing

  // The keyboard cursor walks the buttons that exist right now (arrows or Tab), Enter presses the one it is on.
  readonly property bool noHerdr: svc && svc.ready && !svc.herdr
  readonly property bool stopped: svc && svc.ready && svc.herdr && !svc.running
  readonly property var actionIds: {
    var a = []
    if (noHerdr) a.push("herdr")
    if (stopped) a.push("fix")
    if (up) { if (pairing) a.push("cancel"); else a.push("pair", "open") }
    return a
  }
  property int cursor: 0
  onActionIdsChanged: cursor = Math.max(0, Math.min(cursor, actionIds.length - 1))
  function cursorIs(id) { return opened && actionIds[cursor] === id }
  function hoverOn(id, h) { if (h && actionIds.indexOf(id) >= 0) cursor = actionIds.indexOf(id) }
  function step(d) { if (actionIds.length > 0) cursor = (cursor + d + actionIds.length) % actionIds.length }
  function activate(id) {
    if (!svc) return
    if (id === "herdr") svc.openUrl("https://herdr.dev")
    else if (id === "fix") { if (svc.units.length > 0) svc.startUnit(svc.units[0]); else svc.openUrl(svc.readmeUrl) }
    else if (id === "pair") svc.startPairing()
    else if (id === "cancel") svc.endPairing()
    else if (id === "open") svc.openUrl(svc.url)
  }

  function pad2(n) { return n < 10 ? "0" + n : String(n) }
  function clock(s) { return Math.floor(s / 60) + ":" + pad2(s % 60) }

  KeyboardPanel {
    id: kpanel
    anchorItem: panel.widget.anchorItem
    owner: panel.widget
    bar: panel.widget.bar
    open: panel.opened
    focusTarget: keyCatcher
    contentWidth: kpanel.fittedContentWidth(panel.panelWidth)
    contentHeight: kpanel.fittedContentHeight(content.implicitHeight, Style.space(640))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: panel.widget.close()
      onMoveRequested: function(dx, dy) { panel.step(dx !== 0 ? dx : dy) }
      onTabRequested: function(direction) { panel.step(direction) }
      onActivateRequested: panel.activate(panel.actionIds[panel.cursor])

      ColumnLayout {
        id: content
        width: parent.width
        spacing: Style.space(10)

        // ---------------------------------------------------------- header
        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(10)
          Rectangle {
            width: Style.space(12); height: width; radius: width / 2; border.width: 0
            color: !panel.up ? panel.faint : svc.blocked > 0 ? panel.warnColor : Color.accent
          }
          Text {
            text: "SHEPR"
            color: panel.foreground
            font.family: panel.fontFamily
            font.pixelSize: Style.font.subtitle
            font.bold: true
            font.letterSpacing: 2
          }
          Text {
            text: svc && svc.ready ? svc.build : ""
            textFormat: Text.PlainText
            color: panel.dim
            font.family: panel.mono
            font.pixelSize: Style.font.bodySmall
          }
          Item { Layout.fillWidth: true }
          Text {
            text: !svc || !svc.ready ? "reading status" : panel.up ? "running" : "not running"
            color: panel.up ? Color.accent : panel.dim
            font.family: panel.mono
            font.pixelSize: Style.font.bodySmall
          }
        }

        // ---------------------------------------------------- problem banner
        // herdr missing or Shepr stopped: say what, and give the one thing to do about it.
        Rectangle {
          id: banner
          readonly property bool noHerdr: panel.noHerdr
          readonly property bool stopped: panel.stopped
          visible: noHerdr || stopped
          Layout.fillWidth: true
          implicitHeight: bannerRow.implicitHeight + Style.space(20)
          radius: Style.space(4)
          color: Util.alpha(panel.warnColor, 0.10)
          border.width: 1
          border.color: Util.alpha(panel.warnColor, 0.6)
          RowLayout {
            id: bannerRow
            anchors.fill: parent
            anchors.margins: Style.space(10)
            spacing: Style.space(10)
            Text {
              Layout.fillWidth: true
              wrapMode: Text.WordWrap
              textFormat: Text.PlainText
              color: panel.foreground
              font.family: panel.fontFamily
              font.pixelSize: Style.font.body
              text: banner.noHerdr
                ? "herdr is not available here (" + (svc.herdrError || "not installed") + "). Shepr shows herdr's agent sessions, so it needs herdr on this machine."
                : svc && svc.units.length > 0
                  ? "Shepr is not running on this machine."
                  : "Shepr is not installed as a service here. The README has the install steps (Use it)."
            }
            Button {
              visible: banner.stopped
              bordered: true
              hasCursor: panel.cursorIs("fix")
              text: svc && svc.units.length > 0 ? "Start Shepr" : "Open README"
              onHovered: function(h) { panel.hoverOn("fix", h) }
              onClicked: panel.activate("fix")
            }
            Button {
              visible: banner.noHerdr
              bordered: true
              hasCursor: panel.cursorIs("herdr")
              text: "herdr.dev"
              onHovered: function(h) { panel.hoverOn("herdr", h) }
              onClicked: panel.activate("herdr")
            }
          }
        }

        // ------------------------------------------------------ state chips
        RowLayout {
          Layout.fillWidth: true
          visible: svc && svc.ready && svc.herdr
          spacing: Style.space(8)
          Repeater {
            model: [
              { k: "blocked", t: "WAITING" },
              { k: "working", t: "WORKING" },
              { k: "idle",    t: "IDLE" },
              { k: "done",    t: "DONE" }
            ]
            delegate: Rectangle {
              required property var modelData
              readonly property int n: svc ? svc.count(modelData.k) : 0
              readonly property bool hot: modelData.k === "blocked" && n > 0
              Layout.fillWidth: true
              implicitHeight: Style.space(44)
              radius: Style.space(4)
              color: hot ? Util.alpha(panel.warnColor, 0.12) : panel.faint
              border.width: 1
              border.color: hot ? Util.alpha(panel.warnColor, 0.7) : Util.alpha(panel.foreground, 0.12)
              Column {
                anchors.centerIn: parent
                spacing: Style.space(1)
                Text {
                  anchors.horizontalCenter: parent.horizontalCenter
                  text: String(parent.parent.n)
                  color: parent.parent.hot ? panel.warnColor : panel.foreground
                  font.family: panel.mono
                  font.pixelSize: Style.font.subtitle
                  font.bold: true
                }
                Text {
                  anchors.horizontalCenter: parent.horizontalCenter
                  text: modelData.t
                  color: panel.dim
                  font.family: panel.mono
                  font.pixelSize: Style.font.bodySmall - 1
                  font.letterSpacing: 1
                }
              }
            }
          }
        }

        // ------------------------------------------------------ waiting list
        Text {
          visible: svc && svc.ready && svc.herdr
          text: "WAITING ON YOU"
          color: panel.dim
          font.family: panel.mono
          font.pixelSize: Style.font.bodySmall
          font.letterSpacing: 1
        }
        // Bounded box: grows with the rows up to maxRows, then scrolls in place.
        ListView {
          id: list
          visible: svc && svc.ready && svc.herdr && count > 0
          Layout.fillWidth: true
          Layout.preferredHeight: Math.min(count, panel.maxRows) * (panel.rowH + spacing)
          clip: true
          spacing: Style.space(3)
          interactive: count > panel.maxRows
          boundsBehavior: Flickable.StopAtBounds
          model: svc ? svc.waiting : []

          delegate: Rectangle {
            required property var modelData
            width: ListView.view.width
            height: panel.rowH
            radius: Style.space(3)
            color: panel.faint
            RowLayout {
              anchors.fill: parent
              anchors.leftMargin: Style.space(10)
              anchors.rightMargin: Style.space(10)
              spacing: Style.space(10)
              Rectangle { width: Style.space(8); height: width; radius: width / 2; color: panel.warnColor; border.width: 0 }
              Text {
                Layout.preferredWidth: Style.space(150)
                text: modelData.project || "?"
                textFormat: Text.PlainText
                elide: Text.ElideRight
                color: panel.foreground
                font.family: panel.fontFamily
                font.pixelSize: Style.font.body
                font.bold: true
              }
              Text {
                Layout.fillWidth: true
                text: modelData.title || ""
                textFormat: Text.PlainText
                elide: Text.ElideRight
                color: panel.dim
                font.family: panel.fontFamily
                font.pixelSize: Style.font.body
              }
              Text {
                text: modelData.agent || ""
                textFormat: Text.PlainText
                color: panel.dim
                font.family: panel.mono
                font.pixelSize: Style.font.bodySmall
              }
            }
          }
        }
        Text {
          visible: svc && svc.ready && svc.herdr && list.count === 0
          text: "Nothing is waiting on you."
          color: panel.dim
          font.family: panel.fontFamily
          font.pixelSize: Style.font.body
        }

        // ------------------------------------------------------------ phone
        Rectangle {
          visible: panel.up
          Layout.fillWidth: true
          implicitHeight: phoneRow.implicitHeight + Style.space(24)
          radius: Style.space(4)
          color: "transparent"
          border.width: 1
          border.color: Util.alpha(panel.foreground, 0.18)

          RowLayout {
            id: phoneRow
            anchors.fill: parent
            anchors.margins: Style.space(12)
            spacing: Style.space(14)

            // the one-time pairing picture, shown only while a code is live
            Rectangle {
              visible: panel.pairing && svc.pairImage !== ""
              Layout.preferredWidth: panel.qrSize + Style.space(12)
              Layout.preferredHeight: panel.qrSize + Style.space(12)
              color: "#ffffff"
              radius: Style.space(3)
              Image {
                anchors.centerIn: parent
                width: panel.qrSize; height: panel.qrSize
                source: svc ? svc.pairImage : ""
                cache: false
                smooth: false
                fillMode: Image.PreserveAspectFit
              }
            }

            ColumnLayout {
              Layout.fillWidth: true
              spacing: Style.space(6)

              Text {
                text: panel.pairing ? "Scan this with Shepr on your iPhone" : "PHONE"
                color: panel.pairing ? panel.foreground : panel.dim
                font.family: panel.pairing ? panel.fontFamily : panel.mono
                font.pixelSize: panel.pairing ? Style.font.body : Style.font.bodySmall
                font.letterSpacing: panel.pairing ? 0 : 1
                font.bold: panel.pairing
              }
              Text {
                visible: panel.pairing
                text: "In Shepr tap Scan QR, or type the code by hand."
                color: panel.dim
                font.family: panel.fontFamily
                font.pixelSize: Style.font.bodySmall
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
              }
              Text {
                visible: panel.pairing
                text: svc ? svc.pairCode : ""
                textFormat: Text.PlainText
                color: panel.warnColor
                font.family: panel.mono
                font.pixelSize: Style.font.subtitle
                font.bold: true
              }
              Text {
                visible: panel.pairing
                text: svc ? "valid " + panel.clock(svc.pairSecondsLeft) + ", works once" : ""
                color: panel.dim
                font.family: panel.mono
                font.pixelSize: Style.font.bodySmall
              }

              Text {
                visible: !panel.pairing
                Layout.fillWidth: true
                text: svc ? svc.url : ""
                textFormat: Text.PlainText
                elide: Text.ElideMiddle
                color: panel.foreground
                font.family: panel.mono
                font.pixelSize: Style.font.body
              }
              Text {
                visible: !panel.pairing
                text: svc ? (svc.pairedNote !== "" ? svc.pairedNote + ". " : "") + svc.devices + (svc.devices === 1 ? " phone is" : " phones are") + " paired."
                            : ""
                color: svc && svc.pairedNote !== "" ? Color.accent : panel.dim
                font.family: panel.fontFamily
                font.pixelSize: Style.font.bodySmall
              }
              Text {
                visible: !panel.pairing && svc && svc.pairError !== ""
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: svc ? svc.pairError : ""
                textFormat: Text.PlainText
                color: panel.downColor
                font.family: panel.fontFamily
                font.pixelSize: Style.font.bodySmall
              }

              RowLayout {
                spacing: Style.space(8)
                Button {
                  visible: !panel.pairing
                  bordered: true
                  hasCursor: panel.cursorIs("pair")
                  text: svc && svc.pairBusy ? "Making a code" : "Pair a phone"
                  onHovered: function(h) { panel.hoverOn("pair", h) }
                  onClicked: panel.activate("pair")
                }
                Button {
                  visible: panel.pairing
                  bordered: true
                  hasCursor: panel.cursorIs("cancel")
                  text: "Cancel"
                  onHovered: function(h) { panel.hoverOn("cancel", h) }
                  onClicked: panel.activate("cancel")
                }
                Button {
                  visible: !panel.pairing
                  bordered: true
                  hasCursor: panel.cursorIs("open")
                  text: "Open Shepr"
                  onHovered: function(h) { panel.hoverOn("open", h) }
                  onClicked: panel.activate("open")
                }
              }
            }
          }
        }

        // ------------------------------------------------------------ footer
        Text {
          Layout.fillWidth: true
          text: "arrows or Tab move  |  Enter presses  |  Esc closes"
          color: panel.dim
          horizontalAlignment: Text.AlignRight
          font.family: panel.mono
          font.pixelSize: Style.font.bodySmall - 1
        }
      }
    }
  }
}
