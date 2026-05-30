import SwiftUI

/// First-run model picker + API-key entry. The GUI twin of install.sh's
/// "Which model should PAI use?" prompt followed by its hidden key read.
/// Shown before the provisioning spinner; `onContinue` hands the chosen
/// provider back to AppDelegate, which seeds config.yaml and writes the key.
struct ModelSetupView: View {
    @ObservedObject var setup: ModelSetup
    /// Called with the chosen provider (key already typed into `setup.apiKey`).
    let onContinue: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            content
            Divider()
            footer
        }
        .frame(width: 520, height: 460)
    }

    private var header: some View {
        HStack(spacing: 12) {
            Image(systemName: "key.horizontal")
                .font(.system(size: 28, weight: .light))
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(Color.accentColor)
            VStack(alignment: .leading, spacing: 2) {
                Text("Connect a model")
                    .font(.title2.weight(.semibold))
                Text("PAI runs on a hosted model. Pick a provider and paste its API key. Stored locally in ~/.pai/.env.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer()
        }
        .padding(20)
    }

    private var content: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                ForEach(ModelSetup.providers) { provider in
                    providerRow(provider)
                }

                VStack(alignment: .leading, spacing: 6) {
                    Text(setup.selected.envVar)
                        .font(.caption.weight(.semibold).monospaced())
                        .foregroundStyle(.secondary)
                    SecureField("Paste your \(setup.selected.label) API key", text: $setup.apiKey)
                        .textFieldStyle(.roundedBorder)
                        .onSubmit { onContinue() }
                    Text("Leave blank to skip — you can add the key to ~/.pai/.env later, but PAI won't respond until you do.")
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(.top, 4)
            }
            .padding(20)
        }
    }

    private func providerRow(_ provider: ModelSetup.Provider) -> some View {
        Button {
            setup.selected = provider
        } label: {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: setup.selected == provider ? "largecircle.fill.circle" : "circle")
                    .foregroundStyle(setup.selected == provider ? Color.accentColor : Color.secondary)
                    .font(.system(size: 16))
                VStack(alignment: .leading, spacing: 1) {
                    HStack(spacing: 6) {
                        Text(provider.label).font(.body.weight(.medium))
                        Text(provider.model)
                            .font(.caption2.monospaced())
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 5).padding(.vertical, 1)
                            .background(Color.secondary.opacity(0.15), in: Capsule())
                    }
                    Text(provider.blurb)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private var footer: some View {
        HStack {
            Spacer()
            Button(isBlank ? "Skip for now" : "Continue") { onContinue() }
                .keyboardShortcut(.defaultAction)
                .buttonStyle(.borderedProminent)
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 14)
    }

    private var isBlank: Bool {
        setup.apiKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
}
