import Foundation

/// Everything the Lock Screen shows, in keys short enough that it fits a 4 KB Live Activity push with room to spare
/// (Apple counts attributes and state together). The host builds it. The phone only draws it.
public struct LiveStatus: Codable, Hashable, Sendable {
    public struct Host: Codable, Hashable, Sendable {
        public var n: String          // name
        public var o: Bool            // answering
        public init(n: String, o: Bool = true) { self.n = n; self.o = o }
        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            n = try c.decodeIfPresent(String.self, forKey: .n) ?? "?"
            o = try c.decodeIfPresent(Bool.self, forKey: .o) ?? true
        }
        enum CodingKeys: String, CodingKey { case n, o }
    }

    public struct Row: Codable, Hashable, Sendable {
        public var n: String          // what the session is called
        public var h: String          // which host it runs on
        public var s: String          // "b" blocked on you, "d" finished, "w" working
        public var a: Int             // when that state began (epoch seconds), 0 when not known
        public init(n: String, h: String, s: String, a: Int = 0) { self.n = n; self.h = h; self.s = s; self.a = a }
        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            n = try c.decodeIfPresent(String.self, forKey: .n) ?? "?"
            h = try c.decodeIfPresent(String.self, forKey: .h) ?? ""
            s = try c.decodeIfPresent(String.self, forKey: .s) ?? "w"
            a = try c.decodeIfPresent(Int.self, forKey: .a) ?? 0
        }
        enum CodingKeys: String, CodingKey { case n, h, s, a }
    }

    public struct Meter: Codable, Hashable, Sendable {
        public var n: String          // plan, like "Claude"
        public var l: String          // window, like "5h" or "week"
        public var p: Int             // percent used, 0 to 100
        public var e: Int             // when the window resets (epoch seconds), 0 when not known
        public init(n: String, l: String, p: Int, e: Int = 0) { self.n = n; self.l = l; self.p = p; self.e = e }
        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            n = try c.decodeIfPresent(String.self, forKey: .n) ?? "?"
            l = try c.decodeIfPresent(String.self, forKey: .l) ?? ""
            p = try c.decodeIfPresent(Int.self, forKey: .p) ?? 0
            e = try c.decodeIfPresent(Int.self, forKey: .e) ?? 0
        }
        enum CodingKeys: String, CodingKey { case n, l, p, e }
    }

    public var v: Int
    public var t: Int                 // built at (epoch seconds)
    public var b: Int                 // sessions blocked on you
    public var d: Int                 // sessions that finished recently
    public var w: Int                 // sessions working
    public var i: Int                 // sessions idle
    public var z: Int                 // herdr workspaces with no agent running
    public var h: [Host]
    public var r: [Row]               // most urgent first
    public var m: [Meter]
    public var x: String?             // a plain sentence shown instead of the rows

    public init(v: Int = 1, t: Int, b: Int = 0, d: Int = 0, w: Int = 0, i: Int = 0, z: Int = 0,
                h: [Host] = [], r: [Row] = [], m: [Meter] = [], x: String? = nil) {
        self.v = v; self.t = t; self.b = b; self.d = d; self.w = w; self.i = i; self.z = z
        self.h = h; self.r = r; self.m = m; self.x = x
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        v = try c.decodeIfPresent(Int.self, forKey: .v) ?? 1
        t = try c.decodeIfPresent(Int.self, forKey: .t) ?? 0
        b = try c.decodeIfPresent(Int.self, forKey: .b) ?? 0
        d = try c.decodeIfPresent(Int.self, forKey: .d) ?? 0
        w = try c.decodeIfPresent(Int.self, forKey: .w) ?? 0
        i = try c.decodeIfPresent(Int.self, forKey: .i) ?? 0
        z = try c.decodeIfPresent(Int.self, forKey: .z) ?? 0
        h = try c.decodeIfPresent([Host].self, forKey: .h) ?? []
        r = try c.decodeIfPresent([Row].self, forKey: .r) ?? []
        m = try c.decodeIfPresent([Meter].self, forKey: .m) ?? []
        x = try c.decodeIfPresent(String.self, forKey: .x)
    }
    enum CodingKeys: String, CodingKey { case v, t, b, d, w, i, z, h, r, m, x }

    public static func note(_ text: String, now: Date = Date()) -> LiveStatus {
        LiveStatus(t: Int(now.timeIntervalSince1970), x: text)
    }
}
