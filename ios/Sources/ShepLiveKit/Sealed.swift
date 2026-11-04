import Foundation
import CryptoKit

/// A Live Activity push travels through Apple, and Shepr's rule is that no session name does. So the host seals the status with
/// ChaCha20-Poly1305 under a key only this phone and the host know, and the Lock Screen extension opens it. Apple sees ciphertext.
/// Layout of the base64 text: 12-byte nonce, ciphertext, 16-byte tag (CryptoKit's "combined" form).
public enum Sealed {
    public static let aad = Data("shepr-live-v1".utf8)

    public enum Failure: Error { case notBase64, tooShort, wrongKey }

    public static func open(_ b64: String, key: Data) throws -> LiveStatus {
        guard let raw = Data(base64Encoded: b64) else { throw Failure.notBase64 }
        guard raw.count >= 12 + 16 else { throw Failure.tooShort }
        let box = try ChaChaPoly.SealedBox(combined: raw)
        let plain: Data
        do { plain = try ChaChaPoly.open(box, using: SymmetricKey(data: key), authenticating: aad) } catch { throw Failure.wrongKey }
        return try JSONDecoder().decode(LiveStatus.self, from: plain)
    }

    public static func seal(_ s: LiveStatus, key: Data, nonce: ChaChaPoly.Nonce? = nil) throws -> String {
        let plain = try JSONEncoder().encode(s)
        let box = try ChaChaPoly.seal(plain, using: SymmetricKey(data: key), nonce: nonce, authenticating: aad)
        return box.combined.base64EncodedString()
    }
}
