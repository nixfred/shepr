import XCTest
import CryptoKit
@testable import ShepLiveKit

final class SignerTests: XCTestCase {
    func testSignatureVerifiesOverTheCanonicalMessage() throws {
        let key = DeviceKey()
        let body = Data(#"{"a":1}"#.utf8)
        let h = try key.sign(method: "POST", target: "/api/x?y=1", body: body, now: Date(timeIntervalSince1970: 1_730_000_000), nonce: "abcdefghijkl")
        XCTAssertEqual(h["X-Shepr-Ts"], "1730000000000")
        XCTAssertEqual(h["X-Shepr-Nonce"], "abcdefghijkl")
        let msg = Data(("1730000000000\nabcdefghijkl\nPOST\n/api/x?y=1\n" + Data(SHA256.hash(data: body)).hex).utf8)
        XCTAssertEqual(msg, DeviceKey.canonicalMessage(ts: "1730000000000", nonce: "abcdefghijkl", method: "POST", target: "/api/x?y=1", body: body))
        let sigRaw = try XCTUnwrap(Data(base64url: try XCTUnwrap(h["X-Shepr-Sig"])))
        XCTAssertEqual(sigRaw.count, 64)
        XCTAssertTrue(key.p256.publicKey.isValidSignature(try P256.Signing.ECDSASignature(rawRepresentation: sigRaw), for: msg))
    }

    func testDeviceIdIsTheHostsName() {
        let key = DeviceKey()
        let x963 = key.p256.publicKey.x963Representation
        XCTAssertEqual(x963.count, 65); XCTAssertEqual(x963.first, 4)
        XCTAssertEqual(key.id, String(Data(SHA256.hash(data: x963)).hex.prefix(16)))
        XCTAssertEqual(key.id.count, 16)
    }

    func testJwkIsAnUncompressedP256Point() throws {
        let j = DeviceKey().jwk
        XCTAssertEqual(j["kty"], "EC"); XCTAssertEqual(j["crv"], "P-256")
        XCTAssertEqual(try XCTUnwrap(Data(base64url: try XCTUnwrap(j["x"]))).count, 32)
        XCTAssertEqual(try XCTUnwrap(Data(base64url: try XCTUnwrap(j["y"]))).count, 32)
    }

    func testKeySurvivesItsRawForm() throws {
        let a = DeviceKey()
        XCTAssertEqual(try DeviceKey(rawRepresentation: a.rawRepresentation).id, a.id)
    }

    func testPairLink() {
        let l = PairLink.parse("https://gus.tail0940f.ts.net:8742/#shepr-pair=ABCD-EFGH-JKMN")
        XCTAssertEqual(l?.base.absoluteString, "https://gus.tail0940f.ts.net:8742")
        XCTAssertEqual(l?.code, "ABCD-EFGH-JKMN")
        XCTAssertNil(PairLink.parse("https://example.com/"))
        XCTAssertNil(PairLink.parse("ftp://x/#shepr-pair=ABCD-EFGH-JKMN"))
        XCTAssertNil(PairLink.parse("https://x/#shepr-pair=SHORT"))
    }

    func testMemoryStore() throws {
        let s = MemoryStore()
        XCTAssertNil(s.get("a")); try s.set(Data([1, 2]), for: "a"); XCTAssertEqual(s.get("a"), Data([1, 2])); s.remove("a"); XCTAssertNil(s.get("a"))
    }
}
