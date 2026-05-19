import SwiftUI

struct ChatWindow: View {
    let pai: PAIInfo
    @StateObject private var watcher: DayFileWatcher
    @State private var draft: String = ""
    @State private var sendError: String?

    init(pai: PAIInfo) {
        self.pai = pai
        _watcher = StateObject(wrappedValue: DayFileWatcher(pid: pai.pid))
    }

    var body: some View {
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
        .navigationTitle("\(pai.slug) #\(pai.pid)")
        .onAppear { watcher.start() }
        .onDisappear { watcher.stop() }
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
}
