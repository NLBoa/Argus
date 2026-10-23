import AppKit
import Observation
import WidgetKit

/// Polls the feed, reloads the widget when it changes, and pops a countdown at each milestone.
@MainActor
@Observable
final class BarModel {
    static let poll: Duration = .seconds(5 * 60)
    static let tick: Duration = .seconds(30)
    private static let shownKey = "shownCountdowns"

    private(set) var load: FeedLoad = .unavailable
    private(set) var counting: [FeedItem] = []
    private let client = FeedClient()
    private let panels = CountdownPanels()
    private var shown: Set<String>

    var summary: String {
        load.feed?.summary ?? "Can't reach Argus"
    }

    var asOf: Date? {
        if case .cached(_, let asOf) = load { asOf } else { nil }
    }

    init() {
        shown = Set(UserDefaults.standard.stringArray(forKey: Self.shownKey) ?? [])
        load = client.cached()
        Task { await run() }
        #if DEBUG
            // `open Argus.app --args -ArgusTestCountdown YES` shows one at launch, to check it by eye.
            if UserDefaults.standard.bool(forKey: "ArgusTestCountdown") { Task { showTestCountdown() } }
        #endif
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in await self?.refresh() }
        }
    }

    private func run() async {
        var sincePoll = Self.poll
        while !Task.isCancelled {
            if sincePoll >= Self.poll {
                await refresh()
                sincePoll = .zero
            } else {
                check()
            }
            try? await Task.sleep(for: Self.tick)
            sincePoll += Self.tick
        }
    }

    func refresh() async {
        let next = await client.load()
        if next.feed != load.feed {
            WidgetCenter.shared.reloadAllTimelines()
        }
        load = next
        check()
    }

    /// Runs on every tick so a milestone pops on time even between fetches.
    private func check() {
        let now = Date.now
        let items = load.feed?.upcoming(at: now) ?? []
        counting = items.filter { Countdown.counting($0, at: now) }
        panels.closeAll(except: Set(counting.map(\.id)))
        let alerts = Countdown.pendingAlerts(items, at: now, shown: shown)
        for alert in alerts {
            panels.show(alert.item)
            shown.insert(alert.key)
        }
        if !alerts.isEmpty {
            // Keep keys for items still in the feed, so the list doesn't grow forever.
            let live = Set(items.map { "\($0.id):" })
            shown = shown.filter { key in live.contains { key.hasPrefix($0) } }
            UserDefaults.standard.set(Array(shown), forKey: Self.shownKey)
        }
    }

    #if DEBUG
        func showTestCountdown() {
            panels.show(
                FeedItem(
                    id: -1, title: "Test countdown", course: "CMSC351", accent: "indigo",
                    kind: "Assignment", dueAt: .now.addingTimeInterval(2 * 3600 + 45 * 60), url: "/today"))
        }
    #endif
}
