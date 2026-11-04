import Foundation

public struct ShepError: Error, CustomStringConvertible {
    public let code: String
    public let message: String
    public init(code: String, message: String) { self.code = code; self.message = message }
    public var description: String { "\(code): \(message)" }
}

public struct PairResult: Decodable {
    public let device: String
    public let fp: String
    public let host: String
}

/// What `shepr pair` puts in its QR code: https://<host>:<port>/#shepr-pair=XXXX-XXXX-XXXX
public struct PairLink: Equatable {
    public let base: URL
    public let code: String

    public static func parse(_ text: String) -> PairLink? {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let u = URL(string: t), let scheme = u.scheme, scheme == "https" || scheme == "http", let host = u.host,
              let frag = u.fragment, frag.hasPrefix("shepr-pair=") else { return nil }
        let code = String(frag.dropFirst("shepr-pair=".count))
        var c = URLComponents()
        c.scheme = scheme; c.host = host; c.port = u.port
        guard code.count >= 12, let base = c.url else { return nil }
        return PairLink(base: base, code: code)
    }
}

/// Talks to a Shepr host the way the web app does: unsigned for hello and pair, ECDSA-signed for everything else.
public final class ShepClient: @unchecked Sendable {
    public let base: URL
    public let key: DeviceKey?
    private let session: URLSession
    private var skew: TimeInterval = 0
    private let lock = NSLock()

    // NSLock may not be taken inside an async function, so the two touches of `skew` live in plain functions.
    private func currentSkew() -> TimeInterval { lock.lock(); defer { lock.unlock() }; return skew }
    private func setSkew(_ v: TimeInterval) { lock.lock(); skew = v; lock.unlock() }

    public init(base: URL, key: DeviceKey? = nil, session: URLSession? = nil) {
        self.base = base
        self.key = key
        if let session { self.session = session } else {
            let cfg = URLSessionConfiguration.ephemeral
            cfg.timeoutIntervalForRequest = 12
            cfg.waitsForConnectivity = false
            self.session = URLSession(configuration: cfg)
        }
    }

    private func url(_ path: String) -> URL {
        var b = base.absoluteString
        while b.hasSuffix("/") { b.removeLast() }
        return URL(string: b + path)!
    }

    public func hello() async throws -> Data { try await send("GET", "/api/hello", body: nil, signed: false) }

    public func pair(code: String, name: String) async throws -> PairResult {
        guard let key else { throw ShepError(code: "no_key", message: "this client has no device key") }
        let body = try JSONSerialization.data(withJSONObject: ["code": code, "jwk": key.jwk, "name": name])
        return try JSONDecoder().decode(PairResult.self, from: try await send("POST", "/api/pair", body: body, signed: false))
    }

    public func get(_ path: String) async throws -> Data { try await signedSend("GET", path, nil) }

    public func post(_ path: String, _ json: [String: Any]) async throws -> Data {
        try await signedSend("POST", path, try JSONSerialization.data(withJSONObject: json))
    }

    private func signedSend(_ method: String, _ path: String, _ body: Data?) async throws -> Data {
        do { return try await send(method, path, body: body, signed: true) } catch let e as ShepError where e.code == "stale" {
            // The phone's clock is off: learn the host's time once, then retry.
            if let h = try? JSONSerialization.jsonObject(with: try await hello()) as? [String: Any], let t = h["t"] as? Double {
                setSkew(t - Date().timeIntervalSince1970)
            }
            return try await send(method, path, body: body, signed: true)
        }
    }

    private func send(_ method: String, _ path: String, body: Data?, signed: Bool) async throws -> Data {
        var req = URLRequest(url: url(path))
        req.httpMethod = method
        if method == "POST" {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.setValue("1", forHTTPHeaderField: "X-Shepr")
            req.httpBody = body
        }
        if signed {
            guard let key else { throw ShepError(code: "no_key", message: "this client has no device key") }
            let h = try key.sign(method: method, target: path, body: body ?? Data(), now: Date().addingTimeInterval(currentSkew()))
            for (k, v) in h { req.setValue(v, forHTTPHeaderField: k) }
        }
        let (data, resp) = try await session.data(for: req)
        guard let http = resp as? HTTPURLResponse else { throw ShepError(code: "bad_response", message: "not an HTTP answer") }
        if (200..<300).contains(http.statusCode) { return data }
        let j = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        throw ShepError(code: (j?["error"] as? String) ?? "http_\(http.statusCode)", message: (j?["message"] as? String) ?? "HTTP \(http.statusCode)")
    }
}
