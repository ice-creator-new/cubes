//
//  MarkdownParse.swift
//  cubes
//
//  The subset agent replies actually use: headings, lists, tables, fences and inline emphasis.
//  SwiftUI Text does not draw tables, and a uniform font wipes bold, so this stays structural.
//

import Foundation

enum MarkdownWeight: Equatable {
    case regular
    case semibold
}

struct MarkdownPiece: Equatable {
    var text: String
    var weight: MarkdownWeight = .regular
    var italic = false
    var code = false
}

enum MarkdownBlock: Equatable {
    case heading(Int, String)
    case paragraph(String)
    case bullets([String])
    case numbers([String])
    case table(headers: [String], rows: [[String]])
    case code(String)
    case rule
}

enum MarkdownDocument {
    static func parse(_ text: String) -> [MarkdownBlock] {
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        var blocks: [MarkdownBlock] = []
        var index = 0
        while index < lines.count {
            let line = lines[index]
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("```") {
                var code = ""
                index += 1
                while index < lines.count, !lines[index].trimmingCharacters(in: .whitespaces).hasPrefix("```") {
                    if !code.isEmpty { code.append("\n") }
                    code.append(lines[index])
                    index += 1
                }
                if index < lines.count { index += 1 }
                blocks.append(.code(code))
                continue
            }
            if trimmed.isEmpty {
                index += 1
                continue
            }
            if isRule(trimmed) {
                blocks.append(.rule)
                index += 1
                continue
            }
            if let heading = heading(trimmed) {
                blocks.append(.heading(heading.0, heading.1))
                index += 1
                continue
            }
            if isTableRow(trimmed), index + 1 < lines.count, isTableSeparator(lines[index + 1]) {
                let headers = cells(trimmed)
                index += 2
                var rows: [[String]] = []
                while index < lines.count, isTableRow(lines[index].trimmingCharacters(in: .whitespaces)) {
                    rows.append(cells(lines[index]))
                    index += 1
                }
                blocks.append(.table(headers: headers, rows: rows))
                continue
            }
            if let marker = listMarker(trimmed) {
                var items = [marker.text]
                let ordered = marker.ordered
                index += 1
                while index < lines.count {
                    let next = lines[index].trimmingCharacters(in: .whitespaces)
                    if next.isEmpty { break }
                    guard let item = listMarker(next), item.ordered == ordered else { break }
                    items.append(item.text)
                    index += 1
                }
                blocks.append(ordered ? .numbers(items) : .bullets(items))
                continue
            }
            var paragraph = line
            index += 1
            while index < lines.count {
                let next = lines[index]
                let nextTrimmed = next.trimmingCharacters(in: .whitespaces)
                if nextTrimmed.isEmpty || nextTrimmed.hasPrefix("```") || heading(nextTrimmed) != nil || isRule(nextTrimmed) {
                    break
                }
                if isTableRow(nextTrimmed), index + 1 < lines.count, isTableSeparator(lines[index + 1]) {
                    break
                }
                if listMarker(nextTrimmed) != nil { break }
                paragraph.append("\n")
                paragraph.append(next)
                index += 1
            }
            blocks.append(.paragraph(paragraph))
        }
        return blocks.isEmpty ? [.paragraph(text)] : blocks
    }

