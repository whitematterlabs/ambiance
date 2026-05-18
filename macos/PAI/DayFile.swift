import Foundation
import Combine

struct Message: Identifiable, Hashable {
    let id = UUID()
    let sender: String   // "me", "pai", or other slug
    let time: String     // "HH:MM"
    let body: String
}

/// Tails today's me/<pid>/<date>.md, parses on the `[HH:MM] sender:`
/// header boundary, and republishes the message list on every change.
/// Mirrors `src/sbin/tui/state.py:74-133`.
@MainActor
final class DayFileWatcher: ObservableObject {
    @Published private(set) var messages: [Message] = []
    @Published private(set) var path: URL

    private let pid: Int
    private var source: DispatchSourceFileSystemObject?
    private var dirFd: Int32 = -1
    private var pollTimer: Timer?
    // Header regex: line starting with "[HH:MM] sender:"
    private static let header = try! NSRegularExpression(
        pattern: #"^\[(\d{2}:\d{2})\] (\S+?):\s?(.*)$"#
    )

    init(pid: Int) {
        self.pid = pid
        self.path = FHS.dayFile(pid: pid)
    }

    func start() {
        ensureFile()
        reload()
        watchParent()
        // Belt-and-suspenders 0.5s poll: FSEvents/DispatchSource can coalesce
        // writes and miss the last one. Cheap — one stat per tick.
        pollTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.reloadIfChanged() }
        }
    }

    func stop() {
        pollTimer?.invalidate()
        pollTimer = nil
        source?.cancel()
        source = nil
        if dirFd >= 0 { close(dirFd); dirFd = -1 }
    }

    /// Append `[HH:MM] me: <text>` to the day-file, then publish a
    /// `new_message` event so the kernel wakes the target PAI.
    func sendFromMe(_ text: String) throws {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        try ensureDir()
        let df = DateFormatter()
        df.locale = Locale(identifier: "en_US_POSIX")
        df.dateFormat = "HH:mm"
        let hm = df.string(from: Date())
        let line = "[\(hm)] me: \(trimmed)\n"
        let data = Data(line.utf8)

        let fm = FileManager.default
        if !fm.fileExists(atPath: path.path) {
            try data.write(to: path)
        } else {
            let handle = try FileHandle(forWritingTo: path)
            defer { try? handle.close() }
            try handle.seekToEnd()
            try handle.write(contentsOf: data)
        }

        try EventEmitter.newMessage(targetPid: pid, text: trimmed)
        reload()
    }

    private var lastSize: Int = -1
    private var lastMTime: TimeInterval = -1

    private func reloadIfChanged() {
        let attrs = try? FileManager.default.attributesOfItem(atPath: path.path)
        let size = (attrs?[.size] as? NSNumber)?.intValue ?? 0
        let mtime = (attrs?[.modificationDate] as? Date)?.timeIntervalSince1970 ?? 0
        if size == lastSize && mtime == lastMTime { return }
        lastSize = size
        lastMTime = mtime
        reload()
    }

    private func reload() {
        guard let text = try? String(contentsOf: path, encoding: .utf8) else {
            if !messages.isEmpty { messages = [] }
            return
        }
        var built: [Message] = []
        var curTime: String?
        var curSender: String?
        var curLines: [String] = []
        for rawLine in text.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = String(rawLine)
            let range = NSRange(line.startIndex..<line.endIndex, in: line)
            if let m = Self.header.firstMatch(in: line, options: [], range: range) {
                if let t = curTime, let s = curSender {
                    built.append(Message(sender: s, time: t, body: joined(curLines)))
                }
                let time = String(line[Range(m.range(at: 1), in: line)!])
                let sender = String(line[Range(m.range(at: 2), in: line)!])
                let rest = String(line[Range(m.range(at: 3), in: line)!])
                curTime = time
                curSender = sender
                curLines = rest.isEmpty ? [] : [rest]
            } else if curTime != nil {
                curLines.append(line)
            }
        }
        if let t = curTime, let s = curSender {
            built.append(Message(sender: s, time: t, body: joined(curLines)))
        }
        if built != messages { messages = built }
    }

    private func joined(_ lines: [String]) -> String {
        lines.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func ensureDir() throws {
        try FileManager.default.createDirectory(
            at: path.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
    }

    private func ensureFile() {
        try? ensureDir()
        if !FileManager.default.fileExists(atPath: path.path) {
            FileManager.default.createFile(atPath: path.path, contents: nil)
        }
    }

    private func watchParent() {
        let dir = path.deletingLastPathComponent().path
        let fd = open(dir, O_EVTONLY)
        guard fd >= 0 else { return }
        dirFd = fd
        let src = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd,
            eventMask: [.write, .extend, .delete, .rename],
            queue: .main
        )
        src.setEventHandler { [weak self] in
            self?.reloadIfChanged()
        }
        src.setCancelHandler { [weak self] in
            if let fd = self?.dirFd, fd >= 0 { close(fd) }
            self?.dirFd = -1
        }
        src.resume()
        source = src
    }
}
