import AppKit
import os

/// Keeps a Mac pasteboard and one session's clipboard in sync, in both directions.
///
/// Mac -> Windows: a changed pasteboard (`checkPasteboard`, polled while watching) is announced as a
/// format list; the server then asks for data, which is read on the main thread and converted and
/// sent from a background queue. Windows -> Mac: a server format list becomes a lazy pasteboard item
/// (`RemotePasteboardPromise`); text is prefetched so it survives the session. Files: see
/// `ClipboardFiles`.
///
/// After a reconnect the channel comes up again and the sync announces the pasteboard afresh.
/// The pasteboard is injected: tests use a private named pasteboard, never `.general`.
@MainActor
public final class ClipboardSync: RemoteClipboardDelegate {
    public static let defaultMaxDataSize = 128 << 20
    public static let defaultMaxFileTransferSize = ClipboardFiles.defaultMaxTransferSize
    /// How long an app reading promised data may wait for the server.
    static let readTimeout: TimeInterval = 10

    /// Off: nothing is announced, published or answered (the channel stays open).
    public var isEnabled = true {
        didSet {
            guard isEnabled != oldValue else { return }
            lastSeenChangeCount = nil
            if isEnabled { checkPasteboard() }
        }
    }
    /// Format lists the server accepted from us; lets callers wait for an announcement to land.
    public private(set) var acceptedAnnouncements = 0
    /// Server clipboard changes published to the pasteboard.
    public private(set) var publishedRemoteChanges = 0
    /// Format lists the server announced (counted even while disabled); lets agents see a copy land.
    public private(set) var remoteChangeCount = 0
    /// Whether the clipboard channel is up.
    public var isChannelReady: Bool { channelReady }

    private let remote: RemoteClipboard
    private let pasteboard: NSPasteboard
    private let maxDataSize: Int
    private nonisolated let fetcher: RemoteDataFetcher
    private let flavors = ClipboardFlavors.all
    /// Converts and sends answers in request order.
    private let responder = DispatchQueue(label: "nrw.neuhaus.weitblick-remote.clipboard", qos: .userInitiated)
    private var channelReady = false
    private var lastSeenChangeCount: Int?
    /// The pasteboard change we made ourselves; never announced back (no echo).
    private var ownChangeCount: Int?
    private var announced: [UInt32: (flavor: any ClipboardFlavor, format: WindowsClipboardFormat)] = [:]
    private var promise: RemotePasteboardPromise?
    /// The server's latest format list.
    private var remoteFormats: [RemoteClipboardFormat] = []
    /// Text an agent put on the server clipboard (`offerText`); the server's clipboard owner is us
    /// until it announces a change or the pasteboard is announced again.
    private var offeredText: String?
    private nonisolated let files: ClipboardFiles
    private var poller: DispatchSourceTimer?
    private nonisolated static let logger = Logger(subsystem: "nrw.neuhaus.weitblick-remote", category: "clipboard")

    /// `maxDataSize` bounds one clipboard format (text, image, …), `maxFileTransferSize` the total
    /// of one file copy.
    public init(remote: RemoteClipboard, pasteboard: NSPasteboard, maxDataSize: Int = defaultMaxDataSize,
                maxFileTransferSize: UInt64 = defaultMaxFileTransferSize) {
        self.remote = remote
        self.pasteboard = pasteboard
        self.maxDataSize = maxDataSize
        self.fetcher = RemoteDataFetcher(maxSize: maxDataSize) { [remote] formatID in
            remote.request(formatID: formatID)
        }
        self.files = ClipboardFiles(remote: remote, maxTransferSize: maxFileTransferSize)
        remote.delegate = self
    }

