//
//  MarkdownBody.swift
//  cubes
//
//  Agent replies render as Markdown on the canvas. Incomplete fences while streaming stay
//  readable instead of flashing a parse error.
//

import SwiftUI

/// `@名字` runs open a member profile through this scheme, inside ordinary flowing text.
enum MentionLink {
    static let scheme = "cubes-mention"

    static func url(for name: String) -> URL? {
        var components = URLComponents()
        components.scheme = scheme
        components.host = "member"
        components.queryItems = [URLQueryItem(name: "name", value: name)]
        return components.url
    }

    static func name(from url: URL) -> String? {
        guard url.scheme == scheme else { return nil }
        return URLComponents(url: url, resolvingAgainstBaseURL: false)?
            .queryItems?
            .first { $0.name == "name" }?
            .value
    }
}

struct MarkdownBody: View {
    let text: String
    var isStreaming = false
    /// Member names to draw as mentions; the rest of the text stays plain.
    var mentionNames: Set<String> = []

    private let size: CGFloat = 15

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            let blocks = MarkdownDocument.parse(text)
            ForEach(Array(blocks.enumerated()), id: \.offset) { index, block in
                let cursor = isStreaming && index == blocks.count - 1
                switch block {
                case .heading(let level, let value):
                    inline(value + (cursor ? "▌" : ""), size: level == 1 ? 20 : 17, weight: .semibold)
                case .paragraph(let value):
                    inline(value + (cursor ? "▌" : ""), size: size)
                case .bullets(let items):
                    list(items, ordered: false, cursor: cursor)
                case .numbers(let items):
                    list(items, ordered: true, cursor: cursor)
                case .table(let headers, let rows):
                    table(headers, rows)
                case .code(let value):
                    Text(value.isEmpty ? " " : value)
                        .font(.system(size: 13, design: .monospaced))
                        .foregroundStyle(Palette.ink)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(12)
                        .background {
                            RoundedRectangle(cornerRadius: 12, style: .continuous)
                                .fill(Palette.surfaceMuted)
                        }
                case .rule:
                    Rectangle()
                        .fill(Palette.hairline)
                        .frame(height: 1)
                        .padding(.vertical, 4)
                }
            }
        }
    }

    private func list(_ items: [String], ordered: Bool, cursor: Bool) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(ordered ? "\(index + 1)." : "•")
                        .font(.system(size: size, weight: .semibold))
                        .foregroundStyle(Palette.inkTertiary)
                        .frame(width: ordered ? 22 : 12, alignment: .trailing)
                    inline(item + (cursor && index == items.count - 1 ? "▌" : ""), size: size)
                }
            }
        }
    }

    private func table(_ headers: [String], _ rows: [[String]]) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                VStack(alignment: .leading, spacing: 4) {
                    if let title = row.first, !title.isEmpty {
                        inline(title, size: 15, weight: .semibold)
                    }
                    ForEach(Array(headers.enumerated()), id: \.offset) { column, header in
                        if column > 0 {
                            HStack(alignment: .firstTextBaseline, spacing: 8) {
                                Text(header)
                                    .font(.system(size: 13))
                                    .foregroundStyle(Palette.inkTertiary)
                                    .frame(width: 72, alignment: .leading)
                                inline(column < row.count ? row[column] : "", size: 14)
                            }
                        }
                    }
                }
                .padding(.vertical, 8)
                Rectangle().fill(Palette.hairline).frame(height: 1)
            }
        }
        .accessibilityElement(children: .combine)
    }

    private func inline(_ source: String, size: CGFloat, weight: Font.Weight = .regular) -> Text {
        var combined = AttributedString()
        for piece in MarkdownDocument.pieces(source) {
            if piece.code {
                var code = AttributedString(piece.text)
                code.font = .system(size: size - 1, weight: .medium, design: .monospaced)
                code.foregroundColor = Palette.ink
                combined.append(code)
                continue
            }
            let resolved: Font.Weight = piece.weight == .semibold || weight == .semibold ? .semibold : weight
            combined.append(attributed(piece.text, size: size, weight: resolved, italic: piece.italic))
        }
        return Text(combined)
    }

    /// Plain ink, with `@名字` emphasised and tappable.
    private func attributed(_ text: String, size: CGFloat, weight: Font.Weight, italic: Bool) -> AttributedString {
        var string = AttributedString(text)
        string.font = .system(size: size, weight: weight)
        string.foregroundColor = Palette.ink
        if italic { string.inlinePresentationIntent = .emphasized }
        guard !mentionNames.isEmpty else { return string }

        for token in MentionText.tokens(in: text) where mentionNames.contains(token.name) {
            guard let lower = AttributedString.Index(token.range.lowerBound, within: string),
                  let upper = AttributedString.Index(token.range.upperBound, within: string)
            else { continue }
            let range = lower..<upper
            string[range].font = .system(size: size, weight: .semibold)
            string[range].foregroundColor = Palette.accent
            string[range].link = MentionLink.url(for: token.name)
        }
        return string
    }
}
