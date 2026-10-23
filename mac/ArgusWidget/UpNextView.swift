import SwiftUI
import WidgetKit

struct UpNextView: View {
    var entry: Entry

    var body: some View {
        switch entry.load {
        case .live(let feed): content(feed, asOf: nil)
        case .cached(let feed, let asOf): content(feed, asOf: asOf)
        case .unavailable:
            message("Can't reach Argus", detail: "Check that Tailscale is connected.", icon: "wifi.slash")
        }
    }

    @ViewBuilder
    private func content(_ feed: Feed, asOf: Date?) -> some View {
        let items = feed.upcoming(at: entry.date)
        let note = footer(feed, asOf: asOf)
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text("Up next").font(.headline)
                Spacer(minLength: 8)
                Text(feed.summary)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            if items.isEmpty {
                Spacer(minLength: 0)
                Label("Nothing due this week", systemImage: "checkmark.circle")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity)
                Spacer(minLength: 0)
            } else {
                VStack(spacing: 5) {
                    ForEach(items.prefix(note == nil ? 4 : 3)) { item in
                        Link(destination: Argus.link(item.url)) {
                            Row(item: item, now: entry.date)
                        }
                    }
                }
                Spacer(minLength: 0)
            }
            if let note {
                Text(note)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
    }

    private func footer(_ feed: Feed, asOf: Date?) -> String? {
        if let asOf { return "As of \(DueText.clock(asOf))" }
        return feed.notes.first
    }

    private func message(_ title: String, detail: String, icon: String) -> some View {
        VStack(spacing: 6) {
            Image(systemName: icon).font(.title2).foregroundStyle(.secondary)
            Text(title).font(.headline)
            Text(detail).font(.caption).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// One line, Reminders-style: stripe, title, course, then when it's due or the live countdown.
private struct Row: View {
    var item: FeedItem
    var now: Date

    var body: some View {
        HStack(spacing: 8) {
            RoundedRectangle(cornerRadius: 1.5)
                .fill(Palette.accent(item.accent))
                .frame(width: 3, height: 16)
                .widgetAccentable()
            Text(item.title)
                .font(.system(size: 13, weight: .medium))
                .lineLimit(1)
                .layoutPriority(1)
            Text(item.course)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
            Spacer(minLength: 6)
            if Countdown.counting(item, at: now) {
                HStack(spacing: 3) {
                    Image(systemName: "timer").imageScale(.small)
                    Text(timerInterval: now...item.dueAt, countsDown: true)
                        .monospacedDigit()
                        .multilineTextAlignment(.trailing)
                        .frame(width: 58, alignment: .trailing)
                }
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(Palette.brand)
                .widgetAccentable()
            } else {
                Text(DueText.format(item.dueAt, now: now))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
        .frame(height: 20)
        .contentShape(Rectangle())
    }
}
