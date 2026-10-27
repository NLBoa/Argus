import SwiftUI
import WidgetKit

@main
struct ArgusWidgets: WidgetBundle {
    var body: some Widget {
        UpNextWidget()
    }
}

struct UpNextWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "UpNext", provider: Provider()) { entry in
            UpNextView(entry: entry)
                .containerBackground(.ultraThinMaterial, for: .widget)
        }
        .configurationDisplayName("Up next")
        .description("Your next deadlines, with a countdown on the day they're due.")
        .supportedFamilies([.systemMedium, .systemLarge])
    }
}

struct Entry: TimelineEntry {
    var date: Date
    var load: FeedLoad
}

struct Provider: TimelineProvider {
    /// WidgetKit's refresh budget allows about one reload every 15 minutes (D071).
    static let refresh: TimeInterval = 15 * 60

    func placeholder(in context: Context) -> Entry {
        Entry(date: .now, load: .live(.sample))
    }

    func getSnapshot(in context: Context, completion: @escaping @Sendable (Entry) -> Void) {
        if context.isPreview {
            completion(placeholder(in: context))
            return
        }
        Task { completion(Entry(date: .now, load: await FeedClient().load())) }
    }

    func getTimeline(in context: Context, completion: @escaping @Sendable (Timeline<Entry>) -> Void) {
        Task {
            let now = Date.now
            let load = await FeedClient().load()
            // One entry now, then one at each moment a row starts counting or falls off.
            let later = Countdown.changes(load.feed?.items ?? [], after: now)
                .prefix(while: { $0 < now.addingTimeInterval(24 * 3600) })
            let entries = [Entry(date: now, load: load)] + later.map { Entry(date: $0, load: load) }
            completion(Timeline(entries: entries, policy: .after(now.addingTimeInterval(Self.refresh))))
        }
    }
}

extension Feed {
    static let sample = Feed(
        version: 1, generatedAt: .now, summary: "2 due today, 1 tomorrow", notes: [],
        items: [
            FeedItem(
                id: 1, title: "Homework 3", course: "CMSC351", accent: "indigo", kind: "Assignment",
                dueAt: .now.addingTimeInterval(3 * 3600), url: "/items/1"),
            FeedItem(
                id: 2, title: "Problem Set 6", course: "MATH240", accent: "teal", kind: "Assignment",
                dueAt: .now.addingTimeInterval(6 * 3600), url: "/items/2"),
            FeedItem(
                id: 3, title: "Lab report", course: "ENGL393", accent: "plum", kind: "Assignment",
                dueAt: .now.addingTimeInterval(30 * 3600), url: "/items/3"),
        ])
}
