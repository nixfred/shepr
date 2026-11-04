import XCTest
import SwiftUI
@testable import ShepLiveKit

@MainActor
final class LayoutTests: XCTestCase {
    /// Apple: the Lock Screen presentation is 84 to 160 points tall and the system may truncate anything taller.
    func testLockScreenNeverExceedsAppleHeightLimit() throws {
        for (name, s) in LiveStatus.samples {
            for width in [320.0, 371.0, 408.0] {
                let shot = try XCTUnwrap(Snapshot.png(LockScreenView(status: s), width: CGFloat(width)), "\(name) did not render")
                XCTAssertLessThanOrEqual(shot.size.height, 160.5, "\(name) @\(width) is \(shot.size.height) pt tall")
                XCTAssertGreaterThan(shot.size.height, 40, "\(name) @\(width) rendered empty")
            }
        }
    }

    func testStaleMarkIsShown() throws {
        let a = try XCTUnwrap(Snapshot.png(LockScreenView(status: .busy, isStale: false), width: 371))
        let b = try XCTUnwrap(Snapshot.png(LockScreenView(status: .busy, isStale: true), width: 371))
        XCTAssertNotEqual(a.data, b.data)
    }
}
