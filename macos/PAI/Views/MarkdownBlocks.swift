import SwiftUI

/// Lightweight block-level markdown renderer. Splits a message body into
/// blocks and renders each with the right font + spacing. Inline syntax
/// (bold/italic/code/link/strikethrough) is handed off to
/// AttributedString(markdown:).
///
/// Covers what LLM chat output actually uses:
///   # / ## / ### headers
///   - / * / + bullet lists (nested via indentation)
///   1. / 1) ordered lists (nested via indentation)
///   - [ ] / - [x] task lists
///   ```lang fenced code blocks (lang shown as a small tag)
///   GFM tables with optional alignment (:--, --:, :--:)
///   > blockquotes
///   --- / *** horizontal rules
///   regular paragraphs with inline markdown
struct MarkdownBlocks: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                BlockView(block: block, inline: inline)
            }
        }
    }

    private var blocks: [Block] { Self.parse(text) }

    fileprivate func inline(_ src: String) -> AttributedString {
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

    // MARK: - model

    indirect enum Block: Hashable {
        case header(Int, String)
        case paragraph(String)
        case list(ordered: Bool, items: [ListItem])
        case code(lang: String, body: String)
        case quote(String)
        case rule
        case table(headers: [String], alignments: [TableAlign], rows: [[String]])
    }

    struct ListItem: Hashable {
        let body: String
        let checked: Bool?   // nil = plain item; false = unchecked task; true = checked task
        let children: [Block]
    }

    enum TableAlign: Hashable { case leading, center, trailing }

    // MARK: - parser

    static func parse(_ src: String) -> [Block] {
        let lines = src.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        var i = 0
        return parseBlocks(lines: lines, i: &i, minIndent: 0)
    }

    private static func parseBlocks(lines: [String], i: inout Int, minIndent: Int) -> [Block] {
        var out: [Block] = []
        while i < lines.count {
            let line = lines[i]
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            if trimmed.isEmpty { i += 1; continue }

            let indent = leadingSpaces(line)
            if indent < minIndent { break }

            // fenced code
            if trimmed.hasPrefix("```") {
                let lang = String(trimmed.dropFirst(3)).trimmingCharacters(in: .whitespaces)
                var body: [String] = []
                i += 1
                while i < lines.count {
                    if lines[i].trimmingCharacters(in: .whitespaces).hasPrefix("```") {
                        i += 1; break
                    }
                    body.append(lines[i])
                    i += 1
                }
                out.append(.code(lang: lang, body: body.joined(separator: "\n")))
                continue
            }

            if trimmed == "---" || trimmed == "***" { out.append(.rule); i += 1; continue }

            if let (level, body) = matchHeader(trimmed) {
                out.append(.header(level, body)); i += 1; continue
            }

            // table: header row + separator row
            if let (block, consumed) = matchTable(lines: lines, from: i) {
                out.append(block); i += consumed; continue
            }

            if isBullet(trimmed) || isOrdered(trimmed) {
                out.append(parseList(lines: lines, i: &i, listIndent: indent))
                continue
            }

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

            // paragraph: collect consecutive non-blank, non-block lines
            var para: [String] = [trimmed]
            i += 1
            while i < lines.count {
                let t = lines[i].trimmingCharacters(in: .whitespaces)
                if t.isEmpty { break }
                if matchHeader(t) != nil
                    || isBullet(t) || isOrdered(t)
                    || t.hasPrefix("```")
                    || t.hasPrefix("> ") || t == ">"
                    || t == "---" || t == "***" { break }
                para.append(t)
                i += 1
            }
            out.append(.paragraph(para.joined(separator: "\n")))
        }
        return out
    }

    private static func parseList(lines: [String], i: inout Int, listIndent: Int) -> Block {
        let firstTrim = lines[i].trimmingCharacters(in: .whitespaces)
        let ordered = isOrdered(firstTrim)
        var items: [ListItem] = []

        while i < lines.count {
            let line = lines[i]
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.isEmpty { break }
            let indent = leadingSpaces(line)
            if indent != listIndent { break }
            let isOrd = isOrdered(trimmed)
            let isBul = isBullet(trimmed)
            if ordered && !isOrd { break }
            if !ordered && !isBul { break }

            let stripped = ordered ? stripOrdered(trimmed) : stripBullet(trimmed)
            let (checked, body) = parseTask(stripped)
            i += 1

            var children: [Block] = []
            if i < lines.count {
                let nextTrim = lines[i].trimmingCharacters(in: .whitespaces)
                let nextIndent = leadingSpaces(lines[i])
                if !nextTrim.isEmpty && nextIndent > listIndent {
                    children = parseBlocks(lines: lines, i: &i, minIndent: nextIndent)
                }
            }

            items.append(ListItem(body: body, checked: checked, children: children))
        }

        return .list(ordered: ordered, items: items)
    }

    private static func matchTable(lines: [String], from i: Int) -> (Block, Int)? {
        guard i + 1 < lines.count else { return nil }
        let header = lines[i].trimmingCharacters(in: .whitespaces)
        let sep = lines[i + 1].trimmingCharacters(in: .whitespaces)
        guard header.contains("|"), isTableSeparator(sep) else { return nil }

        let headers = splitCells(header)
        let alignCells = splitCells(sep)
        let alignments: [TableAlign] = alignCells.map { cell in
            let c = cell.trimmingCharacters(in: .whitespaces)
            let left = c.hasPrefix(":")
            let right = c.hasSuffix(":")
            if left && right { return .center }
            if right { return .trailing }
            return .leading
        }

        var rows: [[String]] = []
        var j = i + 2
        while j < lines.count {
            let t = lines[j].trimmingCharacters(in: .whitespaces)
            if t.isEmpty || !t.contains("|") { break }
            // pad/truncate to header width
            var cells = splitCells(t)
            while cells.count < headers.count { cells.append("") }
            if cells.count > headers.count { cells = Array(cells.prefix(headers.count)) }
            rows.append(cells)
            j += 1
        }

        return (.table(headers: headers, alignments: alignments, rows: rows), j - i)
    }

    private static func isTableSeparator(_ s: String) -> Bool {
        guard s.contains("|") else { return false }
        let cells = splitCells(s)
        guard !cells.isEmpty else { return false }
        for raw in cells {
            let t = raw.trimmingCharacters(in: .whitespaces)
            if t.isEmpty { return false }
            var idx = t.startIndex
            if t[idx] == ":" { idx = t.index(after: idx) }
            var sawDash = false
            while idx < t.endIndex, t[idx] == "-" { sawDash = true; idx = t.index(after: idx) }
            if idx < t.endIndex, t[idx] == ":" { idx = t.index(after: idx) }
            if !sawDash || idx != t.endIndex { return false }
        }
        return true
    }

    private static func splitCells(_ s: String) -> [String] {
        var s = s
        if s.hasPrefix("|") { s.removeFirst() }
        if s.hasSuffix("|") { s.removeLast() }
        return s.split(separator: "|", omittingEmptySubsequences: false).map {
            $0.trimmingCharacters(in: .whitespaces)
        }
    }

    private static func parseTask(_ s: String) -> (Bool?, String) {
        if s.hasPrefix("[ ] ") { return (false, String(s.dropFirst(4))) }
        if s.hasPrefix("[x] ") || s.hasPrefix("[X] ") { return (true, String(s.dropFirst(4))) }
        if s == "[ ]" { return (false, "") }
        if s == "[x]" || s == "[X]" { return (true, "") }
        return (nil, s)
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
        var sawDigit = false
        for (idx, c) in line.enumerated() {
            if c.isNumber { sawDigit = true; continue }
            if !sawDigit { return false }
            if c == "." || c == ")" {
                let next = line.index(line.startIndex, offsetBy: idx + 1, limitedBy: line.endIndex)
                if let n = next, n < line.endIndex, line[n] == " " { return true }
            }
            return false
        }
        return false
    }

    private static func stripOrdered(_ line: String) -> String {
        var idx = line.startIndex
        while idx < line.endIndex, line[idx].isNumber { idx = line.index(after: idx) }
        if idx < line.endIndex, line[idx] == "." || line[idx] == ")" {
            idx = line.index(after: idx)
            if idx < line.endIndex, line[idx] == " " { idx = line.index(after: idx) }
        }
        return String(line[idx...])
    }

    private static func leadingSpaces(_ s: String) -> Int {
        var n = 0
        for c in s {
            if c == " " { n += 1 }
            else if c == "\t" { n += 4 }
            else { break }
        }
        return n
    }
}

