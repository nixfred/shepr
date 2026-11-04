import SwiftUI

struct CountChip: View {
    let symbol: String
    let n: Int
    let color: Color
    var body: some View {
        HStack(spacing: 2) {
            Image(systemName: symbol).font(.system(size: 10.5, weight: .bold))
            Text("\(n)").font(.system(size: 13, weight: .bold, design: .rounded)).monospacedDigit()
        }
        .foregroundStyle(n > 0 ? color : Theme.dim.opacity(0.55))
    }
}

struct HostDots: View {
    let hosts: [LiveStatus.Host]
    var body: some View {
        HStack(spacing: 6) {
            ForEach(Array(hosts.prefix(4).enumerated()), id: \.offset) { _, h in
                HStack(spacing: 3) {
                    Circle().fill(h.o ? Theme.ok : Theme.hot).frame(width: 5, height: 5)
                    Text(h.n).font(.system(size: 9, weight: .semibold, design: .monospaced)).foregroundStyle(h.o ? Theme.dim : Theme.hot).lineLimit(1)
                }
            }
        }
    }
}

struct HeaderLine: View {
    let s: LiveStatus
    var body: some View {
        HStack(spacing: 8) {
            Text("SHEPR").font(.system(size: 10.5, weight: .heavy)).tracking(1.6).foregroundStyle(Theme.accent)
            HostDots(hosts: s.h)
            Spacer(minLength: 4)
            CountChip(symbol: "exclamationmark.octagon.fill", n: s.b, color: Theme.warn)
            CountChip(symbol: "checkmark.circle.fill", n: s.d, color: Theme.ok)
            CountChip(symbol: "gearshape.fill", n: s.w, color: Theme.accent)
            CountChip(symbol: "moon.zzz.fill", n: s.i, color: Theme.dim)
        }
        .frame(height: 18)
    }
}

struct RowLine: View {
    let row: LiveStatus.Row
    var body: some View {
        HStack(spacing: 7) {
            Circle().fill(Theme.tint(row.s)).frame(width: 7, height: 7)
            Text(row.n).font(.system(size: 13.5, weight: .semibold)).foregroundStyle(Theme.text).lineLimit(1).privacySensitive()
            Text(row.h.uppercased()).font(.system(size: 9, weight: .semibold, design: .monospaced)).foregroundStyle(Theme.dim).lineLimit(1)
            Spacer(minLength: 4)
            trailing
        }
        .frame(height: 17)
    }

    @ViewBuilder private var trailing: some View {
        switch row.s {
        case "b": clock(word: "waiting", color: Theme.warn)
        case "d": clock(word: "done", color: Theme.ok)
        default: Text("working").font(.system(size: 10, weight: .medium)).foregroundStyle(Theme.dim)
        }
    }

    /// The system keeps this ticking by itself, so a long wait counts up on the Lock Screen without any push.
    @ViewBuilder private func clock(word: String, color: Color) -> some View {
        HStack(spacing: 4) {
            Text(word).font(.system(size: 9.5, weight: .medium)).foregroundStyle(Theme.dim).lineLimit(1).fixedSize()
            if row.a > 0 {
                Text(Date(timeIntervalSince1970: TimeInterval(row.a)), style: .timer)
                    .font(.system(size: 12.5, weight: .semibold, design: .rounded)).monospacedDigit()
                    .foregroundStyle(color).lineLimit(1).minimumScaleFactor(0.7).frame(width: 64, alignment: .trailing)
            }
        }
    }
}

struct MeterBit: View {
    let m: LiveStatus.Meter
    private var color: Color { m.p >= 90 ? Theme.hot : (m.p >= 70 ? Theme.warn : Theme.accent) }
    var body: some View {
        HStack(spacing: 3) {
            Text(m.n).font(.system(size: 9.5, weight: .medium)).foregroundStyle(Theme.dim).lineLimit(1)
            ZStack(alignment: .leading) {
                Capsule().fill(Theme.line).frame(width: 26, height: 4)
                Capsule().fill(color).frame(width: 26 * CGFloat(max(0, min(100, m.p))) / 100, height: 4)
            }
            Text("\(m.p)%").font(.system(size: 9.5, weight: .semibold)).monospacedDigit().foregroundStyle(color)
        }
    }
}

struct FooterLine: View {
    let s: LiveStatus
    let stale: Bool
    let hidden: Int
    var body: some View {
        HStack(spacing: 10) {
            ForEach(Array(s.m.prefix(hidden > 0 ? 2 : 3).enumerated()), id: \.offset) { _, m in MeterBit(m: m) }
            if hidden > 0 { Text("+\(hidden) more").font(.system(size: 9.5, weight: .medium)).foregroundStyle(Theme.dim) }
            Spacer(minLength: 4)
            if stale { Text("stale").font(.system(size: 9.5, weight: .bold)).foregroundStyle(Theme.warn) }
            Text(Date(timeIntervalSince1970: TimeInterval(s.t)), style: .time)
                .font(.system(size: 9.5, weight: .medium)).monospacedDigit().foregroundStyle(Theme.dim)
        }
        .frame(height: 13)
    }
}

/// The big one. Apple lets the Lock Screen presentation grow from 84 to 160 points tall (width 371 or 408), so the whole
/// layout is built to stay under 160 with the busiest status it can be handed. LayoutTests checks that for real.
public struct LockScreenView: View {
    let status: LiveStatus
    let isStale: Bool
    static let maxRows = 5

    public init(status: LiveStatus, isStale: Bool = false) {
        self.status = status
        self.isStale = isStale
    }

    private var shown: [LiveStatus.Row] { Array(status.r.prefix(Self.maxRows)) }

    public var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HeaderLine(s: status)
            if shown.isEmpty {
                Text(status.x ?? "Nothing is waiting on you.")
                    .font(.system(size: 13.5, weight: .semibold)).foregroundStyle(Theme.dim).lineLimit(1)
                    .frame(height: 17, alignment: .leading)
            } else {
                ForEach(Array(shown.enumerated()), id: \.offset) { _, row in RowLine(row: row) }
            }
            FooterLine(s: status, stale: isStale, hidden: max(0, status.b + status.d + status.w - shown.count))
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
