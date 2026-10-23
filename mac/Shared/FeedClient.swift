import Foundation

/// What a fetch produced: the live feed, the last good one (with when it was fetched), or nothing.
enum FeedLoad: Equatable, Sendable {
    case live(Feed)
    case cached(Feed, asOf: Date)
    case unavailable

    var feed: Feed? {
        switch self {
        case .live(let f), .cached(let f, _): f
        case .unavailable: nil
        }
    }
}

/// Fetches `/widget.json` and keeps the last good copy in the target's own container.
struct FeedClient: Sendable {
    var base: URL = Argus.baseURL
    var cacheFile: URL = FeedClient.defaultCache

    static var defaultCache: URL {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return dir.appending(path: "Argus/widget.json")
    }

    func load() async -> FeedLoad {
        var request = URLRequest(url: base.appending(path: "widget.json"))
        request.timeoutInterval = 10
        request.cachePolicy = .reloadIgnoringLocalCacheData
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard (response as? HTTPURLResponse)?.statusCode == 200 else { return cached() }
            let feed = try Feed.decode(data)
            try? FileManager.default.createDirectory(
                at: cacheFile.deletingLastPathComponent(), withIntermediateDirectories: true)
            try? data.write(to: cacheFile, options: .atomic)
            return .live(feed)
        } catch {
            return cached()
        }
    }

    func cached() -> FeedLoad {
        guard let data = try? Data(contentsOf: cacheFile), let feed = try? Feed.decode(data) else {
            return .unavailable
        }
        let attrs = try? FileManager.default.attributesOfItem(atPath: cacheFile.path)
        let asOf = attrs?[.modificationDate] as? Date ?? feed.generatedAt
        return .cached(feed, asOf: asOf)
    }
}
