import SwiftUI
import AppKit

/// Token-entry screen for remote access's one-time ngrok setup. Reused in two
/// places: the optional first-run wizard step (full size) and the QR panel when
/// the user enables remote access without a token yet (`compact`).
///
/// `onSaved` fires after the token is stored; `onSkip` is "set up later" /
/// cancel. The caller decides what each means (first-run just proceeds; the
/// toggle path retries the tunnel on save and turns remote off on skip).
struct NgrokSetupView: View {
    @ObservedObject var setup: NgrokSetup
    var compact: Bool = false
    var onSaved: () -> Void
    var onSkip: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            header
            TextField("Paste your ngrok authtoken", text: $setup.token)
                .textFieldStyle(.roundedBorder)
                .font(.body.monospaced())
                .disabled(setup.busy)
                .onSubmit(save)

            Button {
                NSWorkspace.shared.open(Ngrok.dashboardURL)
            } label: {
                Label("Get a free token at ngrok.com", systemImage: "arrow.up.right.square")
                    .font(.callout)
            }
            .buttonStyle(.link)

            if let error = setup.error {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }

            footer
        }
        .padding(compact ? 0 : 22)
        .frame(width: compact ? 300 : 480)
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 12) {
            if !compact {
                Image(systemName: "antenna.radiowaves.left.and.right")
                    .font(.system(size: 26, weight: .light))
                    .symbolRenderingMode(.hierarchical)
                    .foregroundStyle(Color.accentColor)
            }
            VStack(alignment: .leading, spacing: 3) {
                Text("Remote access (optional)")
                    .font(compact ? .headline : .title3.weight(.semibold))
                Text("To reach PAI from your phone, it tunnels through ngrok. Paste a free ngrok authtoken once and the “Enable remote access” toggle just works. You can skip this and set it up later.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var footer: some View {
        HStack {
            Button("Set up later", action: onSkip)
                .disabled(setup.busy)
            Spacer()
            if setup.busy {
                ProgressView().controlSize(.small)
            }
            Button("Save") { save() }
                .keyboardShortcut(.defaultAction)
                .buttonStyle(.borderedProminent)
                .disabled(setup.busy || setup.token.trimmingCharacters(in: .whitespaces).isEmpty)
        }
    }

    private func save() {
        Task { @MainActor in
            if await setup.save() { onSaved() }
        }
    }
}
