// swift-tools-version:6.0
import PackageDescription

// Shepr Live: the optional native companion that puts Shepr's status on the iPhone Lock Screen as a Live Activity.
// Apple frameworks only. No third-party code, the same as the web app.
//   ShepLiveKit        the status model, sealed payloads, request signing, the Lock Screen views (iOS and macOS)
//   shepr-live-render  draws the Lock Screen views to PNG and checks Apple's 160 pt height limit (macOS)
//   shepr-live-probe   pairs with a Shepr host and calls its live endpoints, to test the real wire format (macOS)
let package = Package(
    name: "ShepLive",
    platforms: [.iOS("18.0"), .macOS("14.0")],
    products: [
        .library(name: "ShepLiveKit", targets: ["ShepLiveKit"]),
        .executable(name: "shepr-live-render", targets: ["ShepLiveRender"]),
        .executable(name: "shepr-live-probe", targets: ["ShepLiveProbe"]),
    ],
    targets: [
        .target(name: "ShepLiveKit"),
        .executableTarget(name: "ShepLiveRender", dependencies: ["ShepLiveKit"]),
        .executableTarget(name: "ShepLiveProbe", dependencies: ["ShepLiveKit"]),
        .testTarget(name: "ShepLiveKitTests", dependencies: ["ShepLiveKit"], resources: [.copy("Fixtures")]),
    ],
    swiftLanguageModes: [.v5]
)
