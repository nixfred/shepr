import Foundation
import ShepLiveKit

/// A command-line phone, for testing the real wire format against a real Shepr server.
///   shepr-live-probe --server URL [--key FILE] pair CODE | hello | status | register start|update TOKEN [ID] | unregister [ID] | test
@main
enum Probe {
    static func usage() -> Never {
        FileHandle.standardError.write(Data("usage: shepr-live-probe --server URL [--key FILE] pair CODE | hello | status | register start|update TOKEN [ID] | unregister [ID] | test\n".utf8))
        exit(2)
    }

    static func main() async {
        var args = Array(CommandLine.arguments.dropFirst())
        func opt(_ name: String) -> String? {
            guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
            let v = args[i + 1]
            args.removeSubrange(i...(i + 1))
            return v
        }
        guard let server = opt("--server"), let base = URL(string: server) else { usage() }
        let keyPath = opt("--key") ?? "shepr-live-probe.key"
        let key: DeviceKey
        if let raw = FileManager.default.contents(atPath: keyPath), let k = try? DeviceKey(rawRepresentation: raw) { key = k } else {
            key = DeviceKey()
            FileManager.default.createFile(atPath: keyPath, contents: key.rawRepresentation, attributes: [.posixPermissions: 0o600])
        }
        let client = ShepClient(base: base, key: key)
        guard let cmd = args.first else { usage() }
        func show(_ d: Data) { print(String(decoding: d, as: UTF8.self)) }
        do {
            switch cmd {
            case "hello": show(try await client.hello())
            case "pair":
                guard args.count > 1 else { usage() }
                let r = try await client.pair(code: args[1], name: "Probe")
                print("paired \(r.device) \(r.fp)")
            case "status": show(try await client.get("/api/live/status"))
            case "register":
                guard args.count > 2 else { usage() }
                var body: [String: Any] = ["kind": args[1], "token": args[2], "env": "sandbox", "key": Data(repeating: 7, count: 32).base64EncodedString()]
                if args.count > 3 { body["id"] = args[3] }
                show(try await client.post("/api/live/register", body))
            case "unregister":
                show(try await client.post("/api/live/unregister", args.count > 1 ? ["id": args[1]] : [:]))
            case "test": show(try await client.post("/api/live/test", [:]))
            default: usage()
            }
        } catch {
            FileHandle.standardError.write(Data("probe: \(error)\n".utf8))
            exit(1)
        }
    }
}
