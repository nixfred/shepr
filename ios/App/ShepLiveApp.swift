import SwiftUI
import ShepLiveKit

@main
struct ShepLiveApp: App {
    @State private var model = AppModel()
    @Environment(\.scenePhase) private var phase

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(model)
                .preferredColorScheme(.dark)
                .task { await model.boot() }
                .onChange(of: phase) { _, p in model.phaseChanged(p) }
        }
    }
}
