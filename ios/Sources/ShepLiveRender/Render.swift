import Foundation
import SwiftUI
import ShepLiveKit

/// Usage: shepr-live-render [outdir]. Writes a PNG per sample and width, prints the measured size, and exits 1 if any
/// Lock Screen presentation is taller than the 160 points Apple allows.
@main
enum Render {
    @MainActor static func main() {
        let out = CommandLine.arguments.dropFirst().first ?? "render-out"
        try? FileManager.default.createDirectory(atPath: out, withIntermediateDirectories: true)
        var failed = false
        for (name, status) in LiveStatus.samples {
            for width in [371.0, 408.0] {     // iPhone 16 Pro and 16 Pro Max Lock Screen widths, in points
                guard let shot = Snapshot.png(LockScreenView(status: status), width: CGFloat(width)) else {
                    print("FAIL render \(name) @\(Int(width))"); failed = true; continue
                }
                try? shot.data.write(to: URL(fileURLWithPath: "\(out)/lock-\(name)-\(Int(width)).png"))
                let ok = shot.size.height <= 160.5
                print("\(ok ? "PASS" : "FAIL") lock screen \(name) @\(Int(width)): \(Int(shot.size.width)) x \(String(format: "%.1f", shot.size.height)) pt (limit 160)")
                if !ok { failed = true }
            }
            if let shot = Snapshot.png(IslandExpandedBody(status: status), width: 330) {
                try? shot.data.write(to: URL(fileURLWithPath: "\(out)/island-\(name).png"))
                print("INFO island body \(name): \(Int(shot.size.width)) x \(String(format: "%.1f", shot.size.height)) pt")
            }
        }
        exit(failed ? 1 : 0)
    }
}
