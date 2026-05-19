import SwiftUI

/// Lightweight block-level markdown renderer. Splits a message body into
/// blocks (header / bullet / fenced code / paragraph) and renders each
/// with the right font + spacing. Inline syntax (bold/italic/code/link)
/// inside non-code blocks is handed off to AttributedString(markdown:).
///
/// Not a full CommonMark parser — covers what chat messages actually use:
///   # / ## / ### headers
///   - or * bullet lists (single-level)
///   1. 2. ordered lists (single-level)
///   ``` fenced code blocks
///   > blockquotes
///   --- horizontal rules
///   regular paragraphs with inline markdown
///
/// For full CommonMark fidelity, swap in MarkdownUI — but this keeps the
/// build dep-free.
struct MarkdownBlocks: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                view(for: block)
            }
        }
    }

    private var blocks: [Block] { Self.parse(text) }

    @ViewBuilder
    private func view(for block: Block) -> some View {
        switch block {
        case .header(let level, let body):
            Text(inline(body))
                .font(headerFont(level))
                .padding(.top, level <= 2 ? 4 : 2)
        case .paragraph(let body):
            Text(inline(body))
                .fixedSize(horizontal: false, vertical: true)
        case .bullets(let items):
            VStack(alignment: .leading, spacing: 2) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text("•").foregroundStyle(.secondary)
                        Text(inline(item))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        case .ordered(let items):
            VStack(alignment: .leading, spacing: 2) {
                ForEach(Array(items.enumerated()), id: \.offset) { idx, item in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text("\(idx + 1).").foregroundStyle(.secondary)
                            .monospacedDigit()
                        Text(inline(item))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        case .code(let body):
            Text(body)
                .font(.system(.body, design: .monospaced))
                .padding(8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(
                    RoundedRectangle(cornerRadius: 6)
                        .fill(Color.black.opacity(0.08))
                )
        case .quote(let body):
            HStack(spacing: 8) {
                RoundedRectangle(cornerRadius: 2)
                    .fill(Color.secondary.opacity(0.5))
                    .frame(width: 3)
                Text(inline(body))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        case .rule:
            Divider()
        }
    }

    private func headerFont(_ level: Int) -> Font {
        switch level {
        case 1: return .title.weight(.semibold)
        case 2: return .title2.weight(.semibold)
        case 3: return .title3.weight(.semibold)
        case 4: return .headline
        default: return .subheadline.weight(.semibold)
        }
    }

    private func inline(_ src: String) -> AttributedString {
        let opts = AttributedString.MarkdownParsingOptions(
            allowsExtendedAttributes: false,
            interpretedSyntax: .inlineOnlyPreservingWhitespace,
            failurePolicy: .returnPartiallyParsedIfPossible
        )
        if let a = try? AttributedString(markdown: src, options: opts) {
            return a
        }
        return AttributedString(src)
    }

    // MARK: - parser

    enum Block: Hashable {
        case header(Int, String)
        case paragraph(String)
        case bullets([String])
        case ordered([String])
        case code(String)
        case quote(String)
        case rule
    }

    static func parse(_ src: String) -> [Block] {
        var out: [Block] = []
        let lines = src.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        var i = 0
        while i < lines.count {
            let line = lines[i]
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            // fenced code block
            if trimmed.hasPrefix("```") {
                var body: [String] = []
                i += 1
                while i < lines.count {
                    if lines[i].trimmingCharacters(in: .whitespaces).hasPrefix("```") {
                        i += 1
                        break
                    }
                    body.append(lines[i])
                    i += 1
                }
                out.append(.code(body.joined(separator: "\n")))
                continue
            }

            // horizontal rule
            if trimmed == "---" || trimmed == "***" {
                out.append(.rule); i += 1; continue
            }

            // header
            if let (level, body) = matchHeader(trimmed) {
                out.append(.header(level, body)); i += 1; continue
            }

            // bullet list
            if isBullet(trimmed) {
                var items: [String] = []
                while i < lines.count {
                    let t = lines[i].trimmingCharacters(in: .whitespaces)
                    guard isBullet(t) else { break }
                    items.append(stripBullet(t))
                    i += 1
                }
                out.append(.bullets(items))
                continue
            }

            // ordered list
            if isOrdered(trimmed) {
                var items: [String] = []
                while i < lines.count {
                    let t = lines[i].trimmingCharacters(in: .whitespaces)
                    guard isOrdered(t) else { break }
                    items.append(stripOrdered(t))
                    i += 1
                }
                out.append(.ordered(items))
                continue
            }

            // blockquote
            if trimmed.hasPrefix("> ") || trimmed == ">" {
                var body: [String] = []
                while i < lines.count {
                    let t = lines[i].trimmingCharacters(in: .whitespaces)
                    if !(t.hasPrefix("> ") || t == ">") { break }
                    body.append(String(t.dropFirst(t == ">" ? 1 : 2)))
                    i += 1
                }
                out.append(.quote(body.joined(separator: "\n")))
                continue
            }

            // blank line — paragraph separator
            if trimmed.isEmpty { i += 1; continue }

            // paragraph: collect consecutive non-blank, non-block lines
            var para: [String] = [line]
            i += 1
            while i < lines.count {
                let t = lines[i].trimmingCharacters(in: .whitespaces)
                if t.isEmpty
                    || matchHeader(t) != nil
                    || isBullet(t) || isOrdered(t)
                    || t.hasPrefix("```")
                    || t.hasPrefix("> ") || t == ">"
                    || t == "---" || t == "***" {
                    break
                }
                para.append(lines[i])
                i += 1
            }
            out.append(.paragraph(para.joined(separator: "\n")))
        }
        return out
    }

    private static func matchHeader(_ line: String) -> (Int, String)? {
        guard line.first == "#" else { return nil }
        var level = 0
        for c in line {
            if c == "#" { level += 1; if level > 6 { return nil } } else { break }
        }
        guard level >= 1, level <= 6 else { return nil }
        let rest = line.dropFirst(level)
        guard rest.first == " " else { return nil }
        return (level, String(rest.dropFirst()).trimmingCharacters(in: .whitespaces))
    }

    private static func isBullet(_ line: String) -> Bool {
        line.hasPrefix("- ") || line.hasPrefix("* ") || line.hasPrefix("+ ")
    }

    private static func stripBullet(_ line: String) -> String {
        String(line.dropFirst(2))
    }

    private static func isOrdered(_ line: String) -> Bool {
        // Match: digits, then ". " or ") "
        var sawDigit = false
        for (idx, c) in line.enumerated() {
            if c.isNumber { sawDigit = true; continue }
            if !sawDigit { return false }
            // we're past the digits
            if c == "." || c == ")" {
                let next = line.index(line.startIndex, offsetBy: idx + 1, limitedBy: line.endIndex)
                if let n = next, n < line.endIndex, line[n] == " " { return true }
            }
            return false
        }
        return false
    }

    private static func stripOrdered(_ line: String) -> String {
        // Drop leading digits + ". " or ") "
        var idx = line.startIndex
        while idx < line.endIndex, line[idx].isNumber { idx = line.index(after: idx) }
        if idx < line.endIndex, line[idx] == "." || line[idx] == ")" {
            idx = line.index(after: idx)
            if idx < line.endIndex, line[idx] == " " { idx = line.index(after: idx) }
        }
        return String(line[idx...])
    }
}
