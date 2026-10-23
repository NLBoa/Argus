import Foundation

/// The countdown rule (D071), the only place it lives: work due on today's local date with 12 hours
/// or less left counts down. The window pops at 12 h, 3 h, and 1 h left, once per milestone.
enum Countdown {
    static let window: TimeInterval = 12 * 3600
    static let milestones: [TimeInterval] = [12 * 3600, 3 * 3600, 3600]

    static func counting(_ item: FeedItem, at now: Date, calendar: Calendar = .current) -> Bool {
        let left = item.dueAt.timeIntervalSince(now)
        return left > 0 && left <= window && calendar.isDate(item.dueAt, inSameDayAs: now)
    }

    /// When `item` starts counting: 12 hours before it's due, but not before its day starts.
    static func start(of item: FeedItem, calendar: Calendar = .current) -> Date {
        max(item.dueAt.addingTimeInterval(-window), calendar.startOfDay(for: item.dueAt))
    }

    struct Alert: Equatable, Sendable {
        var item: FeedItem
        var milestone: TimeInterval

        /// `id:due_at:milestone`: a moved due date re-arms it, like Argus's notification keys.
        var key: String {
            "\(item.id):\(Int(item.dueAt.timeIntervalSince1970)):\(Int(milestone / 3600))h"
        }
    }

    /// The window to pop for each counting item: its tightest milestone reached, unless already shown.
    /// Opening the Mac at 2 h left shows one window (the 3 h one), not three.
    static func pendingAlerts(
        _ items: [FeedItem], at now: Date, shown: Set<String>, calendar: Calendar = .current
    ) -> [Alert] {
        items.compactMap { item in
            guard counting(item, at: now, calendar: calendar) else { return nil }
            let left = item.dueAt.timeIntervalSince(now)
            guard let milestone = milestones.filter({ left <= $0 }).min() else { return nil }
            let alert = Alert(item: item, milestone: milestone)
            return shown.contains(alert.key) ? nil : alert
        }
    }

    /// Times after `now` when what a widget shows changes: a row starts counting or falls off.
    static func changes(
        _ items: [FeedItem], after now: Date, calendar: Calendar = .current
    ) -> [Date] {
        let times = items.flatMap { [start(of: $0, calendar: calendar), $0.dueAt] }
        return Set(times.filter { $0 > now }).sorted()
    }
}

/// "Today 11:59 pm", "Tomorrow 9 am", "Fri 11:59 pm", "Oct 30": Argus's lowercase clock style.
enum DueText {
    static func format(_ due: Date, now: Date, calendar: Calendar = .current) -> String {
        let time = clock(due, calendar: calendar)
        if calendar.isDate(due, inSameDayAs: now) { return "Today \(time)" }
        if let tomorrow = calendar.date(byAdding: .day, value: 1, to: now),
            calendar.isDate(due, inSameDayAs: tomorrow)
        {
            return "Tomorrow \(time)"
        }
        let style = Date.FormatStyle(timeZone: calendar.timeZone).locale(Locale(identifier: "en_US"))
        if due.timeIntervalSince(now) < 6 * 86400 {
            return "\(due.formatted(style.weekday(.abbreviated))) \(time)"
        }
        return due.formatted(style.month(.abbreviated).day())
    }

    static func clock(_ date: Date, calendar: Calendar = .current) -> String {
        let parts = calendar.dateComponents([.hour, .minute], from: date)
        let hour = parts.hour ?? 0
        let minute = parts.minute ?? 0
        let h = hour % 12 == 0 ? 12 : hour % 12
        let suffix = hour < 12 ? "am" : "pm"
        return minute == 0 ? "\(h) \(suffix)" : String(format: "%d:%02d %@", h, minute, suffix)
    }
}
