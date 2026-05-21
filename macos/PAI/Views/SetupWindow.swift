import SwiftUI

/// Shown once on first launch of a shipped build while `~/.pai` is provisioned.
/// Spinner + a tail of the provisioner log; on failure, the error and a Retry.
/// The window is dismissed by AppDelegate when provisioning succeeds.
struct SetupWindow: View {
    @ObservedObject var provisioner: Provisioner
    let onRetry: () -> Void

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: provisioner.lastError == nil
                  ? "bubble.left.and.bubble.right"
                  : "exclamationmark.triangle")
                .font(.system(size: 42, weight: .light))
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(provisioner.lastError == nil ? Color.accentColor : Color.orange)

            Text(provisioner.lastError == nil ? "Setting up PAI…" : "Setup failed")
                .font(.title2.weight(.semibold))

            Text(message)
                .font(.callout)
                .foregroundStyle(provisioner.lastError == nil ? .secondary : .primary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 440)

            if provisioner.inProgress {
                ProgressView()
                    .progressViewStyle(.linear)
                    .frame(width: 300)
            }

            logTail

            if provisioner.lastError != nil && !provisioner.inProgress {
                Button("Retry", action: onRetry)
                    .controlSize(.large)
                    .buttonStyle(.borderedProminent)
            }
        }
        .padding(28)
        .frame(width: 520, height: 400)
    }

    private var message: String {
        if let err = provisioner.lastError {
            return err + "\n\nFirst-run setup needs git and a network connection to fetch the package registry."
        }
        return "Laying out your PAI filesystem and fetching the package registry. This runs once."
    }

    private var logTail: some View {
        ScrollViewReader { proxy in
            ScrollView {
                Text(provisioner.log.isEmpty ? " " : provisioner.log)
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .id("logEnd")
            }
            .frame(width: 460, height: 130)
            .background(Color(nsColor: .textBackgroundColor))
            .clipShape(RoundedRectangle(cornerRadius: 6))
            .overlay(
                RoundedRectangle(cornerRadius: 6).strokeBorder(Color(nsColor: .separatorColor))
            )
            .onChange(of: provisioner.log) { _, _ in
                proxy.scrollTo("logEnd", anchor: .bottom)
            }
        }
    }
}
