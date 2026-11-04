import SwiftUI
import ActivityKit
import UIKit
import ShepLiveKit

/// What /api/live/status answers: the status to draw, and what the host knows about pushing to this phone.
struct LiveEnvelope: Decodable {
    struct Push: Decodable {
        struct Last: Decodable { var at: Int; var code: Int; var why: String }
        var configured: Bool
        var env: String?
        var start: Bool
        var activities: Int
        var last: Last?
    }
    var status: LiveStatus
    var push: Push
    var host: String
}

@MainActor @Observable
final class AppModel {
    struct Link: Codable, Equatable { var base: URL; var device: String; var host: String }

    var link: Link?
    var status: LiveStatus = .note("Not paired yet")
    var push: LiveEnvelope.Push?
    var message = ""
    var busy = false
    var activityState = "off"
    var lastContact: Date?

    /// Set per build configuration. Free builds cannot receive pushes, so they never ask ActivityKit for push tokens.
    let pushEnabled = (Bundle.main.object(forInfoDictionaryKey: "ShepPushEnabled") as? String) == "YES"
    let env = (Bundle.main.object(forInfoDictionaryKey: "ShepAPNsEnv") as? String) ?? "sandbox"

    private var client: ShepClient?
    private var loop: Task<Void, Never>?
    private var tracked = Set<String>()
    private var watching = false
    private var lastSent: LiveStatus?
    private let defaults = UserDefaults.standard

    // MARK: start up

    func boot() async {
        // Testing aid: `xcrun simctl launch <device> com.nixfred.shepr.live -shepDemo` shows a busy made-up status and starts a card, with no host.
        if ProcessInfo.processInfo.arguments.contains("-shepDemo") {
            link = Link(base: URL(string: "https://gus.example.ts.net:8742")!, device: "demo", host: "gus")
            status = .busy
            await start()
            if ProcessInfo.processInfo.arguments.contains("-shepDemoAlert") {   // also shows the Lock Screen presentation as a banner on a device without a Dynamic Island
                var bg = UIBackgroundTaskIdentifier.invalid
                bg = UIApplication.shared.beginBackgroundTask { UIApplication.shared.endBackgroundTask(bg) }   // keeps the app running while it sits in the background
                try? await Task.sleep(for: .seconds(4))
                let content = ActivityContent(state: ShepActivityAttributes.ContentState(status: .busy), staleDate: nil)
                for a in Activity<ShepActivityAttributes>.activities {
                    await a.update(content, alertConfiguration: AlertConfiguration(title: "Shepr", body: "Demo update", sound: .default))
                }
                UIApplication.shared.endBackgroundTask(bg)
            }
            return
        }
        loadLink()
        guard link != nil else { return }
        await refresh()
        watchActivities()
        syncActivityState()
    }

    func phaseChanged(_ p: ScenePhase) {
        guard link != nil else { return }
        if p == .active {
            startLoop()
            Task { [self] in await refresh() }
        } else {
            loop?.cancel(); loop = nil
        }
    }

