import Foundation

/// Drops a YAML event into `~/.pai/run/pai/events/` for the kernel to
/// consume. Mirrors `src/boot/processes.py:55-68`:
///
///   - Filename: `<YYYYMMDDTHHMMSS><micros>-<source>.yaml`
///   - Atomic: write to `.yaml.tmp`, then `rename()` (so watchdog's
///     CREATE fires once and never on a partial file).
enum EventEmitter {
    static func newMessage(targetPid: Int, text: String) throws {
        let payload: [(String, String)] = [
            ("source", "menubar"),
            ("kind", "new_message"),
            ("thread", "me"),
            ("target_pid", String(targetPid)),
            ("text", yamlScalar(text)),
        ]
        try write(payload: payload, source: "menubar")
    }

    private static func write(payload: [(String, String)], source: String) throws {
        try FileManager.default.createDirectory(
            at: FHS.eventsDir, withIntermediateDirectories: true
        )

        let df = DateFormatter()
        df.locale = Locale(identifier: "en_US_POSIX")
        df.dateFormat = "yyyyMMdd'T'HHmmss"
        let now = Date()
        let stamp = df.string(from: now)
        // Python's %f → 6-digit microseconds. Approximate from a Date.
        let micros = Int((now.timeIntervalSince1970 - floor(now.timeIntervalSince1970)) * 1_000_000)
        let microStr = String(format: "%06d", micros)

        let filename = "\(stamp)\(microStr)-\(source).yaml"
        let finalURL = FHS.eventsDir.appendingPathComponent(filename)
        let tmpURL = FHS.eventsDir.appendingPathComponent(filename + ".tmp")

        var yaml = ""
        for (k, v) in payload { yaml += "\(k): \(v)\n" }
        try Data(yaml.utf8).write(to: tmpURL, options: .atomic)

        // POSIX rename — atomic on same filesystem. FileManager.replaceItem
        // does copy+delete which is NOT atomic and would defeat the point.
        let rc = rename(tmpURL.path, finalURL.path)
        if rc != 0 {
            try? FileManager.default.removeItem(at: tmpURL)
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
        }
    }

    /// Render `text` as a YAML scalar that survives newlines/colons/quotes.
    /// Uses double-quoted form with JSON-style escapes — a valid YAML 1.1+
    /// scalar that yaml.safe_load on the kernel side accepts.
    private static func yamlScalar(_ text: String) -> String {
        var escaped = ""
        escaped.reserveCapacity(text.count + 2)
        escaped.append("\"")
        for ch in text.unicodeScalars {
            switch ch {
            case "\\": escaped.append("\\\\")
            case "\"": escaped.append("\\\"")
            case "\n": escaped.append("\\n")
            case "\r": escaped.append("\\r")
            case "\t": escaped.append("\\t")
            default:
                if ch.value < 0x20 {
                    escaped.append(String(format: "\\x%02X", ch.value))
                } else {
                    escaped.unicodeScalars.append(ch)
                }
            }
        }
        escaped.append("\"")
        return escaped
    }
}
