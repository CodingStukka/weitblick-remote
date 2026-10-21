/// A wrong tool argument; the message goes back to the model as an error result.
public struct ToolArgumentError: Error, Equatable, CustomStringConvertible {
    public var message: String

    public init(_ message: String) { self.message = message }

    public var description: String { message }
}

/// Typed access to a `tools/call` arguments object with model-readable errors. Unknown keys are ignored.
public struct ToolArguments: Sendable {
    public let raw: [String: JSONValue]

    public init(_ value: JSONValue) {
        raw = value.objectValue ?? [:]
    }

    public func has(_ key: String) -> Bool {
        if let value = raw[key], value != .null { return true }
        return false
    }

    public func string(_ key: String) throws(ToolArgumentError) -> String {
        guard let value = try optionalString(key) else { throw ToolArgumentError("Missing argument '\(key)'.") }
        return value
    }

    public func optionalString(_ key: String) throws(ToolArgumentError) -> String? {
        guard has(key) else { return nil }
        guard let value = raw[key]?.stringValue else { throw ToolArgumentError("'\(key)' must be a string.") }
        return value
    }

    /// A string that must not be empty after trimming.
    public func nonEmptyString(_ key: String) throws(ToolArgumentError) -> String {
        let value = try string(key)
        guard value.contains(where: { !$0.isWhitespace }) else { throw ToolArgumentError("'\(key)' is empty.") }
        return value
    }

    public func optionalBool(_ key: String) throws(ToolArgumentError) -> Bool? {
        guard has(key) else { return nil }
        if let value = raw[key]?.boolValue { return value }
        // Some clients send flags as strings.
        switch raw[key]?.stringValue?.lowercased() {
        case "true": return true
        case "false": return false
        default: throw ToolArgumentError("'\(key)' must be true or false.")
        }
    }

    public func bool(_ key: String, default fallback: Bool) throws(ToolArgumentError) -> Bool {
        try optionalBool(key) ?? fallback
    }

    public func number(_ key: String) throws(ToolArgumentError) -> Double {
        guard let value = try optionalNumber(key) else { throw ToolArgumentError("Missing argument '\(key)'.") }
        return value
    }

    public func optionalNumber(_ key: String) throws(ToolArgumentError) -> Double? {
        guard has(key) else { return nil }
        if let value = raw[key]?.doubleValue, value.isFinite { return value }
        if let text = raw[key]?.stringValue, let value = Double(text), value.isFinite { return value }
        throw ToolArgumentError("'\(key)' must be a number.")
    }

    /// A number in `range`; fractions are rounded for integer arguments by the caller.
    public func number(_ key: String, in range: ClosedRange<Double>, default fallback: Double? = nil)
        throws(ToolArgumentError) -> Double {
        guard let value = try optionalNumber(key) ?? fallback else {
            throw ToolArgumentError("Missing argument '\(key)'.")
        }
        guard range.contains(value) else {
            throw ToolArgumentError("'\(key)' must be between \(format(range.lowerBound)) and \(format(range.upperBound)).")
        }
        return value
    }

    public func integer(_ key: String, in range: ClosedRange<Int>, default fallback: Int? = nil)
        throws(ToolArgumentError) -> Int {
        guard let value = try optionalNumber(key).map({ Int($0.rounded()) }) ?? fallback else {
            throw ToolArgumentError("Missing argument '\(key)'.")
        }
        guard range.contains(value) else {
            throw ToolArgumentError("'\(key)' must be between \(range.lowerBound) and \(range.upperBound).")
        }
        return value
    }

    /// One of `choices` (case-insensitive), or `fallback` when missing.
    public func choice(_ key: String, of choices: [String], default fallback: String) throws(ToolArgumentError) -> String {
        guard let value = try optionalString(key)?.lowercased() else { return fallback }
        guard choices.contains(value) else {
            throw ToolArgumentError("'\(key)' must be one of: \(choices.joined(separator: ", ")).")
        }
        return value
    }

    public func stringArray(_ key: String) throws(ToolArgumentError) -> [String] {
        guard has(key) else { return [] }
        if let single = raw[key]?.stringValue { return [single] }
        guard let items = raw[key]?.arrayValue else { throw ToolArgumentError("'\(key)' must be a list of strings.") }
        var strings: [String] = []
        for item in items {
            guard let text = item.stringValue else { throw ToolArgumentError("'\(key)' must be a list of strings.") }
            strings.append(text)
        }
        return strings
    }

    /// `{x, y, width, height}` in screenshot pixels, or nil when missing.
    public func optionalRect(_ key: String) throws(ToolArgumentError) -> PixelRect? {
        guard has(key) else { return nil }
        guard let object = raw[key]?.objectValue else {
            throw ToolArgumentError("'\(key)' must be an object with x, y, width and height.")
        }
        var values: [Double] = []
        for name in ["x", "y", "width", "height"] {
            guard let value = object[name]?.doubleValue, value.isFinite else {
                throw ToolArgumentError("'\(key)' needs a number '\(name)'.")
            }
            values.append(value)
        }
        guard values[2] >= 1, values[3] >= 1 else { throw ToolArgumentError("'\(key)' must be at least 1×1 pixels.") }
        return PixelRect(x: values[0], y: values[1], width: values[2], height: values[3])
    }

    /// A list of points, as `[[x, y], …]` or `[{"x":…, "y":…}, …]`.
    public func points(_ key: String) throws(ToolArgumentError) -> [PixelPoint] {
        guard let items = raw[key]?.arrayValue else { throw ToolArgumentError("'\(key)' must be a list of [x, y] points.") }
        var points: [PixelPoint] = []
        for item in items {
            if let pair = item.arrayValue, pair.count == 2, let x = pair[0].doubleValue, let y = pair[1].doubleValue {
                points.append(PixelPoint(x: x, y: y))
            } else if let x = item["x"]?.doubleValue, let y = item["y"]?.doubleValue {
                points.append(PixelPoint(x: x, y: y))
            } else {
                throw ToolArgumentError("'\(key)' must be a list of [x, y] points.")
            }
        }
        return points
    }

    private func format(_ value: Double) -> String {
        value.rounded() == value ? String(Int(value)) : String(value)
    }
}
