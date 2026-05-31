import SwiftUI
import CoreImage
import CoreImage.CIFilterBuiltins
import AppKit

/// The "scan to open / type to log in" panel shown when remote access is on.
/// Renders a QR of the connect URL (CoreImage, no dependency) plus the public
/// URL and the access code as text. Presented as a sheet from the StatusBar
/// toggle; updates live as `RemoteAccess` moves starting → ready / failed.
struct RemoteAccessPanel: View {
    @ObservedObject var remote: RemoteAccess
    var onClose: () -> Void

    // Token-entry shown when remote is toggled on without ngrok configured.
    @StateObject private var ngrok = NgrokSetup()

    var body: some View {
        // `.needsAuth` is self-contained (it carries its own Save / Set-up-later
        // footer), so it renders without the panel chrome.
        if case .needsAuth = remote.phase {
            NgrokSetupView(
                setup: ngrok,
                compact: true,
                onSaved: { remote.toggle(true) },          // configured → retry
                onSkip: { remote.toggle(false); onClose() }  // leave it off
            )
            .padding(22)
        } else {
            chrome
        }
    }

    private var chrome: some View {
        VStack(spacing: 16) {
            Text("Remote access")
                .font(.headline)

            switch remote.phase {
            case .starting, .off, .needsAuth:
                VStack(spacing: 12) {
                    ProgressView()
                    Text("Starting remote access…")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                .frame(width: 280, height: 200)

            case .failed(let message):
                VStack(spacing: 12) {
                    Image(systemName: "exclamationmark.triangle")
                        .font(.system(size: 30))
                        .foregroundStyle(.orange)
                    Text(message)
                        .font(.callout)
                        .multilineTextAlignment(.center)
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                        .frame(maxWidth: 300)
                }
                .frame(minWidth: 300)

            case .ready:
                readyBody
            }

            Button("Done", action: onClose)
                .keyboardShortcut(.defaultAction)
        }
        .padding(22)
        .frame(minWidth: 340)
    }

    @ViewBuilder
    private var readyBody: some View {
        VStack(spacing: 14) {
            if let url = remote.connectURL, let qr = Self.makeQR(url) {
                Image(nsImage: qr)
                    .interpolation(.none)
                    .resizable()
                    .frame(width: 220, height: 220)
                    .background(Color.white)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
            }
            Text("Scan with your phone's camera to open PAI.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)

            VStack(spacing: 6) {
                if let url = remote.publicURL {
                    labeledRow("URL", value: url)
                }
                if let code = remote.accessCode {
                    labeledRow("Access code", value: code, mono: true)
                }
            }
            .padding(.top, 2)

            Text("Or open the URL and type the access code to log in. Turning remote access off (or quitting PAI) takes the link down.")
                .font(.caption)
                .foregroundStyle(.tertiary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 300)
        }
    }

    private func labeledRow(_ label: String, value: String, mono: Bool = false) -> some View {
        HStack(spacing: 8) {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
                .frame(width: 84, alignment: .trailing)
            Text(value)
                .font(mono ? .body.monospaced() : .callout)
                .textSelection(.enabled)
                .lineLimit(1)
                .truncationMode(.middle)
            Spacer(minLength: 0)
            Button {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(value, forType: .string)
            } label: {
                Image(systemName: "doc.on.doc")
            }
            .buttonStyle(.borderless)
            .help("Copy")
        }
        .frame(maxWidth: 300)
    }

    /// Build a QR NSImage from a string via CoreImage's CIQRCodeGenerator.
    static func makeQR(_ string: String) -> NSImage? {
        let filter = CIFilter.qrCodeGenerator()
        filter.message = Data(string.utf8)
        filter.correctionLevel = "M"
        guard let output = filter.outputImage else { return nil }
        let scaled = output.transformed(by: CGAffineTransform(scaleX: 12, y: 12))
        let rep = NSCIImageRep(ciImage: scaled)
        let image = NSImage(size: rep.size)
        image.addRepresentation(rep)
        return image
    }
}
