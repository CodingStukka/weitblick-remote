import Foundation

/// Compares two frames of the same size (4 bytes per pixel, rows `stride` bytes apart) to tell when
/// the screen has settled. A blinking caret is not a change worth waiting for.
public enum FrameDiff {
    /// The bounding box of all changed pixels, or nil when the frames are equal.
    public static func changedBounds(
        _ old: UnsafeRawBufferPointer, _ new: UnsafeRawBufferPointer, width: Int, height: Int, stride: Int
    ) -> DesktopRect? {
        let rowBytes = width * 4
        guard width > 0, height > 0, stride >= rowBytes, old.count >= stride * (height - 1) + rowBytes,
              new.count >= stride * (height - 1) + rowBytes,
              let oldBase = old.baseAddress, let newBase = new.baseAddress
        else { return nil }
        var minX = width, minY = height, maxX = -1, maxY = -1
        for y in 0..<height {
            let oldRow = oldBase + y * stride
            let newRow = newBase + y * stride
            guard memcmp(oldRow, newRow, rowBytes) != 0 else { continue }
            minY = min(minY, y)
            maxY = y
            let oldPixels = oldRow.assumingMemoryBound(to: UInt32.self)
            let newPixels = newRow.assumingMemoryBound(to: UInt32.self)
            var first = 0
            while first < width, oldPixels[first] == newPixels[first] { first += 1 }
            var last = width - 1
            while last > first, oldPixels[last] == newPixels[last] { last -= 1 }
            minX = min(minX, first)
            maxX = max(maxX, last)
        }
        guard maxY >= 0 else { return nil }
        return DesktopRect(x: minX, y: minY, width: maxX - minX + 1, height: maxY - minY + 1)
    }

    /// Whether a change is big enough to count (more than a caret: wider than `maxIgnoredWidth` or
    /// taller than `maxIgnoredHeight`).
    public static func isSignificant(_ bounds: DesktopRect?, maxIgnoredWidth: Int = 4, maxIgnoredHeight: Int = 48) -> Bool {
        guard let bounds else { return false }
        return bounds.width > maxIgnoredWidth || bounds.height > maxIgnoredHeight
    }
}

/// Decides when a polled screen has settled: no significant change for `settle` seconds.
public struct SettleTracker: Sendable {
    public let settle: Double
    public private(set) var lastChange: Double

    public init(settle: Double, start: Double) {
        self.settle = settle
        lastChange = start
    }

    /// Feed one poll; true once nothing significant changed for `settle` seconds.
    public mutating func update(changed: Bool, at time: Double) -> Bool {
        if changed { lastChange = time }
        return time - lastChange >= settle
    }
}
