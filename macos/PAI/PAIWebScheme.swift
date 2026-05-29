import Foundation
@preconcurrency import WebKit

/// `WKURLSchemeHandler` for `pai://app/...`. Proxies *every* request — both
/// the React dist (`/`, `/assets/…`) and API calls (`/api/*`) — over a
/// unix-domain socket to pai_web. The browser-side code is unchanged: it
/// fetches relative paths which resolve to `pai://app/...` and land here.
///
/// Why this exists: keeping the local owner surface off the loopback TCP port
/// leaves that port free for a future ngrok-tunneled remote surface. The .app
/// talks to pai_web over a private unix socket; remote (if/when opted in) gets
/// its own TCP listener.
final class PAIWebSchemeHandler: NSObject, WKURLSchemeHandler, @unchecked Sendable {
    static let scheme = "pai"

    private let socketPath: String

    /// Background queue for the blocking socket I/O so the main thread stays
    /// responsive while SSE streams.
    private let ioQueue = DispatchQueue(
        label: "pai.web.scheme.io",
        qos: .userInitiated,
        attributes: .concurrent
    )

    /// Per-task cancellation flag, flipped by `stop(urlSchemeTask:)`. The
    /// background thread checks it before calling back into the task.
    private var canceled = NSHashTable<AnyObject>.weakObjects()
    private let canceledLock = NSLock()

    init(socketPath: String) {
        self.socketPath = socketPath
    }

    func webView(_ webView: WKWebView, start task: WKURLSchemeTask) {
        let url = task.request.url ?? URL(string: "pai://app/")!
        ioQueue.async { [weak self] in
            self?.proxy(task: task, url: url)
        }
    }

    func webView(_ webView: WKWebView, stop task: WKURLSchemeTask) {
        canceledLock.lock()
        canceled.add(task)
        canceledLock.unlock()
    }

    private func isCanceled(_ task: WKURLSchemeTask) -> Bool {
        canceledLock.lock()
        defer { canceledLock.unlock() }
        return canceled.contains(task)
    }

    // MARK: - proxy (unix socket → pai_web)

    private func proxy(task: WKURLSchemeTask, url: URL) {
        if isCanceled(task) { return }
        let request = task.request
        let body = Self.drainBody(request: request)
        let method = request.httpMethod ?? "GET"
        var path = url.path.isEmpty ? "/" : url.path
        if let q = url.query, !q.isEmpty { path += "?\(q)" }

        let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        if fd < 0 {
            return fail(task, status: 502, reason: "socket() failed")
        }
        defer { Darwin.close(fd) }

        guard connectUnix(fd: fd, path: socketPath) else {
            return fail(task, status: 502, reason: "web server not reachable")
        }

        // Build the request head. Connection: close avoids keep-alive book-
        // keeping; we open a fresh socket per request.
        var head = "\(method) \(path) HTTP/1.1\r\n"
        head += "Host: app\r\n"
        head += "Connection: close\r\n"
        if let ct = request.value(forHTTPHeaderField: "Content-Type") {
            head += "Content-Type: \(ct)\r\n"
        }
        if let accept = request.value(forHTTPHeaderField: "Accept") {
            head += "Accept: \(accept)\r\n"
        }
        if !body.isEmpty {
            head += "Content-Length: \(body.count)\r\n"
        } else if method != "GET" && method != "HEAD" {
            head += "Content-Length: 0\r\n"
        }
        head += "\r\n"

        guard Self.writeAll(fd, Data(head.utf8)) else {
            return fail(task, status: 502, reason: "write request failed")
        }
        if !body.isEmpty {
            guard Self.writeAll(fd, body) else {
                return fail(task, status: 502, reason: "write body failed")
            }
        }

        // Read until end-of-headers (\r\n\r\n).
        var buf = Data()
        var headerEnd = -1
        while headerEnd < 0 {
            if isCanceled(task) { return }
            var tmp = [UInt8](repeating: 0, count: 4096)
            let n = tmp.withUnsafeMutableBufferPointer { Darwin.read(fd, $0.baseAddress, $0.count) }
            if n <= 0 {
                return fail(task, status: 502, reason: "upstream closed before headers")
            }
            buf.append(tmp, count: n)
            if let r = buf.range(of: Data([0x0d, 0x0a, 0x0d, 0x0a])) {
                headerEnd = r.upperBound
            }
            if buf.count > 64 * 1024 && headerEnd < 0 {
                return fail(task, status: 502, reason: "response headers too large")
            }
        }

        let headerBytes = buf.subdata(in: 0..<headerEnd - 4)
        guard let headerText = String(data: headerBytes, encoding: .ascii) else {
            return fail(task, status: 502, reason: "bad response headers")
        }
        let lines = headerText.components(separatedBy: "\r\n")
        guard let status = Self.parseStatus(lines.first ?? "") else {
            return fail(task, status: 502, reason: "bad status line")
        }
        var headers: [String: String] = [:]
        var transferEncoding = ""
        for line in lines.dropFirst() {
            guard let colon = line.firstIndex(of: ":") else { continue }
            let name = String(line[..<colon]).trimmingCharacters(in: .whitespaces)
            let value = String(line[line.index(after: colon)...]).trimmingCharacters(in: .whitespaces)
            if name.isEmpty { continue }
            if name.lowercased() == "transfer-encoding" {
                transferEncoding = value.lowercased()
                continue  // hop-by-hop — don't forward to WebKit
            }
            if name.lowercased() == "connection" { continue }
            headers[name] = value
        }

        let response = HTTPURLResponse(
            url: url,
            statusCode: status,
            httpVersion: "HTTP/1.1",
            headerFields: headers
        )!

        if isCanceled(task) { return }
        callOnMain { if !self.isCanceled(task) { task.didReceive(response) } }

        let leftover = headerEnd < buf.count ? buf.subdata(in: headerEnd..<buf.count) : Data()
        if transferEncoding.contains("chunked") {
            streamChunked(task: task, fd: fd, initial: leftover)
        } else {
            streamIdentity(task: task, fd: fd, initial: leftover)
        }
    }

