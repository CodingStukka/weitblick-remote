import Foundation

/// Splits a byte stream into newline-delimited messages (the MCP stdio framing): `\n` ends a
/// message, a `\r` before it is dropped, empty lines are skipped. Not thread safe.
public struct LineFramer: Sendable {
    public enum Failure: Error, Equatable {
        /// A message grew beyond `maxLineLength` bytes without a newline.
        case lineTooLong(Int)
    }

    public let maxLineLength: Int
    private var buffer = Data()

    public init(maxLineLength: Int = 32 << 20) {
        self.maxLineLength = maxLineLength
    }

    /// Feeds bytes and returns the complete lines they finish, in order.
    public mutating func append(_ bytes: some DataProtocol) throws(Failure) -> [String] {
        buffer.append(contentsOf: bytes)
        var lines: [String] = []
        while let newline = buffer.firstIndex(of: 0x0A) {
            var line = buffer[buffer.startIndex..<newline]
            buffer.removeSubrange(buffer.startIndex...newline)
            if line.last == 0x0D { line = line.dropLast() }
            if !line.isEmpty { lines.append(String(decoding: line, as: UTF8.self)) }
        }
        if buffer.count > maxLineLength {
            let size = buffer.count
            buffer = Data()
            throw .lineTooLong(size)
        }
        return lines
    }

    /// Bytes waiting for their newline.
    public var pendingByteCount: Int { buffer.count }
}
