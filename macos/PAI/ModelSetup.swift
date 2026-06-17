import Foundation
import Combine

/// First-run model/provider + API-key step. The GUI twin of install.sh's
/// "Which model should PAI use by default?" prompt and its `ensure_api_key`
/// routine: pick a provider, and store only that provider's key.
///
/// PAI can't run without a key — `boot/__init__.py` loads `$PAI_ROOT/.env`
/// (then `.env.local`) at startup and the LLM client reads the provider's key
/// var from the environment. A Finder-launched .app inherits no shell env and
/// has no `.env` of its own, so without this screen the kernel boots and then
/// fails on the first turn. We write the chosen key to `$PAI_ROOT/.env` — the
/// precedence-1 location boot reads, and the only one a .app sees.
@MainActor
final class ModelSetup: ObservableObject {
    /// A selectable provider. `model` and `envVar` mirror install.sh's menu
    /// (`anthropic`→`claude-opus-4-7`, `deepseek`→`deepseek-v4-pro`,
    /// `openai`→`gpt-5.5`) and `llm.PROVIDERS`' key-var mapping.
    struct Provider: Identifiable, Equatable {
        let id: String        // provider key written to config.yaml
        let label: String     // shown in the picker
        let blurb: String     // one-line description under the label
        let model: String     // model id seeded into config.yaml
        let envVar: String    // API-key variable written to .env
    }

    /// Order matters: the first whose key is already reachable becomes the
    /// silent default (Anthropic preferred, matching install.sh's "default 1").
    static let providers: [Provider] = [
        Provider(id: "anthropic", label: "Claude Opus", blurb: "Anthropic — the default.",
                 model: "claude-opus-4-7", envVar: "ANTHROPIC_API_KEY"),
        Provider(id: "deepseek", label: "DeepSeek", blurb: "Open-weight, lower cost.",
                 model: "deepseek-v4-pro", envVar: "DEEPSEEK_API_KEY"),
        Provider(id: "openai", label: "GPT-5.5", blurb: "OpenAI — via the LiteLLM proxy.",
                 model: "gpt-5.5", envVar: "OPENAI_API_KEY"),
    ]

    /// The provider the user has selected in the picker (defaults to the first).
    @Published var selected: Provider = ModelSetup.providers[0]
    /// The key typed into the secure field. Never persisted except via `.env`.
    @Published var apiKey: String = ""

    // MARK: First-run gating

    /// Same probe as Provisioner/KernelLauncher: only shipped builds (bundled
    /// runtime) have a first run to gate. Dev checkouts set up `~/.pai` via
    /// `paifs-init`, which already handles the key prompt.
    private var isShippedBuild: Bool {
        guard let res = Bundle.main.resourceURL else { return false }
        let py = res.appendingPathComponent("runtime/python/bin/python3")
        return FileManager.default.isExecutableFile(atPath: py.path)
    }

    /// The first provider whose key is already reachable (shell env or a
    /// `$PAI_ROOT/.env[.local]` boot already loads), or nil if none is. When
    /// non-nil we skip the screen and seed the fleet on that provider so it
    /// boots on a model that actually has a key.
    var reachableProvider: Provider? {
        Self.providers.first { keyIsReachable($0.envVar) }
    }

    /// True only on a shipped first run with no key reachable for any provider.
    /// Otherwise the screen is skipped: an existing key drives the default
    /// provider, and re-provisions (schema bumps) never re-ask.
    var needsSetup: Bool {
        isShippedBuild && reachableProvider == nil
    }

    /// Provider/model to seed when the screen is skipped because a key is
    /// already present — boot on the provider whose key we found.
    var skipDefault: Provider { reachableProvider ?? Self.providers[0] }

    private func keyIsReachable(_ varName: String) -> Bool {
        if let v = ProcessInfo.processInfo.environment[varName], !v.isEmpty { return true }
        for name in [".env.local", ".env"] {
            let url = FHS.root.appendingPathComponent(name)
            if let text = try? String(contentsOf: url, encoding: .utf8),
               envFileDefines(varName, in: text) {
                return true
            }
        }
        return false
    }

    /// Matches a `VAR=...` assignment (ignoring blank lines and `#` comments),
    /// mirroring install.sh's `grep -qE "^${var}="`.
    private func envFileDefines(_ varName: String, in text: String) -> Bool {
        text.split(whereSeparator: \.isNewline).contains { line in
            let t = line.drop { $0 == " " || $0 == "\t" }
            return t.hasPrefix("\(varName)=")
        }
    }

    // MARK: Persistence

    /// Append `VAR=key` to `$PAI_ROOT/.env` (created if absent, chmod 0600),
    /// mirroring install.sh's `ensure_api_key`. No-op on a blank key (the user
    /// chose "Skip"), so the kernel can still pick a key up from the shell env
    /// or the owner can add `.env` later. Returns false if the write failed.
    @discardableResult
    func commitKey() -> Bool {
        let key = apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !key.isEmpty else { return true }
        let root = FHS.root
        let envURL = root.appendingPathComponent(".env")
        let fm = FileManager.default
        do {
            try fm.createDirectory(at: root, withIntermediateDirectories: true)
            var contents = (try? String(contentsOf: envURL, encoding: .utf8)) ?? ""
            if !contents.isEmpty && !contents.hasSuffix("\n") { contents += "\n" }
            contents += "\(selected.envVar)=\(key)\n"
            try contents.write(to: envURL, atomically: true, encoding: .utf8)
            try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: envURL.path)
            // Don't keep the secret in memory once it's on disk.
            apiKey = ""
            return true
        } catch {
            return false
        }
    }
}