    /// Forward body bytes verbatim until EOF. Used for Content-Length AND for
    /// the headerless SSE stream (server keeps the connection open and writes
    /// `data: …\n\n` chunks; we relay each byte the moment it arrives so
    /// `EventSource` sees events live).
    private func streamIdentity(task: WKURLSchemeTask, fd: Int32, initial: Data) {
        if !initial.isEmpty {
            if isCanceled(task) { return }
            callOnMain { if !self.isCanceled(task) { task.didReceive(initial) } }
        }
        var tmp = [UInt8](repeating: 0, count: 4096)
        while true {
            if isCanceled(task) { return }
            let n = tmp.withUnsafeMutableBufferPointer { Darwin.read(fd, $0.baseAddress, $0.count) }
            if n <= 0 { break }
            let chunk = Data(tmp.prefix(n))
            if isCanceled(task) { return }
            callOnMain { if !self.isCanceled(task) { task.didReceive(chunk) } }
        }
        callOnMain { if !self.isCanceled(task) { task.didFinish() } }
    }

    /// Decode HTTP/1.1 chunked transfer-encoding and forward the de-chunked
    /// payload to the task. pai_web's stdlib server doesn't currently emit
    /// chunked encoding, but a future framework swap might — handle it.
    private func streamChunked(task: WKURLSchemeTask, fd: Int32, initial: Data) {
        var buf = initial
        var tmp = [UInt8](repeating: 0, count: 4096)
        func readMore() -> Bool {
            let n = tmp.withUnsafeMutableBufferPointer { Darwin.read(fd, $0.baseAddress, $0.count) }
            if n <= 0 { return false }
            buf.append(tmp, count: n)
            return true
        }
        while true {
            // Read chunk-size line.
            while buf.range(of: Data([0x0d, 0x0a])) == nil {
                if isCanceled(task) { return }
                if !readMore() {
                    return callOnMain {
                        if !self.isCanceled(task) { task.didFinish() }
                    }
                }
            }
            let crlf = buf.range(of: Data([0x0d, 0x0a]))!
            let sizeLine = String(data: buf.subdata(in: 0..<crlf.lowerBound), encoding: .ascii) ?? ""
            buf.removeSubrange(0..<crlf.upperBound)
            let hex = sizeLine.split(separator: ";").first.map(String.init) ?? sizeLine
            guard let size = Int(hex.trimmingCharacters(in: .whitespaces), radix: 16) else {
                return callOnMain { if !self.isCanceled(task) { task.didFinish() } }
            }
            if size == 0 {
                return callOnMain { if !self.isCanceled(task) { task.didFinish() } }
            }
            // Read `size` payload bytes + trailing CRLF.
            while buf.count < size + 2 {
                if isCanceled(task) { return }
                if !readMore() {
                    return callOnMain {
                        if !self.isCanceled(task) { task.didFinish() }
                    }
                }
            }
            let payload = buf.subdata(in: 0..<size)
            buf.removeSubrange(0..<size + 2)
            if isCanceled(task) { return }
            callOnMain { if !self.isCanceled(task) { task.didReceive(payload) } }
        }
    }

