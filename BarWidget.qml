import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

// A dot and a number: how many agent sessions are waiting on you. Theme accent = nothing waiting, fixed amber =
// something is waiting, dim = Shepr or herdr is not there. (Some themes map "urgent" to green, so the warning
// colour is fixed and never derived from the theme.)
BarWidget {
  id: root
  moduleName: "nixfred.shepr"
  property var anchorItem: button

  readonly property var svc: bar && bar.shell ? bar.shell.serviceFor(moduleName) : null
  readonly property bool ready: svc ? svc.ready : false
  readonly property bool running: svc ? svc.running : false
  readonly property bool herdr: svc ? svc.herdr : false
  readonly property int blocked: svc ? svc.blocked : 0

  function setting(name, fallback) {
    var v = settings ? settings[name] : undefined
    return v === undefined ? fallback : v
  }
  readonly property bool showCount: String(setting("showCount", true)) !== "false"
  readonly property color foreground: bar ? bar.foreground : Color.foreground

  readonly property color dotColor: !ready || !running || !herdr ? Util.alpha(foreground, 0.3)
    : blocked > 0 ? "#f5a623"
    : Color.accent

  readonly property string label: !ready ? "--" : !running ? "off" : !herdr ? "--" : blocked > 0 ? String(blocked) : "0"

  readonly property real contentWidth: Style.space(showCount ? 52 : 24)
  implicitWidth: vertical ? barSize : contentWidth
  implicitHeight: vertical ? contentWidth : barSize

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: ""
    labelVisible: false
    hasVisualContent: true
    active: false
    useActiveColor: false
    tooltipText: {
      var s = root.svc
      if (!s || !s.ready) return "Shepr: reading status"
      if (!s.herdr) return "Shepr: herdr is not available here"
      if (!s.running) return "Shepr is not running on this machine"
      var w = s.blocked === 0 ? "Nothing is waiting on you" : s.blocked + (s.blocked === 1 ? " session is" : " sessions are") + " waiting on you"
      return "Shepr  " + w + "\n" + s.devices + (s.devices === 1 ? " phone" : " phones") + " paired"
    }

    Row {
      anchors.centerIn: parent
      spacing: Style.space(5)

      Item {
        width: Style.space(14); height: width
        anchors.verticalCenter: parent.verticalCenter
        Rectangle {
          anchors.centerIn: parent
          width: parent.width; height: width; radius: width / 2
          color: root.dotColor; border.width: 0; opacity: 0.22
        }
        Rectangle {
          anchors.centerIn: parent
          width: parent.width * 0.6; height: width; radius: width / 2
          color: root.dotColor; border.width: 0
          Behavior on color { ColorAnimation { duration: 300 } }
        }
      }

      Text {
        visible: root.showCount
        anchors.verticalCenter: parent.verticalCenter
        text: root.label
        textFormat: Text.PlainText
        color: root.ready && root.running && root.herdr && root.blocked > 0 ? "#f5a623" : root.foreground
        font.family: root.bar ? root.bar.fontFamily : Style.font.family
        font.pixelSize: Style.font.bodySmall
        font.bold: root.blocked > 0
      }
    }

    onPressed: function(code) {
      if (root.bar) root.bar.hideTooltip(root)
      root.toggle()
    }
  }

  readonly property bool opened: panel.opened
  function open() { panel.controller.show() }
  function close() { panel.controller.hide() }
  function toggle() { opened ? close() : open() }
  function closeForPopoutSwitch() { close() }
  readonly property bool popoutSwitchClosing: false

  ShepPanel {
    id: panel
    widget: root
  }
}
