/// A point in screenshot pixels (as the model sees them).
public struct PixelPoint: Equatable, Sendable {
    public var x: Double
    public var y: Double

    public init(x: Double, y: Double) {
        self.x = x
        self.y = y
    }
}

/// A rectangle in pixels.
public struct PixelRect: Equatable, Sendable {
    public var x: Double
    public var y: Double
    public var width: Double
    public var height: Double

    public init(x: Double, y: Double, width: Double, height: Double) {
        self.x = x
        self.y = y
        self.width = width
        self.height = height
    }

    public var maxX: Double { x + width }
    public var maxY: Double { y + height }
    public var center: PixelPoint { PixelPoint(x: x + width / 2, y: y + height / 2) }
}

/// A rectangle on the remote desktop in whole pixels.
public struct DesktopRect: Equatable, Sendable {
    public var x: Int
    public var y: Int
    public var width: Int
    public var height: Int

    public init(x: Int, y: Int, width: Int, height: Int) {
        self.x = x
        self.y = y
        self.width = width
        self.height = height
    }
}

/// Between desktop pixels (what RDP uses) and screenshot pixels (what the model sees). Desktops whose
/// long side exceeds `maxSide` are scaled down for screenshots; everything else is 1:1.
public struct ScreenMapping: Equatable, Sendable {
    public let desktopWidth: Int
    public let desktopHeight: Int
    /// Screenshot pixels per desktop pixel, ≤ 1.
    public let scale: Double

    public init(desktopWidth: Int, desktopHeight: Int, maxSide: Int = ToolCatalog.maxScreenshotSide) {
        self.desktopWidth = max(1, desktopWidth)
        self.desktopHeight = max(1, desktopHeight)
        let longSide = max(self.desktopWidth, self.desktopHeight)
        scale = longSide > maxSide ? Double(maxSide) / Double(longSide) : 1
    }

    public var screenshotWidth: Int { max(1, Int((Double(desktopWidth) * scale).rounded())) }
    public var screenshotHeight: Int { max(1, Int((Double(desktopHeight) * scale).rounded())) }

    /// The desktop pixel under a screenshot point; nil (with the reason) outside the screenshot.
    public func desktopPoint(_ point: PixelPoint) throws(ToolArgumentError) -> (x: Int, y: Int) {
        guard point.x >= 0, point.y >= 0, point.x < Double(screenshotWidth) + 0.5, point.y < Double(screenshotHeight) + 0.5 else {
            throw ToolArgumentError(
                "Point \(format(point.x)),\(format(point.y)) is outside the screen "
                    + "(0…\(screenshotWidth - 1), 0…\(screenshotHeight - 1) in screenshot pixels).")
        }
        let x = Int((point.x / scale).rounded(.down))
        let y = Int((point.y / scale).rounded(.down))
        return (min(x, desktopWidth - 1), min(y, desktopHeight - 1))
    }

    /// The desktop pixels covering a screenshot region, clipped to the screen.
    public func desktopRect(_ region: PixelRect) throws(ToolArgumentError) -> DesktopRect {
        let minX = max(0, Int((region.x / scale).rounded(.down)))
        let minY = max(0, Int((region.y / scale).rounded(.down)))
        let maxX = min(desktopWidth, Int((region.maxX / scale).rounded(.up)))
        let maxY = min(desktopHeight, Int((region.maxY / scale).rounded(.up)))
        guard maxX > minX, maxY > minY else {
            throw ToolArgumentError(
                "The region is outside the screen (\(screenshotWidth)×\(screenshotHeight) screenshot pixels).")
        }
        return DesktopRect(x: minX, y: minY, width: maxX - minX, height: maxY - minY)
    }

    /// A desktop rectangle in screenshot pixels.
    public func screenshotRect(_ rect: PixelRect) -> PixelRect {
        PixelRect(x: rect.x * scale, y: rect.y * scale, width: rect.width * scale, height: rect.height * scale)
    }

    public func screenshotPoint(desktopX: Double, desktopY: Double) -> PixelPoint {
        PixelPoint(x: desktopX * scale, y: desktopY * scale)
    }

    /// "1280×800 screenshot pixels, scale 1 (1:1 with the desktop)" for tool results.
    public var summary: String {
        if scale == 1 {
            return "\(screenshotWidth)×\(screenshotHeight) px, scale 1 (screenshot pixels are desktop pixels)"
        }
        return "\(screenshotWidth)×\(screenshotHeight) px, scale \(format(scale, digits: 4)) "
            + "(desktop \(desktopWidth)×\(desktopHeight); coordinates are mapped for you)"
    }
}

func format(_ value: Double, digits: Int = 1) -> String {
    if value.rounded() == value, abs(value) < 1e9 { return String(Int(value)) }
    var factor = 1.0
    for _ in 0..<digits { factor *= 10 }
    let rounded = (value * factor).rounded() / factor
    return String(rounded)
}
