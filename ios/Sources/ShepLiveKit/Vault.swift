import Foundation
import Security

public protocol SecretStore {
    func get(_ account: String) -> Data?
    func set(_ data: Data, for account: String) throws
    func remove(_ account: String)
}

public struct VaultError: Error, CustomStringConvertible {
    public let status: OSStatus
    public var description: String { "keychain error \(status)" }
}

/// Keychain items for this app. The sealing key is shared with the Lock Screen extension through a keychain access group,
/// so the extension can open pushed status while the phone is locked (items are readable after the first unlock).
public struct KeychainStore: SecretStore {
    public let service: String
    public let accessGroup: String?
    public init(service: String, accessGroup: String? = nil) { self.service = service; self.accessGroup = accessGroup }

    private func query(_ account: String) -> [String: Any] {
        var q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: account]
        if let g = accessGroup { q[kSecAttrAccessGroup as String] = g }
        return q
    }

    public func get(_ account: String) -> Data? {
        var q = query(account)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        return SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess ? out as? Data : nil
    }

    public func set(_ data: Data, for account: String) throws {
        SecItemDelete(query(account) as CFDictionary)
        var q = query(account)
        q[kSecValueData as String] = data
        q[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let st = SecItemAdd(q as CFDictionary, nil)
        if st != errSecSuccess { throw VaultError(status: st) }
    }

    public func remove(_ account: String) { SecItemDelete(query(account) as CFDictionary) }
}

public final class MemoryStore: SecretStore {
    private var items: [String: Data] = [:]
    private let lock = NSLock()
    public init() {}
    public func get(_ account: String) -> Data? { lock.lock(); defer { lock.unlock() }; return items[account] }
    public func set(_ data: Data, for account: String) throws { lock.lock(); items[account] = data; lock.unlock() }
    public func remove(_ account: String) { lock.lock(); items[account] = nil; lock.unlock() }
}

public enum Vault {
    public static let service = "com.nixfred.shepr.live"

    /// Set in both Info.plists as $(AppIdentifierPrefix)com.nixfred.shepr.live, so it carries the signing team's prefix.
    public static var group: String? { Bundle.main.object(forInfoDictionaryKey: "ShepKeychainGroup") as? String }

    public static func deviceStore() -> KeychainStore { KeychainStore(service: service) }
    public static func sealStore() -> KeychainStore { KeychainStore(service: "shepr.live.seal", accessGroup: group) }

    /// The 32-byte key the host seals pushed status with. Made once, here, and sent to the host over the signed channel.
    public static func sealKey(createIfMissing: Bool = false) -> Data? {
        let store = sealStore()
        if let k = store.get("seal"), k.count == 32 { return k }
        guard createIfMissing else { return nil }
        let k = Data((0..<32).map { _ in UInt8.random(in: 0...255) })
        try? store.set(k, for: "seal")
        return store.get("seal")
    }
}
