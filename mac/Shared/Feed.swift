import AppKit
import SwiftUI

/// `GET /widget.json` (D071).
struct Feed: Codable, Equatable, Sendable {
    var version: Int
    var generatedAt: Date
    var summary: String
    var notes: [String]
    var items: [FeedItem]

    static func decode(_ data: Data) throws -> Feed {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        decoder.dateDecodingStrategy = .iso8601
        return try decoder.decode(Feed.self, from: data)
    }

    /// Whether the widget would look the same. `generatedAt` changes on every fetch, so it's ignored:
    /// reloading on it would spend WidgetKit's daily reload budget on nothing.
    func shows(sameAs other: Feed?) -> Bool {
        guard let other else { return false }
        return summary == other.summary && notes == other.notes && items == other.items
    }

    /// Items still ahead of `now`: the feed can be up to 15 minutes old.
    func upcoming(at now: Date) -> [FeedItem] {
        items.filter { $0.dueAt > now }
    }
}

struct FeedItem: Codable, Equatable, Hashable, Identifiable, Sendable {
    var id: Int
    var title: String
    var course: String
    var accent: String
    var kind: String
    var dueAt: Date
    var url: String
}

enum Argus {
    /// `ArgusBaseURL` in Info.plist, set from `ARGUS_BASE_URL` in the xcconfig.
    static var baseURL: URL {
        let raw = Bundle.main.object(forInfoDictionaryKey: "ArgusBaseURL") as? String
        return raw.flatMap(URL.init(string:)) ?? URL(string: "http://127.0.0.1:8000")!
    }

    static func link(_ path: String) -> URL {
        URL(string: path, relativeTo: baseURL)?.absoluteURL ?? baseURL
    }
}

/// Course accents and the brand teal, from argus/web/static/src/tokens.css (light, dark).
enum Palette {
    private static let accents: [String: (UInt32, UInt32)] = [
        "indigo": (0x4A50C8, 0x8F95F2),
        "teal": (0x0F7B7B, 0x4CC3C0),
        "plum": (0x8A3D8F, 0xD38AD8),
        "ochre": (0x996100, 0xE0AD4A),
        "rose": (0xB8325B, 0xF28AA8),
        "sky": (0x1F6FB2, 0x72B3EC),
        "olive": (0x5E7316, 0xB3CC5C),
        "slate": (0x5C6B80, 0x9AA8BD),
    ]

    static func accent(_ name: String) -> Color {
        let (light, dark) = accents[name] ?? accents["slate"]!
        return dynamic(light, dark)
    }

    static let brand = dynamic(0x0B636B, 0x56C6CC)

    private static func dynamic(_ light: UInt32, _ dark: UInt32) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            let isDark = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
            return rgb(isDark ? dark : light)
        })
    }

    private static func rgb(_ hex: UInt32) -> NSColor {
        NSColor(
            srgbRed: CGFloat((hex >> 16) & 0xFF) / 255,
            green: CGFloat((hex >> 8) & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255,
            alpha: 1
        )
    }
}