// MARK: - block view dispatch

private struct BlockView: View {
    let block: MarkdownBlocks.Block
    let inline: (String) -> AttributedString

    @ViewBuilder
    var body: some View {
        switch block {
        case .header(let level, let body):
            Text(inline(body))
                .font(headerFont(level))
                .padding(.top, level <= 2 ? 4 : 2)
        case .paragraph(let body):
            Text(inline(body))
                .fixedSize(horizontal: false, vertical: true)
        case .list(let ordered, let items):
            ListBlockView(ordered: ordered, items: items, inline: inline)
        case .code(let lang, let body):
            CodeBlockView(lang: lang, code: body)
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
        case .table(let headers, let alignments, let rows):
            TableBlockView(headers: headers, alignments: alignments, rows: rows, inline: inline)
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
}

private struct ListBlockView: View {
    let ordered: Bool
    let items: [MarkdownBlocks.ListItem]
    let inline: (String) -> AttributedString

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            ForEach(Array(items.enumerated()), id: \.offset) { idx, item in
                VStack(alignment: .leading, spacing: 2) {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        marker(idx: idx, item: item)
                        Text(inline(item.body))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if !item.children.isEmpty {
                        VStack(alignment: .leading, spacing: 2) {
                            ForEach(Array(item.children.enumerated()), id: \.offset) { _, child in
                                BlockView(block: child, inline: inline)
                            }
                        }
                        .padding(.leading, 16)
                    }
                }
            }
        }
    }

