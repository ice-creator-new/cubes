//
//  MentionText.swift
//  cubes
//
//  `@名字` on the wire is just text, so the client recognises it itself: highlighting, the
//  picker query and the ids sent with a run all come from these few pure functions.
//

import Foundation

enum MentionText {

    struct Token: Equatable {
        /// The member name after `@`, without the marker.
        let name: String
        /// The whole token, `@` included.
        let range: Range<String.Index>
    }

    /// Text typed after an `@` that is still open at the end of the draft.
    struct Query: Equatable {
        let text: String
        /// The `@` and everything after it.
        let range: Range<String.Index>
    }

    /// Characters that end a name. `@` itself ends one, so `@A@B` is two tokens.
    /// Whitespace is covered separately by `isTerminator`.
    private static let terminators: Set<Character> = Set("@，。！？、；：,.!?;:()（）[]【】<>《》\"'“”‘’·`*|~\\/")

    private static func isTerminator(_ character: Character) -> Bool {
        terminators.contains(character) || character.isWhitespace
    }

    /// Every `@名字` run in the text, in order.
    static func tokens(in text: String) -> [Token] {
        var result: [Token] = []
        var index = text.startIndex
        // A token that just ended may be followed directly by another `@名字`.
        var openedByPreviousToken = false
        while index < text.endIndex {
            guard text[index] == "@" else {
                openedByPreviousToken = false
                index = text.index(after: index)
                continue
            }
            // `a@b.com` is not a mention: the marker must open a word.
            if !openedByPreviousToken, index > text.startIndex, !isTerminator(text[text.index(before: index)]) {
                index = text.index(after: index)
                continue
            }
            var end = text.index(after: index)
            var name = ""
            while end < text.endIndex, !isTerminator(text[end]) {
                name.append(text[end])
                end = text.index(after: end)
            }
            openedByPreviousToken = !name.isEmpty
            if openedByPreviousToken {
                result.append(Token(name: name, range: index..<end))
            }
            index = end
        }
        return result
    }

    /// The unfinished `@…` at the end of the draft, if the picker should be showing.
    static func query(in text: String) -> Query? {
        guard let marker = text.lastIndex(of: "@") else { return nil }
        let open = marker == text.startIndex
            || isTerminator(text[text.index(before: marker)])
        guard open else { return nil }
        let suffix = text[text.index(after: marker)...]
        guard suffix.allSatisfy({ !isTerminator($0) }) else { return nil }
        return Query(text: String(suffix), range: marker..<text.endIndex)
    }

    /// Replaces the open query with `@名字 `, or appends one at the end.
    static func inserting(_ name: String, into draft: String, replacing query: Query?) -> String {
        let replacement = "@\(name) "
        if let query, query.range.upperBound == draft.endIndex {
            var text = draft
            text.replaceSubrange(query.range, with: replacement)
            return text
        }
        if draft.isEmpty { return replacement }
        if let last = draft.last, last.isWhitespace || last.isNewline { return draft + replacement }
        return draft + " " + replacement
    }

    /// Members the draft actually names. `candidates` is `(id, name)`.
    static func ids(in draft: String, candidates: [(id: String, name: String)]) -> [String] {
        let names = Set(tokens(in: draft).map(\.name))
        return candidates.filter { names.contains($0.name) }.map(\.id)
    }
}