    static func pieces(_ source: String) -> [MarkdownPiece] {
        var result: [MarkdownPiece] = []
        var buffer = ""
        var index = source.startIndex
        func flush() {
            if !buffer.isEmpty {
                result.append(MarkdownPiece(text: buffer))
                buffer = ""
            }
        }
        while index < source.endIndex {
            if source[index...].hasPrefix("**"),
               let end = closing(source, from: source.index(index, offsetBy: 2), marker: "**") {
                flush()
                result.append(MarkdownPiece(text: inner(source, from: source.index(index, offsetBy: 2), to: end), weight: .semibold))
                index = source.index(end, offsetBy: 2)
                continue
            }
            if source[index] == "`" {
                let rest = source.index(after: index)
                if rest < source.endIndex, let end = source[rest...].firstIndex(of: "`") {
                    flush()
                    result.append(MarkdownPiece(text: String(source[rest..<end]), code: true))
                    index = source.index(after: end)
                    continue
                }
            }
            if source[index] == "[", let link = link(source, from: index) {
                flush()
                result.append(MarkdownPiece(text: link.label, weight: .semibold))
                index = link.end
                continue
            }
            if source[index] == "*" || source[index] == "_" {
                let marker = source[index]
                let rest = source.index(after: index)
                if rest < source.endIndex, let end = source[rest...].firstIndex(of: marker), end != rest {
                    flush()
                    result.append(MarkdownPiece(text: String(source[rest..<end]), italic: true))
                    index = source.index(after: end)
                    continue
                }
            }
            buffer.append(source[index])
            index = source.index(after: index)
        }
        flush()
        return result.isEmpty ? [MarkdownPiece(text: source)] : result
    }

    private static func heading(_ line: String) -> (Int, String)? {
        guard line.hasPrefix("#") else { return nil }
        let marks = line.prefix { $0 == "#" }.count
        guard (1...6).contains(marks) else { return nil }
        let rest = line.dropFirst(marks)
        guard rest.first == " " else { return nil }
        return (marks, String(rest.dropFirst()).trimmingCharacters(in: .whitespaces))
    }

    private static func isRule(_ line: String) -> Bool {
        let marks = CharacterSet(charactersIn: "-*_")
        return line.count >= 3 && line.unicodeScalars.allSatisfy { marks.contains($0) }
    }

    private static func isTableRow(_ line: String) -> Bool {
        line.contains("|") && !isTableSeparator(line)
    }

    private static func isTableSeparator(_ line: String) -> Bool {
        let parts = cells(line)
        guard !parts.isEmpty else { return false }
        return parts.allSatisfy { cell in
            let trimmed = cell.trimmingCharacters(in: .whitespaces)
            return !trimmed.isEmpty && trimmed.allSatisfy { $0 == "-" || $0 == ":" }
        }
    }

    private static func cells(_ line: String) -> [String] {
        var text = line.trimmingCharacters(in: .whitespaces)
        if text.hasPrefix("|") { text.removeFirst() }
        if text.hasSuffix("|") { text.removeLast() }
        return text.split(separator: "|", omittingEmptySubsequences: false).map {
            $0.trimmingCharacters(in: .whitespaces)
        }
    }

    private static func listMarker(_ line: String) -> (ordered: Bool, text: String)? {
        if line.hasPrefix("- ") || line.hasPrefix("* ") || line.hasPrefix("+ ") {
            return (false, String(line.dropFirst(2)))
        }
        guard let dot = line.firstIndex(where: { $0 == "." || $0 == ")" }) else { return nil }
        let prefix = line[..<dot]
        guard !prefix.isEmpty, prefix.allSatisfy(\.isNumber) else { return nil }
        let after = line.index(after: dot)
        guard after < line.endIndex, line[after] == " " else { return nil }
        return (true, String(line[line.index(after: after)...]))
    }

    private static func closing(_ source: String, from start: String.Index, marker: String) -> String.Index? {
        var index = start
        while index < source.endIndex {
            if source[index...].hasPrefix(marker) { return index }
            index = source.index(after: index)
        }
        return nil
    }

    private static func inner(_ source: String, from start: String.Index, to end: String.Index) -> String {
        String(source[start..<end])
    }

    private static func link(_ source: String, from start: String.Index) -> (label: String, end: String.Index)? {
        guard let close = source[start...].firstIndex(of: "]"),
              source.index(after: close) < source.endIndex,
              source[source.index(after: close)] == "("
        else { return nil }
        let urlStart = source.index(close, offsetBy: 2)
        guard let urlEnd = source[urlStart...].firstIndex(of: ")") else { return nil }
        let label = String(source[source.index(after: start)..<close])
        guard !label.isEmpty else { return nil }
        return (label, source.index(after: urlEnd))
    }
}
