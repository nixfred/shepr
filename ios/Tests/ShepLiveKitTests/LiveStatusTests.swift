import XCTest
@testable import ShepLiveKit

final class LiveStatusTests: XCTestCase {
    func testDecodesWhatTheHostSends() throws {
        let json = #"{"v":1,"t":1730000000,"b":2,"d":1,"w":3,"i":4,"z":1,"h":[{"n":"gus","o":true},{"n":"vic","o":false}],"r":[{"n":"herder.remote","h":"gus","s":"b","a":1729999000}],"m":[{"n":"Claude","l":"5h","p":63,"e":1730003600}]}"#
        let s = try JSONDecoder().decode(LiveStatus.self, from: Data(json.utf8))
        XCTAssertEqual(s.b, 2); XCTAssertEqual(s.d, 1); XCTAssertEqual(s.w, 3); XCTAssertEqual(s.i, 4); XCTAssertEqual(s.z, 1)
        XCTAssertEqual(s.h.map(\.n), ["gus", "vic"]); XCTAssertEqual(s.h.map(\.o), [true, false])
        XCTAssertEqual(s.r.first, LiveStatus.Row(n: "herder.remote", h: "gus", s: "b", a: 1729999000))
        XCTAssertEqual(s.m.first, LiveStatus.Meter(n: "Claude", l: "5h", p: 63, e: 1730003600))
    }

    func testToleratesMissingAndUnknownKeys() throws {
        let s = try JSONDecoder().decode(LiveStatus.self, from: Data(#"{"b":1,"future":{"x":1},"r":[{"n":"a","extra":true}]}"#.utf8))
        XCTAssertEqual(s.b, 1); XCTAssertEqual(s.w, 0); XCTAssertEqual(s.r.first?.h, ""); XCTAssertEqual(s.r.first?.s, "w")
    }

    func testEverySampleStaysSmall() throws {
        for (name, s) in LiveStatus.samples {
            let n = try JSONEncoder().encode(s).count
            XCTAssertLessThan(n, 1500, "\(name) is \(n) bytes")
        }
    }

    func testRoundTrip() throws {
        for (_, s) in LiveStatus.samples { XCTAssertEqual(try JSONDecoder().decode(LiveStatus.self, from: try JSONEncoder().encode(s)), s) }
    }
}