    @ViewBuilder
    private func marker(idx: Int, item: MarkdownBlocks.ListItem) -> some View {
        if let checked = item.checked {
            Image(systemName: checked ? "checkmark.square.fill" : "square")
                .foregroundStyle(checked ? Color.accentColor : Color.secondary)
        } else if ordered {
            Text("\(idx + 1).")
                .foregroundStyle(.secondary)
                .monospacedDigit()
        } else {
            Text("•").foregroundStyle(.secondary)
        }
    }
}

private struct CodeBlockView: View {
    let lang: String
    let code: String

    var body: some View {
        ZStack(alignment: .topTrailing) {
            Text(code)
                .font(.system(.body, design: .monospaced))
                .textSelection(.enabled)
                .padding(8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(
                    RoundedRectangle(cornerRadius: 6)
                        .fill(Color.black.opacity(0.08))
                )
            if !lang.isEmpty {
                Text(lang)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(
                        RoundedRectangle(cornerRadius: 4)
                            .fill(Color.black.opacity(0.06))
                    )
                    .padding(6)
            }
        }
    }
}

private struct TableBlockView: View {
    let headers: [String]
    let alignments: [MarkdownBlocks.TableAlign]
    let rows: [[String]]
    let inline: (String) -> AttributedString

    var body: some View {
        Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 4) {
            GridRow {
                ForEach(Array(headers.enumerated()), id: \.offset) { idx, h in
                    Text(inline(h)).bold()
                        .frame(maxWidth: .infinity, alignment: align(idx))
                }
            }
            Divider().gridCellColumns(max(headers.count, 1))
            ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                GridRow {
                    ForEach(Array(row.enumerated()), id: \.offset) { idx, cell in
                        Text(inline(cell))
                            .frame(maxWidth: .infinity, alignment: align(idx))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
        .padding(8)
        .overlay(
            RoundedRectangle(cornerRadius: 6)
                .stroke(Color.secondary.opacity(0.3))
        )
    }

    private func align(_ idx: Int) -> Alignment {
        guard idx < alignments.count else { return .leading }
        switch alignments[idx] {
        case .leading: return .leading
        case .center: return .center
        case .trailing: return .trailing
        }
    }
}
