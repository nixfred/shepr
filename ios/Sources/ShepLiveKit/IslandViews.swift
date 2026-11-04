import SwiftUI

/// Dynamic Island pieces (iPhone 14 Pro and later). Compact and minimal show the one number that matters.
public struct IslandCompactLeading: View {
    let s: LiveStatus
    public init(status: LiveStatus) { s = status }
    public var body: some View {
        if s.b > 0 { chip("exclamationmark.octagon.fill", s.b, Theme.warn) }
        else if s.d > 0 { chip("checkmark.circle.fill", s.d, Theme.ok) }
        else { Image(systemName: "checkmark.seal.fill").font(.system(size: 13, weight: .bold)).foregroundStyle(Theme.accent) }
    }
    private func chip(_ symbol: String, _ n: Int, _ color: Color) -> some View {
        HStack(spacing: 2) {
            Image(systemName: symbol)
            Text("\(n)").monospacedDigit()
        }
        .font(.system(size: 13, weight: .bold, design: .rounded)).foregroundStyle(color)
    }
}

public struct IslandCompactTrailing: View {
    let s: LiveStatus
    public init(status: LiveStatus) { s = status }
    public var body: some View {
        if s.w > 0 {
            HStack(spacing: 2) {
                Image(systemName: "gearshape.fill")
                Text("\(s.w)").monospacedDigit()
            }
            .font(.system(size: 13, weight: .bold, design: .rounded)).foregroundStyle(Theme.accent)
        } else {
            Text("idle").font(.system(size: 11, weight: .semibold)).foregroundStyle(Theme.dim)
        }
    }
}

public struct IslandMinimal: View {
    let s: LiveStatus
    public init(status: LiveStatus) { s = status }
    public var body: some View {
        if s.b > 0 {
            Text("\(s.b)").font(.system(size: 13, weight: .heavy, design: .rounded)).foregroundStyle(Theme.warn)
        } else {
            Image(systemName: "gearshape.fill").font(.system(size: 12, weight: .bold)).foregroundStyle(Theme.accent)
        }
    }
}

/// The expanded Island: the same rows as the Lock Screen, three of them.
public struct IslandExpandedBody: View {
    let s: LiveStatus
    public init(status: LiveStatus) { s = status }
    public var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            if s.r.isEmpty {
                Text(s.x ?? "Nothing is waiting on you.").font(.system(size: 13.5, weight: .semibold)).foregroundStyle(Theme.dim)
            } else {
                ForEach(Array(s.r.prefix(3).enumerated()), id: \.offset) { _, row in RowLine(row: row) }
            }
        }
    }
}

public struct IslandCounts: View {
    let s: LiveStatus
    public init(status: LiveStatus) { s = status }
    public var body: some View {
        HStack(spacing: 8) {
            CountChip(symbol: "exclamationmark.octagon.fill", n: s.b, color: Theme.warn)
            CountChip(symbol: "checkmark.circle.fill", n: s.d, color: Theme.ok)
            CountChip(symbol: "gearshape.fill", n: s.w, color: Theme.accent)
        }
    }
}
