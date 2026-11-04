import SwiftUI
#if canImport(AppKit)
import AppKit
#elseif canImport(UIKit)
import UIKit
#endif

/// Draws a view to a PNG at its natural height for a given width, so layout can be measured and looked at without a phone.
public enum Snapshot {
    @MainActor
    public static func png<V: View>(_ view: V, width: CGFloat, dark: Bool = true, scale: CGFloat = 3) -> (data: Data, size: CGSize)? {
        let content = view
            .frame(width: width, alignment: .topLeading)
            .fixedSize(horizontal: false, vertical: true)
            .background(Theme.bg)
            .environment(\.colorScheme, dark ? .dark : .light)
        let r = ImageRenderer(content: content)
        r.scale = scale
        #if canImport(AppKit)
        guard let img = r.nsImage, let tiff = img.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
              let data = rep.representation(using: .png, properties: [:]) else { return nil }
        return (data, img.size)
        #elseif canImport(UIKit)
        guard let img = r.uiImage, let data = img.pngData() else { return nil }
        return (data, img.size)
        #else
        return nil
        #endif
    }
}
