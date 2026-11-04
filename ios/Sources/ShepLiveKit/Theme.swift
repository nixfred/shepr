import SwiftUI

/// Shepr's own palette, the same as app.css, so the Lock Screen looks like the app.
public enum Theme {
    public static let bg = Color(red: 7 / 255, green: 10 / 255, blue: 15 / 255)
    public static let card = Color(red: 14 / 255, green: 22 / 255, blue: 35 / 255)
    public static let line = Color(red: 26 / 255, green: 38 / 255, blue: 53 / 255)
    public static let text = Color(red: 211 / 255, green: 221 / 255, blue: 232 / 255)
    public static let dim = Color(red: 134 / 255, green: 148 / 255, blue: 165 / 255)
    public static let accent = Color(red: 0, green: 240 / 255, blue: 1)
    public static let ok = Color(red: 124 / 255, green: 1, blue: 0)
    public static let warn = Color(red: 1, green: 176 / 255, blue: 0)
    public static let hot = Color(red: 1, green: 77 / 255, blue: 109 / 255)

    public static func tint(_ state: String) -> Color {
        switch state {
        case "b": return warn
        case "d": return ok
        default: return accent
        }
    }
}
