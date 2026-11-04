import Foundation
import CryptoKit

/// The phone's identity to a Shepr host: a P-256 key. Every request after pairing carries an ECDSA signature over
/// "timestamp, nonce, method, target, sha256(body)", exactly what the web app's WebCrypto key signs.
public struct DeviceKey {
    public let p256: P256.Signing.PrivateKey

    public init(p256: P256.Signing.PrivateKey = P256.Signing.PrivateKey()) { self.p256 = p256 }
    public init(rawRepresentation: Data) throws { p256 = try P256.Signing.PrivateKey(rawRepresentation: rawRepresentation) }
    public var rawRepresentation: Data { p256.rawRepresentation }

    var x963: Data { p256.publicKey.x963Representation }          // 0x04 || x || y

    /// The host's name for this phone: the first 16 hex characters of sha256(0x04 || x || y).
    public var id: String { String(Data(SHA256.hash(data: x963)).hex.prefix(16)) }

    public var jwk: [String: String] {
        ["kty": "EC", "crv": "P-256", "x": x963.subdata(in: 1..<33).base64url, "y": x963.subdata(in: 33..<65).base64url]
    }

    public static func canonicalMessage(ts: String, nonce: String, method: String, target: String, body: Data) -> Data {
        let bodyHash = Data(SHA256.hash(data: body)).hex
        return Data([ts, nonce, method, target, bodyHash].joined(separator: "\n").utf8)
    }

    public func sign(method: String, target: String, body: Data, now: Date = Date(), nonce: String? = nil) throws -> [String: String] {
        let ts = String(Int((now.timeIntervalSince1970 * 1000).rounded()))
        let n = nonce ?? Data((0..<12).map { _ in UInt8.random(in: 0...255) }).base64url
        let msg = DeviceKey.canonicalMessage(ts: ts, nonce: n, method: method, target: target, body: body)
        let sig = try p256.signature(for: msg).rawRepresentation     // r || s, 64 bytes
        return ["X-Shepr-Device": id, "X-Shepr-Ts": ts, "X-Shepr-Nonce": n, "X-Shepr-Sig": sig.base64url]
    }
}
