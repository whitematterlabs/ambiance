import SwiftUI

/// First-run (and on-demand) capability picker. Grouped checklist of registry
/// drivers/skills/subagents; installs the selection via paiman. The GUI twin of
/// paisetup's curses checklist. AppDelegate dismisses the window via `onDone`.
struct SetupCapabilitiesView: View {
    @ObservedObject var catalog: CapabilityCatalog
    /// True on the first-run path (shows "Skip for now"); false when reopened
    /// from the menu (shows "Done"/"Close").
    let firstRun: Bool
    let onDone: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            content
            Divider()
            footer
        }
        .frame(width: 580, height: 560)
        .task { if catalog.groups.isEmpty && !catalog.loading { await catalog.load() } }
        .onChange(of: catalog.done) { _, done in if done { onDone() } }
    }

    private var header: some View {
        HStack(spacing: 12) {
            Image(systemName: "puzzlepiece.extension")
                .font(.system(size: 28, weight: .light))
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(Color.accentColor)
            VStack(alignment: .leading, spacing: 2) {
                Text("Choose capabilities")
                    .font(.title2.weight(.semibold))
                Text("Pick the drivers, skills, and subagents your PAI can use. You can change this any time from the menu.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer()
        }
        .padding(20)
    }

    @ViewBuilder
    private var content: some View {
        if catalog.loading {
            centered { ProgressView("Loading the registry…") }
        } else if let err = catalog.loadError {
            centered {
                VStack(spacing: 12) {
                    Image(systemName: "exclamationmark.triangle")
                        .font(.system(size: 32, weight: .light))
                        .foregroundStyle(.orange)
                    Text(err)
                        .font(.callout)
                        .multilineTextAlignment(.center)
                        .frame(maxWidth: 420)
                    Button("Retry") { Task { await catalog.load() } }
                        .buttonStyle(.borderedProminent)
                }
            }
        } else if catalog.installing {
            installingView
        } else {
            list
        }
    }

    private var list: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                ForEach(catalog.groups) { group in
                    if !group.items.isEmpty {
                        section(group)
                    }
                }
                if catalog.groups.allSatisfy(\.items.isEmpty) {
                    Text("No optional capabilities are available in the registry right now.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .center)
                        .padding(.top, 40)
                }
            }
            .padding(20)
        }
    }

    private func section(_ group: CapabilityCatalog.Group) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(group.title.uppercased())
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            ForEach(group.items) { item in
                row(item)
            }
        }
    }

    private func row(_ item: CapabilityCatalog.Item) -> some View {
        Toggle(isOn: Binding(
            get: { item.installed || catalog.selected.contains(item.name) },
            set: { _ in catalog.toggle(item.name) }
        )) {
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 6) {
                    Text(item.name).font(.body.weight(.medium))
                    if item.installed {
                        Text("installed")
                            .font(.caption2.weight(.semibold))
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 5).padding(.vertical, 1)
                            .background(Color.secondary.opacity(0.15), in: Capsule())
                    }
                }
                if !item.description.isEmpty {
                    Text(item.description)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .toggleStyle(.checkbox)
        .disabled(item.installed)
    }

    private var installingView: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("Installing capabilities…").font(.headline)
            }
            ScrollViewReader { proxy in
                ScrollView {
                    Text(catalog.log.isEmpty ? " " : catalog.log)
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .id("logEnd")
                }
                .background(Color(nsColor: .textBackgroundColor))
                .clipShape(RoundedRectangle(cornerRadius: 6))
                .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color(nsColor: .separatorColor)))
                .onChange(of: catalog.log) { _, _ in proxy.scrollTo("logEnd", anchor: .bottom) }
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var footer: some View {
        HStack {
            if !catalog.loading && catalog.loadError == nil && !catalog.installing {
                Text(selectionSummary)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if !catalog.installing {
                Button(firstRun ? "Skip for now" : "Close") { catalog.skip() }
                Button(installLabel) { Task { await catalog.install() } }
                    .keyboardShortcut(.defaultAction)
                    .buttonStyle(.borderedProminent)
                    .disabled(catalog.loading || catalog.loadError != nil)
            }
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 14)
    }

    private var selectionSummary: String {
        let n = catalog.installCount
        return n == 0 ? "Nothing selected to install" : "\(n) to install"
    }

    private var installLabel: String {
        catalog.installCount == 0 ? (firstRun ? "Continue" : "Done") : "Install \(catalog.installCount)"
    }

    private func centered<V: View>(@ViewBuilder _ content: () -> V) -> some View {
        content()
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .padding(20)
    }
}
