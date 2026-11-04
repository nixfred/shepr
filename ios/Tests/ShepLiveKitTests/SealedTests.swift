import XCTest
import CryptoKit
@testable import ShepLiveKit

final class SealedTests: XCTestCase {
    let key = Data((0..<32).map { UInt8($0) })

    func testRoundTrip() throws {
        let s = LiveStatus.busy
        XCTAssertEqual(try Sealed.open(try Sealed.seal(s, key: key), key: key), s)
    }

    func testWrongKeyFails() throws {
        let blob = try Sealed.seal(.busy, key: key)
        XCTAssertThrowsError(try Sealed.open(blob, key: Data(repeating: 9, count: 32)))
    }

    func testTamperedCiphertextFails() throws {
        var raw = try XCTUnwrap(Data(base64Encoded: try Sealed.seal(.busy, key: key)))
        raw[20] ^= 0x01
        XCTAssertThrowsError(try Sealed.open(raw.base64EncodedString(), key: key))
    }

    func testOtherContextFails() throws {
        let plain = try JSONEncoder().encode(LiveStatus.busy)
        let box = try ChaChaPoly.seal(plain, using: SymmetricKey(data: key), authenticating: Data("something-else".utf8))
        XCTAssertThrowsError(try Sealed.open(box.combined.base64EncodedString(), key: key))
    }

    func testGarbageFails() {
        XCTAssertThrowsError(try Sealed.open("not base64 !!", key: key))
        XCTAssertThrowsError(try Sealed.open("AAAA", key: key))
    }

    func testWorstCaseFitsAppleFourKilobytes() throws {
        let blob = try Sealed.seal(.worst, key: key)
        // the push is {"aps":{... "content-state":{"sealed":"<blob>"}}}; Apple's limit is 4096 bytes for the whole thing
        XCTAssertLessThan(blob.utf8.count + 400, 4096, "sealed worst case is \(blob.utf8.count) bytes")
    }
}
