#if os(iOS)
import Foundation
import ActivityKit

/// The Live Activity's data. Static part: which host. Dynamic part: either the status in the clear (an update made by this
/// app, which never leaves the phone) or sealed (an update the host pushed through Apple). Apple counts both parts against 4 KB.
public struct ShepActivityAttributes: ActivityAttributes {
    public struct ContentState: Codable, Hashable {
        public var status: LiveStatus?
        public var sealed: String?
        public init(status: LiveStatus? = nil, sealed: String? = nil) { self.status = status; self.sealed = sealed }

        /// What to draw. Sealed status needs the key from the shared keychain, which exists once the phone has been unlocked.
        public func resolved(sealKey: Data? = Vault.sealKey()) -> LiveStatus {
            if let status { return status }
            if let sealed, let key = sealKey, let s = try? Sealed.open(sealed, key: key) { return s }
            return .note("Open Shepr Live to refresh")
        }
    }

    public var host: String
    public init(host: String) { self.host = host }
}
#endif
