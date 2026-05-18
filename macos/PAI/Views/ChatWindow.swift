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
                    LazyVStack(alignment: .leading, spacing: 8) {
                        ForEach(watcher.messages) { msg in
                            messageRow(msg).id(msg.id)
                        }
                    }
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .onChange(of: watcher.messages) { _, new in
                    if let last = new.last { proxy.scrollTo(last.id, anchor: .bottom) }
                }
            }
            Divider()
            HStack(alignment: .bottom, spacing: 8) {
                TextField("message \(pai.slug)…", text: $draft, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1...6)
                    .onSubmit(send)
                Button("Send", action: send)
                    .keyboardShortcut(.return, modifiers: [.command])
                    .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
            .padding(10)
            if let err = sendError {
                Text(err).font(.caption).foregroundStyle(.red).padding(.bottom, 6)
            }
        }
        .frame(minWidth: 480, minHeight: 360)
        .navigationTitle("\(pai.slug) #\(pai.pid)")
        .onAppear { watcher.start() }
        .onDisappear { watcher.stop() }
    }

    private func messageRow(_ msg: Message) -> some View {
        let mine = msg.sender == "me"
        return HStack {
            if mine { Spacer(minLength: 40) }
            VStack(alignment: mine ? .trailing : .leading, spacing: 2) {
                Text("\(msg.sender) · \(msg.time)")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Text(msg.body)
                    .textSelection(.enabled)
                    .padding(.horizontal, 10).padding(.vertical, 6)
                    .background(
                        RoundedRectangle(cornerRadius: 8)
                            .fill(mine ? Color.accentColor.opacity(0.18)
                                       : Color.gray.opacity(0.15))
                    )
            }
            if !mine { Spacer(minLength: 40) }
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
