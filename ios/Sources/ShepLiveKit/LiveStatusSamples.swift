import Foundation

extension LiveStatus {
    private static var now: Int { Int(Date().timeIntervalSince1970) }

    public static var busy: LiveStatus {
        let n = now
        return LiveStatus(t: n, b: 2, d: 1, w: 3, i: 4, z: 2,
            h: [.init(n: "gus"), .init(n: "vic")],
            r: [.init(n: "herder.remote", h: "gus", s: "b", a: n - 742), .init(n: "mastra-demo", h: "gus", s: "b", a: n - 95),
                .init(n: "mkimage", h: "vic", s: "d", a: n - 300), .init(n: "sigkill", h: "gus", s: "w"), .init(n: "burnbar", h: "vic", s: "w"),
                .init(n: "bot-office", h: "gus", s: "w")],
            m: [.init(n: "Claude", l: "5h", p: 63, e: n + 5400), .init(n: "Codex", l: "5h", p: 18, e: n + 9000), .init(n: "Grok", l: "week", p: 4)])
    }

    public static var calm: LiveStatus {
        LiveStatus(t: now, i: 5, z: 3, h: [.init(n: "gus"), .init(n: "vic")], m: [.init(n: "Claude", l: "5h", p: 12)])
    }

    public static var down: LiveStatus {
        let n = now
        return LiveStatus(t: n, b: 1, w: 1, h: [.init(n: "gus"), .init(n: "vic", o: false)],
            r: [.init(n: "herder.remote", h: "gus", s: "b", a: n - 3700), .init(n: "sigkill", h: "gus", s: "w")],
            m: [.init(n: "Claude", l: "5h", p: 93, e: n + 600)])
    }

    /// The most the Lock Screen has to hold: eight sessions with long names, three hosts, three meters.
    public static var worst: LiveStatus {
        let n = now
        return LiveStatus(t: n, b: 3, d: 2, w: 3, i: 12, z: 9,
            h: [.init(n: "gus"), .init(n: "vic"), .init(n: "fnix"), .init(n: "hex", o: false)],
            r: [.init(n: "a-very-long-workspace-name-for-one-session", h: "gus", s: "b", a: n - 40000),
                .init(n: "another-quite-long-project-name", h: "vic", s: "b", a: n - 5000),
                .init(n: "third-blocked-session", h: "fnix", s: "b", a: n - 90),
                .init(n: "finished-session-with-a-long-name", h: "gus", s: "d", a: n - 1200),
                .init(n: "fifth", h: "vic", s: "d", a: n - 60),
                .init(n: "sixth", h: "gus", s: "w"), .init(n: "seventh", h: "gus", s: "w"), .init(n: "eighth", h: "vic", s: "w")],
            m: [.init(n: "Claude", l: "5h", p: 97, e: n + 900), .init(n: "Codex", l: "week", p: 71), .init(n: "Kimi", l: "month", p: 33)])
    }

    public static var samples: [(name: String, status: LiveStatus)] {
        [("busy", busy), ("calm", calm), ("down", down), ("worst", worst)]
    }
}
