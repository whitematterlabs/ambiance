import SwiftUI

struct ChatWindow: View {
    let pai: PAIInfo
    @ObservedObject var events: EventsTailer
    @StateObject private var watcher: DayFileWatcher
    @StateObject private var speaker = Speaker()
    @State private var draft: String = ""
    @State private var sendError: String?
    @State private var voiceMode: Bool = false
    @State private var lastSpokenId: Message.ID?
    @AppStorage("eventsInspectorVisible") private var showEvents: Bool = true

    init(pai: PAIInfo, events: EventsTailer) {
        self.pai = pai
        self.events = events
        _watcher = StateObject(wrappedValue: DayFileWatcher(pid: pai.pid))
    }

    var body: some View {
        chatColumn
            .inspector(isPresented: $showEvents) {
                EventStripView(tailer: events)
                    .inspectorColumnWidth(min: 220, ideal: 280, max: 480)
            }
            .navigationTitle("\(pai.slug) #\(pai.pid)")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button(action: toggleVoice) {
                        Image(systemName: voiceMode ? "speaker.wave.2.fill" : "speaker.slash")
                            .symbolRenderingMode(.hierarchical)
                            .foregroundStyle(voiceMode ? Color.accentColor : Color.secondary)
                    }
                    .help(voiceMode ? "Voice mode: on (click to mute)" : "Voice mode: off (click to read replies aloud)")
                }
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        showEvents.toggle()
                    } label: {
                        Image(systemName: "sidebar.right")
                            .symbolRenderingMode(.hierarchical)
                            .foregroundStyle(showEvents ? Color.accentColor : Color.secondary)
                    }
                    .help(showEvents ? "Hide events" : "Show events")
                }
            }
    }

    private var chatColumn: some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 14) {
                        ForEach(watcher.messages) { msg in
                            messageRow(msg).id(msg.id)
                        }
                    }
                    .padding(.horizontal, 18)
                    .padding(.vertical, 16)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .background(
                    LinearGradient(
                        colors: [
                            Color(nsColor: .windowBackgroundColor),
                            Color(nsColor: .windowBackgroundColor).opacity(0.6)
                        ],
                        startPoint: .top, endPoint: .bottom
                    )
                )
                .onChange(of: watcher.messages) { _, new in
                    if let last = new.last {
                        withAnimation(.easeOut(duration: 0.18)) {
                            proxy.scrollTo(last.id, anchor: .bottom)
                        }
                    }
                }
            }
            composer
        }
        .frame(minWidth: 520, minHeight: 380)
        .onAppear { watcher.start() }
        .onDisappear {
            watcher.stop()
            speaker.stop()
        }
        .onChange(of: watcher.messages) { _, new in
            guard voiceMode, let last = new.last else { return }
            if last.id == lastSpokenId { return }
            lastSpokenId = last.id
            if last.sender != "me" {
                speaker.speak(last.body)
            }
        }
    }

    private func toggleVoice() {
        voiceMode.toggle()
        if voiceMode {
            // Don't replay history when toggling on — baseline at current tail.
            lastSpokenId = watcher.messages.last?.id
        } else {
            speaker.stop()
        }
    }

    private var composer: some View {
        VStack(spacing: 0) {
            Divider().opacity(0.5)
            HStack(alignment: .bottom, spacing: 10) {
                TextField("message \(pai.slug)…", text: $draft, axis: .vertical)
                    .textFieldStyle(.plain)
                    .lineLimit(1...6)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
                    .background(
                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .fill(Color(nsColor: .textBackgroundColor))
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .strokeBorder(Color.secondary.opacity(0.25), lineWidth: 0.5)
                    )
                    .onSubmit(send)
                Button(action: interrupt) {
                    Image(systemName: "stop.circle.fill")
                        .font(.system(size: 22))
                        .symbolRenderingMode(.hierarchical)
                        .foregroundStyle(Color.secondary)
                }
                .buttonStyle(.plain)
                .keyboardShortcut(.escape, modifiers: [])
                .help("Interrupt PAI (Esc)")
                Button(action: send) {
                    Image(systemName: "arrow.up.circle.fill")
                        .font(.system(size: 24))
                        .symbolRenderingMode(.hierarchical)
                        .foregroundStyle(
                            draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                                ? Color.secondary : Color.accentColor
                        )
                }
                .buttonStyle(.plain)
                .keyboardShortcut(.return, modifiers: [.command])
                .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .help("Send (⌘↩)")
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            if let err = sendError {
                Text(err)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .padding(.horizontal, 14)
                    .padding(.bottom, 8)
            }
        }
        .background(.regularMaterial)
    }

    private func messageRow(_ msg: Message) -> some View {
        let mine = msg.sender == "me"
        // PAI replies share the tab color; other senders (subagents,
        // drivers writing into the me-thread) get a slot-stable hue too.
        let tint: Color = mine ? .accentColor : palette(for: pai.pid)
        return HStack(alignment: .top, spacing: 0) {
            if mine { Spacer(minLength: 70) }
            VStack(alignment: mine ? .trailing : .leading, spacing: 4) {
                MarkdownBlocks(text: msg.body)
                    .textSelection(.enabled)
                    .padding(.horizontal, 13).padding(.vertical, 9)
                    .background(
                        RoundedRectangle(cornerRadius: 14, style: .continuous)
                            .fill(tint.opacity(mine ? 0.16 : 0.10))
                    )
                HStack(spacing: 5) {
                    if !mine {
                        Text(msg.sender)
                            .font(.caption2.weight(.medium))
                            .foregroundStyle(tint.opacity(0.85))
                    }
                    Text(msg.time)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
                .padding(.horizontal, 4)
            }
            if !mine { Spacer(minLength: 70) }
        }
    }

    private func send() {
        let text = draft
        do {
            try watcher.sendFromMe(text)
            draft = ""
            sendError = nil
        } catch {
            sendError = "send failed: \(error.localizedDescription)"
        }
    }

    private func interrupt() {
        speaker.stop()
        do {
            try EventEmitter.interrupt(targetPid: pai.pid)
            sendError = nil
        } catch {
            sendError = "interrupt failed: \(error.localizedDescription)"
        }
    }
}

/// Reads text aloud via `/usr/bin/say`. One utterance at a time — a new
/// `speak` kills the in-flight process so the latest reply takes over
/// instead of queueing behind stale output. `stop()` is used by the
/// interrupt button and on toggle-off.
@MainActor
final class Speaker: ObservableObject {
    private var current: Process?

    func speak(_ text: String) {
        let cleaned = Self.strip(text)
        guard !cleaned.isEmpty else { return }
        stop()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/say")
        p.arguments = [cleaned]
        do {
            try p.run()
            current = p
            p.terminationHandler = { [weak self] proc in
                Task { @MainActor in
                    if self?.current === proc { self?.current = nil }
                }
            }
        } catch {
            current = nil
        }
    }

    func stop() {
        if let p = current, p.isRunning {
            p.terminate()
        }
        current = nil
    }

    /// `say` reads punctuation literally and stumbles on code/URLs.
    /// Strip fenced code, inline code, and markdown link syntax.
    private static func strip(_ text: String) -> String {
        var s = text
        // Fenced code blocks.
        s = s.replacingOccurrences(
            of: #"```[\s\S]*?```"#, with: " ", options: .regularExpression
        )
        // Inline code.
        s = s.replacingOccurrences(
            of: #"`[^`]*`"#, with: " ", options: .regularExpression
        )
        // Markdown links [label](url) -> label.
        s = s.replacingOccurrences(
            of: #"\[([^\]]+)\]\([^)]*\)"#, with: "$1", options: .regularExpression
        )
        // Heading / list markers at line start.
        s = s.replacingOccurrences(
            of: #"(?m)^\s*[#>\-\*]+\s*"#, with: "", options: .regularExpression
        )
        return s.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