    // MARK: - helpers

    private func deliver(_ task: WKURLSchemeTask, response: URLResponse, body: Data) {
        callOnMain {
            if self.isCanceled(task) { return }
            task.didReceive(response)
            task.didReceive(body)
            task.didFinish()
        }
    }

    private func fail(_ task: WKURLSchemeTask, status: Int, reason: String) {
        let body = Data("{\"ok\":false,\"error\":\"\(reason)\"}".utf8)
        let headers = [
            "Content-Type": "application/json",
            "Content-Length": "\(body.count)",
        ]
        let response = HTTPURLResponse(
            url: task.request.url!,
            statusCode: status,
            httpVersion: "HTTP/1.1",
            headerFields: headers
        )!
        deliver(task, response: response, body: body)
    }

    private func callOnMain(_ block: @escaping @Sendable () -> Void) {
        if Thread.isMainThread {
            block()
        } else {
            DispatchQueue.main.async(execute: block)
        }
    }

    private func connectUnix(fd: Int32, path: String) -> Bool {
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let cap = MemoryLayout.size(ofValue: addr.sun_path)
        let bytes = Array(path.utf8)
        if bytes.count >= cap { return false }
        let ok = withUnsafeMutablePointer(to: &addr.sun_path) { tuplePtr -> Bool in
            tuplePtr.withMemoryRebound(to: CChar.self, capacity: cap) { cptr in
                for (i, b) in bytes.enumerated() { cptr[i] = CChar(bitPattern: b) }
                cptr[bytes.count] = 0
                return true
            }
        }
        if !ok { return false }
        let len = socklen_t(MemoryLayout<sockaddr_un>.size)
        let rc = withUnsafePointer(to: &addr) { ptr -> Int32 in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sptr in
                Darwin.connect(fd, sptr, len)
            }
        }
        return rc == 0
    }

    private static func writeAll(_ fd: Int32, _ data: Data) -> Bool {
        var written = 0
        return data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) -> Bool in
            guard let base = raw.baseAddress else { return true }
            while written < data.count {
                let n = Darwin.write(fd, base.advanced(by: written), data.count - written)
                if n <= 0 { return false }
                written += n
            }
            return true
        }
    }

    private static func parseStatus(_ line: String) -> Int? {
        // "HTTP/1.1 200 OK"
        let parts = line.split(separator: " ", maxSplits: 2, omittingEmptySubsequences: true)
        if parts.count < 2 { return nil }
        return Int(parts[1])
    }

    /// `URLRequest.httpBody` is often nil because WKWebView gives us
    /// `httpBodyStream`; drain it into Data so we can write a real
    /// `Content-Length` upstream.
    private static func drainBody(request: URLRequest) -> Data {
        if let body = request.httpBody { return body }
        guard let stream = request.httpBodyStream else { return Data() }
        stream.open()
        defer { stream.close() }
        var out = Data()
        let bufSize = 8192
        let ptr = UnsafeMutablePointer<UInt8>.allocate(capacity: bufSize)
        defer { ptr.deallocate() }
        while stream.hasBytesAvailable {
            let n = stream.read(ptr, maxLength: bufSize)
            if n <= 0 { break }
            out.append(ptr, count: n)
        }
        return out
    }
}
