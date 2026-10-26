import Foundation
import Testing

private let eastern: Calendar = {
    var c = Calendar(identifier: .gregorian)
    c.timeZone = TimeZone(identifier: "America/New_York")!
    return c
}()

/// 2026-10-22 at `hour`:`minute` Eastern (EDT, UTC-4).
private func at(_ hour: Int, _ minute: Int = 0, day: Int = 22) -> Date {
    eastern.date(from: DateComponents(year: 2026, month: 10, day: day, hour: hour, minute: minute))!
}

private func item(_ id: Int = 1, due: Date) -> FeedItem {
    FeedItem(
        id: id, title: "Problem Set 7", course: "MATH240", accent: "teal", kind: "Assignment",
        dueAt: due, url: "/items/\(id)")
}

@Suite struct CountdownRule {
    @Test func countsWithinTwelveHoursOnTheSameDay() {
        let due = item(due: at(23, 59))
        #expect(Countdown.counting(due, at: at(12), calendar: eastern))
        #expect(!Countdown.counting(due, at: at(11, 58), calendar: eastern))  // 12 h 1 min left
        #expect(!Countdown.counting(due, at: at(23, 59), calendar: eastern))  // due now
    }

    @Test func tomorrowMorningWaitsForMidnight() {
        let due = item(due: at(8, day: 23))
        #expect(!Countdown.counting(due, at: at(21), calendar: eastern))  // 11 h left, but tomorrow
        #expect(Countdown.counting(due, at: at(0, 1, day: 23), calendar: eastern))
        #expect(Countdown.start(of: due, calendar: eastern) == at(0, day: 23))
    }

    @Test func popsTheTightestMilestoneOnce() {
        let due = item(due: at(23, 59))
        let first = Countdown.pendingAlerts([due], at: at(21), shown: [], calendar: eastern)
        #expect(first.map(\.milestone) == [3 * 3600])  // 2 h 59 min left: the 3 h window only
        let shown = Set(first.map(\.key))
        #expect(Countdown.pendingAlerts([due], at: at(21, 30), shown: shown, calendar: eastern).isEmpty)
        let last = Countdown.pendingAlerts([due], at: at(23), shown: shown, calendar: eastern)
        #expect(last.map(\.milestone) == [3600])
    }

    @Test func aMovedDueDateRearms() {
        let shown = Set(
            Countdown.pendingAlerts([item(due: at(23, 59))], at: at(12), shown: [], calendar: eastern)
                .map(\.key))
        let moved = Countdown.pendingAlerts(
            [item(due: at(23, 30))], at: at(12), shown: shown, calendar: eastern)
        #expect(moved.count == 1)
    }

    @Test func widgetChangesAtCountdownStartAndDue() {
        let items = [item(1, due: at(23, 59)), item(2, due: at(8, day: 23))]
        #expect(
            Countdown.changes(items, after: at(10), calendar: eastern)
                == [at(11, 59), at(23, 59), at(0, day: 23), at(8, day: 23)])
    }
}

@Suite struct DueTexts {
    @Test func relativeDays() {
        #expect(DueText.format(at(23, 59), now: at(9), calendar: eastern) == "Today 11:59 pm")
        #expect(DueText.format(at(9, day: 23), now: at(9), calendar: eastern) == "Tomorrow 9 am")
        #expect(DueText.format(at(12, 30, day: 24), now: at(9), calendar: eastern) == "Sat 12:30 pm")
        #expect(DueText.format(at(9, day: 30), now: at(9), calendar: eastern) == "Oct 30")
    }
}

@Suite struct FeedDecoding {
    @Test func decodesTheWidgetFeed() throws {
        let json = """
            {"version": 1, "generated_at": "2026-10-22T18:00:00Z", "summary": "2 due today",
             "notes": ["Gradescope last synced 3h ago"],
             "items": [{"id": 2, "title": "Homework 3", "course": "CMSC351", "accent": "indigo",
                        "kind": "Assignment", "due_at": "2026-10-23T03:30:00Z", "url": "/items/2"}]}
            """
        let feed = try Feed.decode(Data(json.utf8))
        #expect(feed.summary == "2 due today" && feed.notes.count == 1)
        #expect(feed.items[0].dueAt == at(23, 30))
        #expect(feed.upcoming(at: at(23, 31)).isEmpty)
    }

    @Test func aNewTimestampAloneIsNoChange() {
        let feed = Feed(
            version: 1, generatedAt: at(9), summary: "1 due today", notes: [],
            items: [item(due: at(23, 59))])
        var later = feed
        later.generatedAt = at(9, 1)
        #expect(later.shows(sameAs: feed))
        later.items[0].dueAt = at(23, 30)
        #expect(!later.shows(sameAs: feed))
        #expect(!feed.shows(sameAs: nil))
    }
}
