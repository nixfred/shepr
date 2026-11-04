import WidgetKit
import SwiftUI
import ActivityKit
import ShepLiveKit

@main
struct ShepLiveBundle: WidgetBundle {
    var body: some Widget { ShepLiveActivityWidget() }
}

struct ShepLiveActivityWidget: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: ShepActivityAttributes.self) { context in
            LockScreenView(status: context.state.resolved(), isStale: context.isStale)
                .activityBackgroundTint(Theme.bg.opacity(0.94))
                .activitySystemActionForegroundColor(Theme.accent)
                .widgetURL(URL(string: "shepr-live://open"))
        } dynamicIsland: { context in
            let s = context.state.resolved()
            return DynamicIsland {
                DynamicIslandExpandedRegion(.leading) { IslandCounts(status: s) }
                DynamicIslandExpandedRegion(.trailing) {
                    Text(Date(timeIntervalSince1970: TimeInterval(s.t)), style: .time)
                        .font(.system(size: 11, weight: .medium)).monospacedDigit().foregroundStyle(Theme.dim)
                }
                DynamicIslandExpandedRegion(.bottom) { IslandExpandedBody(status: s) }
            } compactLeading: {
                IslandCompactLeading(status: s)
            } compactTrailing: {
                IslandCompactTrailing(status: s)
            } minimal: {
                IslandMinimal(status: s)
            }
            .keylineTint(Theme.accent)
        }
    }
}
