import XCTest
import CryptoKit
@testable import ShepLiveKit

/// The host (Python) and the phone (Swift) are two implementations of one cipher and one JSON shape. This file is made by
/// `python3 tests/live_test.py --fixture` and read here, so a change to either side that breaks the other fails a test.
final class FixtureTests: XCTestCase {
    struct Fixture: Decodable { let key: String; let nonce: String; let plain: String; let status: LiveStatus; let sealed: String }

    private func load() throws -> Fixture {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "sealed", withExtension: "json", subdirectory: "Fixtures"))
        return try JSONDecoder().decode(Fixture.self, from: try Data(contentsOf: url))
    }

    private func bytes(_ hex: String) -> Data {
        Data(stride(from: 0, to: hex.count, by: 2).map { i in
            UInt8(hex[hex.index(hex.startIndex, offsetBy: i)..<hex.index(hex.startIndex, offsetBy: i + 2)], radix: 16)!
        })
    }

    func testOpensWhatThePythonHostSealed() throws {
        let f = try load()
        XCTAssertEqual(try Sealed.open(f.sealed, key: bytes(f.key)), f.status)
        XCTAssertEqual(f.status.b, 2); XCTAssertEqual(f.status.r.first?.n, "pipeline")
    }

    func testSealingTheSameBytesGivesTheSameText() throws {
        let f = try load()
        let box = try ChaChaPoly.seal(Data(f.plain.utf8), using: SymmetricKey(data: bytes(f.key)),
                                      nonce: try ChaChaPoly.Nonce(data: bytes(f.nonce)), authenticating: Sealed.aad)
        XCTAssertEqual(box.combined.base64EncodedString(), f.sealed)
    }
}