    private func startLoop() {
        loop?.cancel()
        loop = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(5))
                if Task.isCancelled { break }
                await self?.refresh()
            }
        }
    }

    // MARK: pairing

    private func loadLink() {
        guard let d = defaults.data(forKey: "shepr.live.link"), let l = try? JSONDecoder().decode(Link.self, from: d),
              let raw = Vault.deviceStore().get("device"), let key = try? DeviceKey(rawRepresentation: raw) else { return }
        link = l
        client = ShepClient(base: l.base, key: key)
    }

    func pair(from text: String) async {
        guard let pl = PairLink.parse(text) else { message = "That is not a Shepr pairing code. Run `shepr pair` on the host and scan its QR."; return }
        guard pl.base.scheme == "https" || pl.base.host == "localhost" else { message = "Shepr Live only talks to an https host."; return }
        busy = true; defer { busy = false }
        let key = DeviceKey()
        let c = ShepClient(base: pl.base, key: key)
        do {
            let r = try await c.pair(code: pl.code, name: "Shepr Live")
            try Vault.deviceStore().set(key.rawRepresentation, for: "device")
            let l = Link(base: pl.base, device: r.device, host: r.host)
            defaults.set(try JSONEncoder().encode(l), forKey: "shepr.live.link")
            link = l; client = c; message = ""
            await refresh()
            watchActivities()
            startLoop()
        } catch { message = "\(error)" }
    }

    func unpair() async {
        await stop()
        loop?.cancel(); loop = nil
        Vault.deviceStore().remove("device")
        defaults.removeObject(forKey: "shepr.live.link")
        link = nil; client = nil; push = nil
        status = .note("Not paired yet")
    }

    // MARK: status

    func refresh() async {
        guard let client else { return }
        do {
            let env = try JSONDecoder().decode(LiveEnvelope.self, from: try await client.get("/api/live/status"))
            status = env.status; push = env.push; lastContact = Date(); message = ""
            await updateActivities(env.status)
        } catch { message = "\(error)" }
    }

    private func staleDate() -> Date { Date().addingTimeInterval(150) }

    private func updateActivities(_ s: LiveStatus) async {
        guard s != lastSent else { return }
        lastSent = s
        let content = ActivityContent(state: ShepActivityAttributes.ContentState(status: s), staleDate: staleDate())
        for a in Activity<ShepActivityAttributes>.activities { await a.update(content) }
    }

    // MARK: the Live Activity

    func start() async {
        guard ActivityAuthorizationInfo().areActivitiesEnabled else {
            message = "Live Activities are off for Shepr Live. Turn them on in Settings."; return
        }
        for a in Activity<ShepActivityAttributes>.activities { await a.end(nil, dismissalPolicy: .immediate) }
        do {
            let act = try Activity<ShepActivityAttributes>.request(
                attributes: ShepActivityAttributes(host: link?.host ?? "shepr"),
                content: ActivityContent(state: ShepActivityAttributes.ContentState(status: status), staleDate: staleDate()),
                pushType: pushEnabled ? PushType.token : nil)
            lastSent = status
            track(act); message = ""
        } catch { message = "Could not start: \(error.localizedDescription)" }
        syncActivityState()
    }

    func stop() async {
        for a in Activity<ShepActivityAttributes>.activities { await a.end(nil, dismissalPolicy: .immediate) }
        await unregister(nil)
        syncActivityState()
    }

    func syncActivityState() {
        guard let a = Activity<ShepActivityAttributes>.activities.first else { activityState = "off"; return }
        switch a.activityState {
        case .active: activityState = "on"
        case .stale: activityState = "stale"
        default: activityState = "off"
        }
    }

    // MARK: push tokens (only when the build can receive pushes)

    private func watchActivities() {
        for a in Activity<ShepActivityAttributes>.activities { track(a) }
        guard pushEnabled, !watching else { return }
        watching = true
        Task { [self] in for await t in Activity<ShepActivityAttributes>.pushToStartTokenUpdates { await register(kind: "start", token: t, id: nil) } }
        Task { [self] in for await a in Activity<ShepActivityAttributes>.activityUpdates { track(a) } }
    }

    private func track(_ a: Activity<ShepActivityAttributes>) {
        guard tracked.insert(a.id).inserted else { return }
        if pushEnabled { Task { [self] in for await t in a.pushTokenUpdates { await register(kind: "update", token: t, id: a.id) } } }
        Task { [self] in
            for await s in a.activityStateUpdates {
                syncActivityState()
                if s == .ended || s == .dismissed { await unregister(a.id); _ = tracked.remove(a.id) }
            }
        }
    }

    private func register(kind: String, token: Data, id: String?) async {
        guard let client, let key = Vault.sealKey(createIfMissing: true) else { return }
        var body: [String: Any] = ["kind": kind, "token": token.hex, "env": env, "key": key.base64EncodedString()]
        if let id { body["id"] = id }
        do { push = try JSONDecoder().decode(LiveEnvelope.Push.self, from: try await client.post("/api/live/register", body)) }
        catch { message = "\(error)" }
    }

    private func unregister(_ id: String?) async {
        guard let client else { return }
        _ = try? await client.post("/api/live/unregister", id.map { ["id": $0] } ?? [:])
    }

    func openShepr() {
        if let u = link?.base { UIApplication.shared.open(u) }
    }
}
