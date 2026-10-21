import AgentControl
import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers
import WeitblickKit

/// Desktop pixels copied out of a session's framebuffer.
struct DesktopPixels {
    let image: CGImage
    /// Where `image` sits on the desktop.
    let rect: DesktopRect
}

/// Screenshots for agents: copies pixels without taking the window's dirty rectangles (the session
/// may be on screen at the same time), scales to screenshot size and encodes PNG or JPEG.
enum ScreenCapture {
    /// Encoded images above this size go out as JPEG instead of PNG.
    static let pngLimit = 1_000_000

    /// `rect` of the desktop (all of it when nil), clipped to the framebuffer.
    @MainActor
    static func copy(_ session: RDPSession, rect: DesktopRect? = nil) -> DesktopPixels? {
        let copied: (Data, DesktopRect)?? = session.peekFramebuffer { framebuffer in
            let wanted = rect ?? DesktopRect(x: 0, y: 0, width: framebuffer.width, height: framebuffer.height)
            let minX = max(0, wanted.x), minY = max(0, wanted.y)
            let maxX = min(framebuffer.width, wanted.x + wanted.width)
            let maxY = min(framebuffer.height, wanted.y + wanted.height)
            guard maxX > minX, maxY > minY else { return nil }
            let clipped = DesktopRect(x: minX, y: minY, width: maxX - minX, height: maxY - minY)
            let rowBytes = clipped.width * 4
            var data = Data(count: rowBytes * clipped.height)
            data.withUnsafeMutableBytes { target in
                for row in 0..<clipped.height {
                    memcpy(target.baseAddress! + row * rowBytes,
                           framebuffer.pixels + (clipped.y + row) * framebuffer.bytesPerRow + clipped.x * 4, rowBytes)
                }
            }
            return (data, clipped)
        }
        guard let (data, clipped) = copied ?? nil, let image = makeImage(data, width: clipped.width, height: clipped.height)
        else { return nil }
        return DesktopPixels(image: image, rect: clipped)
    }

    /// BGRA32 pixels (little-endian ARGB words, alpha ignored).
    static func makeImage(_ pixels: Data, width: Int, height: Int) -> CGImage? {
        let info = CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue)
        guard let provider = CGDataProvider(data: pixels as CFData) else { return nil }
        return CGImage(width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: width * 4,
                       space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: info, provider: provider,
                       decode: nil, shouldInterpolate: false, intent: .defaultIntent)
    }

    /// `image` drawn at `width`×`height` (smooth downscaling, or plain upscaling for OCR).
    static func resized(_ image: CGImage, width: Int, height: Int) -> CGImage? {
        guard image.width != width || image.height != height else { return image }
        let info = CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue)
        guard let context = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                                      space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: info.rawValue)
        else { return nil }
        context.interpolationQuality = .high
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        return context.makeImage()
    }

    /// PNG, or JPEG when the PNG would be larger than `pngLimit` (photos, busy wallpapers).
    static func encode(_ image: CGImage) -> (data: Data, mimeType: String)? {
        if let png = encode(image, as: .png, properties: [:]), png.count <= pngLimit {
            return (png, "image/png")
        }
        let jpeg = encode(image, as: .jpeg, properties: [kCGImageDestinationLossyCompressionQuality: 0.85])
        return jpeg.map { ($0, "image/jpeg") }
    }

    private static func encode(_ image: CGImage, as type: UTType, properties: [CFString: Any]) -> Data? {
        let data = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(data, type.identifier as CFString, 1, nil) else { return nil }
        CGImageDestinationAddImage(destination, image, properties as CFDictionary)
        return CGImageDestinationFinalize(destination) ? data as Data : nil
    }
}

/// Watches a session's framebuffer until it stops changing (`wait_for_screen`, input with
/// `screenshot: true`). Polls a copy and diffs it, so it works for windowless sessions too.
@MainActor
struct ScreenSettler {
    /// Seconds without a significant change.
    var settle: Double
    var timeout: Double
    var interval: Duration = .milliseconds(100)

    enum Outcome {
        case settled(after: Double)
        /// Still changing at the deadline; the last change.
        case busy(DesktopRect?)
        case sessionGone
    }

    func wait(for session: () -> RDPSession?) async -> Outcome {
        let start = Date()
        var tracker = SettleTracker(settle: settle, start: 0)
        var previous: (data: Data, width: Int, height: Int, stride: Int)?
        var lastChange: DesktopRect?
        while true {
            guard let current = session() else { return .sessionGone }
            let elapsed = Date().timeIntervalSince(start)
            var changed = false
            current.peekFramebuffer { framebuffer in
                let size = framebuffer.bytesPerRow * framebuffer.height
                if let old = previous, old.width == framebuffer.width, old.height == framebuffer.height,
                   old.stride == framebuffer.bytesPerRow {
                    let bounds = old.data.withUnsafeBytes { oldBytes in
                        FrameDiff.changedBounds(oldBytes, UnsafeRawBufferPointer(start: framebuffer.pixels, count: size),
                                                width: framebuffer.width, height: framebuffer.height,
                                                stride: framebuffer.bytesPerRow)
                    }
                    if FrameDiff.isSignificant(bounds) {
                        changed = true
                        lastChange = bounds
                    }
                    if bounds != nil {
                        previous?.data.withUnsafeMutableBytes { $0.copyMemory(from: UnsafeRawBufferPointer(start: framebuffer.pixels, count: size)) }
                    }
                } else {
                    changed = previous != nil
                    previous = (Data(bytes: framebuffer.pixels, count: size), framebuffer.width, framebuffer.height,
                                framebuffer.bytesPerRow)
                }
            }
            if tracker.update(changed: changed, at: elapsed) { return .settled(after: elapsed) }
            if elapsed >= timeout { return .busy(lastChange) }
            try? await Task.sleep(for: interval)
        }
    }
}
