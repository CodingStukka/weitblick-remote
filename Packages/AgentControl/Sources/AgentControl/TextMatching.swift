import Foundation

/// A place where a query was found in a recognized line.
public struct TextMatch: Equatable, Sendable {
    public let lineIndex: Int
    /// The matched characters in the line's text.
    public let range: Range<String.Index>
    /// Edit distance to the query after folding (0 = same text ignoring case and accents).
    public let errors: Int
}

/// Finds a query in OCR lines: case-, accent- and width-insensitive, tolerant of a few recognition
/// errors (one per five characters). Exact matches win; approximate ones are only returned when
/// there is no exact one.
public enum TextMatching {
    public static func find(_ query: String, in lines: [String], limit: Int = 20) -> [TextMatch] {
        let needle = tokens(of: query.split(whereSeparator: \.isWhitespace).joined(separator: " "))
        guard !needle.isEmpty else { return [] }
        let allowedErrors = needle.count / 5
        var matches: [TextMatch] = []
        for (index, line) in lines.enumerated() {
            let characters = Array(line.indices)
            let haystack = line.map(token)
            for found in search(needle, in: haystack, maxErrors: allowedErrors) {
                let start = characters[found.start]
                let end = found.end < characters.count ? characters[found.end] : line.endIndex
                matches.append(TextMatch(lineIndex: index, range: start..<end, errors: found.errors))
            }
        }
        if matches.contains(where: { $0.errors == 0 }) { matches.removeAll { $0.errors > 0 } }
        matches.sort { ($0.errors, $0.lineIndex) < ($1.errors, $1.lineIndex) }
        return Array(matches.prefix(limit))
    }

    static func tokens(of text: String) -> [String] { text.map(token) }

    /// One comparable unit per character.
    static func token(_ character: Character) -> String {
        if character.isWhitespace { return " " }
        return String(character).folding(options: [.caseInsensitive, .diacriticInsensitive, .widthInsensitive], locale: nil)
    }

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
