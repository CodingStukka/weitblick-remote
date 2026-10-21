import Foundation

/// A place where a query was found in a recognized line.
public struct TextMatch: Equatable, Sendable {
    public let lineIndex: Int
    /// The matched characters in the line's text.
    public let range: Range<String.Index>
    /// Edit distance to the query after folding (0 = same text ignoring case and accents).
    public let errors: Int
}

/// Finds a query in OCR lines the way OCR errs: case-, accent- and width-insensitive, blind to
/// whitespace (recognition drops and adds spaces), characters UI fonts make look alike count as
/// the same (l I | 1, O 0, ß B, bracket and quote shapes), and a few other errors are tolerated
/// (one per five characters). Exact matches win; approximate ones are only returned when there is
/// no exact one.
public enum TextMatching {
    public static func find(_ query: String, in lines: [String], limit: Int = 20) -> [TextMatch] {
        let needle = query.filter { !$0.isWhitespace }.map(token)
        guard !needle.isEmpty else { return [] }
        let allowedErrors = needle.count / 5
        var matches: [TextMatch] = []
        for (index, line) in lines.enumerated() {
            let positions = line.indices.filter { !line[$0].isWhitespace }
            let haystack = positions.map { token(line[$0]) }
            for found in search(needle, in: haystack, maxErrors: allowedErrors) {
                let range = positions[found.start]..<line.index(after: positions[found.end - 1])
                matches.append(TextMatch(lineIndex: index, range: range, errors: found.errors))
            }
        }
        if matches.contains(where: { $0.errors == 0 }) { matches.removeAll { $0.errors > 0 } }
        matches.sort { ($0.errors, $0.lineIndex) < ($1.errors, $1.lineIndex) }
        return Array(matches.prefix(limit))
    }

    /// One comparable unit per character.
    static func token(_ character: Character) -> String {
        let folded = String(character).folding(options: [.caseInsensitive, .diacriticInsensitive, .widthInsensitive],
                                               locale: nil)
        return lookAlikes[folded] ?? folded
    }

    /// Characters Vision confuses in UI fonts (after case folding, so "I" arrives as "i").
    /// Measured on Windows 11 Notepad: "ß € @ {} \\ |" read as "B € @ {\\Il".
    private static let lookAlikes: [String: String] = [
        "|": "l", "1": "l", "i": "l", "!": "l", "ı": "l",
        "0": "o",
        "ss": "b", // "ß" folds to "ss"
        "{": "(", "[": "(", "}": ")", "]": ")",
        "\u{201C}": "\"", "\u{201D}": "\"", "\u{201E}": "\"", "\u{2018}": "'", "\u{2019}": "'", "\u{201A}": "'",
        "`": "'", "\u{00B4}": "'",
        "\u{2013}": "-", "\u{2014}": "-", "\u{2010}": "-", "\u{2212}": "-",
    ]

    /// Approximate substring search (Sellers): every non-overlapping place in `haystack` within
    /// `maxErrors` edits of `needle`, best first per region. Indices are token offsets, end exclusive.
    static func search(_ needle: [String], in haystack: [String], maxErrors: Int) -> [(start: Int, end: Int, errors: Int)] {
        let m = needle.count, n = haystack.count
        guard m > 0, n > 0 else { return [] }
        // Column-wise DP over the haystack; `start` tracks where each alignment began.
        var previousCost = Array(0...m)
        var previousStart = Array(repeating: 0, count: m + 1)
        var candidates: [(start: Int, end: Int, errors: Int)] = []
        for j in 1...n {
            var cost = Array(repeating: 0, count: m + 1)
            var start = Array(repeating: j, count: m + 1)
            for i in 1...m {
                let substitution = previousCost[i - 1] + (needle[i - 1] == haystack[j - 1] ? 0 : 1)
                let skipNeedle = cost[i - 1] + 1
                let skipHaystack = previousCost[i] + 1
                if substitution <= skipNeedle, substitution <= skipHaystack {
                    cost[i] = substitution
                    start[i] = i == 1 ? j - 1 : previousStart[i - 1]
                } else if skipHaystack <= skipNeedle {
                    cost[i] = skipHaystack
                    start[i] = previousStart[i]
                } else {
                    cost[i] = skipNeedle
                    start[i] = start[i - 1]
                }
            }
            if cost[m] <= maxErrors, start[m] < j {
                candidates.append((start[m], j, cost[m]))
            }
            previousCost = cost
            previousStart = start
            previousStart[0] = j
        }
        // Keep the best of overlapping candidates: fewest errors, then closest to the query length.
        let ranked = candidates.sorted {
            ($0.errors, abs(($0.end - $0.start) - m), $0.start) < ($1.errors, abs(($1.end - $1.start) - m), $1.start)
        }
        var chosen: [(start: Int, end: Int, errors: Int)] = []
        for candidate in ranked where !chosen.contains(where: { $0.start < candidate.end && candidate.start < $0.end }) {
            chosen.append(candidate)
        }
        return chosen.sorted { $0.start < $1.start }
    }
}