    /// Polls the pasteboard's change count (cheap) until `stopWatching`.
    public func startWatching(interval: TimeInterval = 0.5) {
        guard poller == nil else { return }
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now() + interval, repeating: interval, leeway: .milliseconds(100))
        timer.setEventHandler { [weak self] in
            MainActor.assumeIsolated { self?.checkPasteboard() }
        }
        timer.resume()
        poller = timer
    }

    public func stopWatching() {
        poller?.cancel()
        poller = nil
    }

    /// Announces the pasteboard to the server if it changed since the last look.
    public func checkPasteboard() {
        announceLocal(force: false)
    }

    /// Call before the session goes away: remote text promised on the pasteboard is written out so
    /// it can still be pasted; everything else promised from this session is gone with it.
    public func sessionWillEnd() {
        stopWatching()
        remote.delegate = nil
        guard pasteboard.changeCount == ownChangeCount, let promise,
              let text = promise.cachedData(for: .string)
        else { return }
        pasteboard.clearContents()
        pasteboard.setData(text, forType: .string)
    }

    // MARK: Mac -> Windows

    private func announceLocal(force: Bool) {
        guard channelReady else { return }
        let changeCount = pasteboard.changeCount
        guard force || changeCount != lastSeenChangeCount else { return }
        lastSeenChangeCount = changeCount
        announced = [:]
        files.clearAnnouncement()
        var formats: [RemoteClipboardFormat] = []
        if isEnabled, changeCount != ownChangeCount {
            let types = pasteboard.types ?? []
            var nextRegisteredID = WindowsClipboardFormat.firstRegisteredID
            if let urls = files.localFileURLs(on: pasteboard), case .registered(let name) = FileGroupDescriptor.format {
                formats.append(RemoteClipboardFormat(id: nextRegisteredID, name: name))
                files.announced(urls, as: nextRegisteredID)
                nextRegisteredID += 1
            }
            for flavor in flavors where flavor.offers(types) {
                for format in flavor.windowsFormats {
                    let entry: RemoteClipboardFormat
                    switch format {
                    case .standard(let id):
                        entry = RemoteClipboardFormat(id: id)
                    case .registered(let name):
                        entry = RemoteClipboardFormat(id: nextRegisteredID, name: name)
                        nextRegisteredID += 1
                    }
                    announced[entry.id] = (flavor, format)
                    formats.append(entry)
                }
            }
        }
        // The channel wants an initial list even if empty; later empty lists mean nothing to send.
        guard force || !formats.isEmpty else { return }
        Self.logger.notice("announcing \(formats.count) formats: \(formats.map { $0.name ?? String($0.id) }, privacy: .public)")
        offeredText = nil
        remote.announce(formats)
    }

    private func respond(to formatID: UInt32) {
        let remote = remote
        let maxDataSize = maxDataSize
        if let offeredText, formatID == Self.unicodeTextID {
            let data = WindowsText.encode(offeredText)
            responder.async { remote.respond(with: data) }
            return
        }
        if isEnabled, formatID == files.announcedFormatID {
            return files.respondWithDescriptor(on: responder)
        }
        guard isEnabled, let entry = announced[formatID], let local = readLocal(entry.flavor) else {
            Self.logger.notice("server asked for format \(formatID), nothing to send")
            responder.async { remote.respond(with: nil) }
            return
        }
        responder.async {
            let data = entry.flavor.windowsData(from: local.data, type: local.type, as: entry.format)
            if let data, data.count > maxDataSize {
                Self.logger.notice("clipboard data of \(data.count) bytes exceeds the limit")
                remote.respond(with: nil)
            } else {
                remote.respond(with: data)
            }
        }
    }

    private func readLocal(_ flavor: any ClipboardFlavor) -> (type: NSPasteboard.PasteboardType, data: Data)? {
        for type in flavor.macTypes {
            if let data = pasteboard.data(forType: type) { return (type, data) }
        }
        return nil
    }

    // MARK: Text for agents (never touches the pasteboard)

    private static let unicodeTextID: UInt32 = 13

    /// The server clipboard as text, read straight from the channel: the text an agent offered if
    /// it is still the server's clipboard, else the server's CF_UNICODETEXT. nil when the clipboard
    /// holds no text, the channel is down or the server does not answer within `timeout`.
    public func remoteText(timeout: TimeInterval = 10) async -> String? {
        if let offeredText { return offeredText }
        guard channelReady, let format = remoteFormats.first(where: WindowsClipboardFormat.unicodeText.matches)
        else { return nil }
        let fetcher = fetcher
        let data = await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                continuation.resume(returning: fetcher.fetch(formatID: format.id, timeout: timeout))
            }
        }
        return data.map(WindowsText.decode)
    }

    /// Puts text on the server clipboard (announces CF_UNICODETEXT and answers the server's request
    /// with it, also while the sync is off). true once the server accepted the format list.
    public func offerText(_ text: String, timeout: TimeInterval = 5) async -> Bool {
        guard channelReady else { return false }
        let before = acceptedAnnouncements
        announced = [:]
        files.clearAnnouncement()
        offeredText = text
        guard remote.announce([RemoteClipboardFormat(id: Self.unicodeTextID)]) else {
            offeredText = nil
            return false
        }
        let deadline = Date().addingTimeInterval(timeout)
        while acceptedAnnouncements == before, Date() < deadline {
            try? await Task.sleep(for: .milliseconds(20))
        }
        return acceptedAnnouncements > before
    }

    // MARK: Windows -> Mac

    private func publish(_ formats: [RemoteClipboardFormat]) {
        remoteFormats = formats
        remoteChangeCount += 1
        offeredText = nil
        guard isEnabled else { return }
        Self.logger.notice("server clipboard: \(formats.map { $0.name ?? String($0.id) }, privacy: .public)")
        // Copied files (Explorer) come with names and shell formats, but the files are the content.
        if let descriptor = formats.first(where: FileGroupDescriptor.format.matches) {
            return files.receive(descriptorFormatID: descriptor.id, using: fetcher) { [weak self] items in
                self?.write(items, promise: nil)
            }
        }
        let offers = Self.offers(for: formats, flavors: flavors)
        guard !offers.isEmpty else { return }
        let promise = RemotePasteboardPromise(offers: offers, fetcher: fetcher, timeout: Self.readTimeout)
        let item = NSPasteboardItem()
        let types = flavors.flatMap(\.macTypes).filter { offers[$0] != nil }
        item.setDataProvider(promise, forTypes: types)
        write([item], promise: promise)

        if let text = offers[.string] {
            let fetcher = fetcher
            DispatchQueue.global(qos: .utility).async {
                _ = fetcher.fetch(formatID: text.formatID, timeout: 30)
            }
        }
    }

    private func write(_ items: [NSPasteboardItem], promise: RemotePasteboardPromise?) {
        pasteboard.clearContents()
        pasteboard.writeObjects(items)
        ownChangeCount = pasteboard.changeCount
        lastSeenChangeCount = ownChangeCount
        self.promise = promise
        publishedRemoteChanges += 1
    }

    /// The Mac types to promise for a server format list. Text wins over images: Office puts a
    /// rendered picture next to copied cells or text, which Mac apps would otherwise paste instead.
    static func offers(for formats: [RemoteClipboardFormat], flavors: [any ClipboardFlavor])
        -> [NSPasteboard.PasteboardType: RemotePasteboardPromise.Offer] {
        let hasText = formats.contains { WindowsClipboardFormat.unicodeText.matches($0) }
        var offers: [NSPasteboard.PasteboardType: RemotePasteboardPromise.Offer] = [:]
        for flavor in flavors where !(hasText && flavor is ImageFlavor) {
            let best = flavor.windowsFormats.lazy.compactMap { format in
                formats.first(where: format.matches).map { (format, $0.id) }
            }.first
            guard let (format, formatID) = best else { continue }
            for type in flavor.macTypes {
                offers[type] = RemotePasteboardPromise.Offer(flavor: flavor, format: format, formatID: formatID)
            }
        }
        return offers
    }

    // MARK: RemoteClipboardDelegate (channel thread)

    public nonisolated func remoteClipboardDidBecomeReady(_ clipboard: RemoteClipboard) {
        onMain { sync in
            sync.channelReady = true
            sync.announceLocal(force: true)
        }
    }

    public nonisolated func remoteClipboard(_ clipboard: RemoteClipboard, didAnswerAnnouncement accepted: Bool) {
        onMain { sync in if accepted { sync.acceptedAnnouncements += 1 } }
    }

    public nonisolated func remoteClipboard(_ clipboard: RemoteClipboard, didChangeFormats formats: [RemoteClipboardFormat]) {
        // Right away, so a late answer for the old clipboard is never cached for the new one.
        fetcher.reset()
        files.reset()
        onMain { $0.publish(formats) }
    }

    public nonisolated func remoteClipboard(_ clipboard: RemoteClipboard, didRequestFormat id: UInt32) {
        onMain { $0.respond(to: id) }
    }

    public nonisolated func remoteClipboard(_ clipboard: RemoteClipboard, didReceive data: Data?) {
        fetcher.deliver(data)
    }

    public nonisolated func remoteClipboard(_ clipboard: RemoteClipboard, didRequestFile request: RemoteFileRequest) {
        files.answer(request, on: responder)
    }

    public nonisolated func remoteClipboard(_ clipboard: RemoteClipboard, didReceiveFile streamID: UInt32, data: Data?) {
        files.fetcher.deliver(streamID: streamID, data: data)
    }

    public nonisolated func remoteClipboardDidClose(_ clipboard: RemoteClipboard) {
        fetcher.reset()
        files.reset()
        onMain { sync in
            sync.channelReady = false
            sync.announced = [:]
            sync.remoteFormats = []
            sync.offeredText = nil
            sync.files.clearAnnouncement()
        }
    }

    private nonisolated func onMain(_ body: @escaping @MainActor (ClipboardSync) -> Void) {
        DispatchQueue.main.async {
            MainActor.assumeIsolated { body(self) }
        }
    }
}
