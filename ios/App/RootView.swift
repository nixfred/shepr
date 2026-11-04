import SwiftUI
import ShepLiveKit

struct RootView: View {
    @Environment(AppModel.self) private var model
    @State private var scanning = false
    @State private var typed = ""

    var body: some View {
        ZStack {
            Theme.bg.ignoresSafeArea()
            VStack(alignment: .leading, spacing: 12) {
                header
                if model.link == nil { pairing } else { paired }
                if !model.message.isEmpty {
                    Text(model.message).font(.system(size: 12, weight: .medium)).foregroundStyle(Theme.hot)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
            }
            .padding(16)
        }
        .sheet(isPresented: $scanning) {
            ZStack(alignment: .topTrailing) {
                QRScanner { code in scanning = false; Task { await model.pair(from: code) } }.ignoresSafeArea()
                Button("Cancel") { scanning = false }.buttonStyle(.borderedProminent).tint(Theme.card).padding(16)
            }
        }
    }

    private var header: some View {
        HStack {
            Text("SHEPR LIVE").font(.system(size: 13, weight: .heavy)).tracking(2).foregroundStyle(Theme.accent)
            Spacer()
            if let l = model.link { Text("paired to \(l.host)").font(.system(size: 12, weight: .medium, design: .monospaced)).foregroundStyle(Theme.ok) }
            else { Text("not paired").font(.system(size: 12, weight: .medium, design: .monospaced)).foregroundStyle(Theme.dim) }
        }
    }

    private var pairing: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Put Shepr on your Lock Screen").font(.system(size: 22, weight: .bold)).foregroundStyle(Theme.text)
            Text("On the machine that runs Shepr, run `shepr pair`. Then scan the QR code it shows, or paste the link under it.")
                .font(.system(size: 14)).foregroundStyle(Theme.dim).fixedSize(horizontal: false, vertical: true)
            Button { scanning = true } label: { Label("Scan the QR code", systemImage: "qrcode.viewfinder").frame(maxWidth: .infinity) }
                .buttonStyle(.borderedProminent).tint(Theme.accent).foregroundStyle(.black).controlSize(.large)
            HStack {
                TextField("https://…/#shepr-pair=…", text: $typed).textInputAutocapitalization(.never).autocorrectionDisabled()
                    .font(.system(size: 13, design: .monospaced)).padding(10).background(Theme.card).clipShape(RoundedRectangle(cornerRadius: 10))
                Button("Pair") { Task { await model.pair(from: typed) } }.buttonStyle(.bordered).tint(Theme.accent).disabled(model.busy || typed.isEmpty)
            }
            Text("Your iPhone must be on your tailnet, the same as the Shepr web app.").font(.system(size: 12)).foregroundStyle(Theme.dim)
        }
    }

    private var paired: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("ON YOUR LOCK SCREEN").font(.system(size: 10, weight: .bold)).tracking(1.5).foregroundStyle(Theme.dim)
            LockScreenView(status: model.status, isStale: model.activityState == "stale")
                .background(Theme.bg).clipShape(RoundedRectangle(cornerRadius: 18))
                .overlay(RoundedRectangle(cornerRadius: 18).stroke(Theme.line, lineWidth: 1))
            HStack(spacing: 10) {
                Button { Task { await model.start() } } label: { Label(model.activityState == "off" ? "Start" : "Restart", systemImage: "play.fill").frame(maxWidth: .infinity) }
                    .buttonStyle(.borderedProminent).tint(Theme.accent).foregroundStyle(.black).controlSize(.large)
                Button { Task { await model.stop() } } label: { Label("Stop", systemImage: "stop.fill").frame(maxWidth: .infinity) }
                    .buttonStyle(.bordered).tint(Theme.hot).controlSize(.large).disabled(model.activityState == "off")
            }
            infoCard
            HStack {
                Button("Open Shepr") { model.openShepr() }.buttonStyle(.bordered).tint(Theme.accent)
                Spacer()
                Button("Unpair", role: .destructive) { Task { await model.unpair() } }.buttonStyle(.bordered)
            }
        }
    }

    private var infoCard: some View {
        VStack(alignment: .leading, spacing: 6) {
            line("Lock Screen card", model.activityState == "off" ? "not running" : (model.activityState == "stale" ? "running, stale" : "running"))
            line("Updates", updates)
            if let t = model.lastContact { HStack { Text("Last contact").foregroundStyle(Theme.dim); Spacer(); Text(t, style: .time).foregroundStyle(Theme.text).monospacedDigit() }.font(.system(size: 12.5)) }
        }
        .padding(12).frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.card).clipShape(RoundedRectangle(cornerRadius: 12))
    }

    private var updates: String {
        guard model.pushEnabled else { return "from this app while it is open" }
        guard let p = model.push else { return "waiting for the host" }
        if !p.configured { return "host has no Apple push key yet" }
        if p.activities > 0 { return "pushed by the host" }
        return p.start ? "ready: the host can start it" : "waiting for Apple's token"
    }

    private func line(_ k: String, _ v: String) -> some View {
        HStack { Text(k).foregroundStyle(Theme.dim); Spacer(); Text(v).foregroundStyle(Theme.text).multilineTextAlignment(.trailing) }.font(.system(size: 12.5))
    }
}
