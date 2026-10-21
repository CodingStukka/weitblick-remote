import AgentControl
import CoreGraphics
import Foundation
import Vision

/// On-device text recognition (Apple Vision, German and English) for `read_screen` and `find_text`.
/// Works on desktop pixels, upscaled for small text, and reports boxes in desktop pixels.
enum ScreenReader {
    /// One recognized line: its text and where it is.
    struct Line: Sendable {
        let text: String
        /// Desktop pixels.
        let box: CGRect
        let confidence: Float
        /// Box of a part of `text` (desktop pixels), for matches inside a line.
        let boxOfRange: @Sendable (Range<String.Index>) -> CGRect?
    }

    /// Small UI text reads better at twice the size; large desktops are read as they are.
    static func upscaleFactor(for pixels: DesktopPixels) -> Int {
        max(pixels.rect.width, pixels.rect.height) <= 2000 ? 2 : 1
    }

    /// Recognizes the text in `pixels`, top to bottom. Runs off the main thread.
    static func read(_ pixels: DesktopPixels) async throws -> [Line] {
        let factor = upscaleFactor(for: pixels)
        let rect = pixels.rect
        let source = pixels.image
        return try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                do {
                    let image = factor == 1 ? source
                        : ScreenCapture.resized(source, width: source.width * factor, height: source.height * factor) ?? source
                    let request = VNRecognizeTextRequest()
                    request.recognitionLevel = .accurate
                    request.recognitionLanguages = ["de-DE", "en-US"]
                    request.usesLanguageCorrection = true
                    request.minimumTextHeight = 0
                    try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
                    let observations = request.results ?? []
                    let toDesktop: @Sendable (CGRect) -> CGRect = { normalized in
                        CGRect(x: Double(rect.x) + normalized.minX * Double(rect.width),
                               y: Double(rect.y) + (1 - normalized.maxY) * Double(rect.height),
                               width: normalized.width * Double(rect.width),
                               height: normalized.height * Double(rect.height))
                    }
                    let lines = observations.compactMap { observation -> Line? in
                        guard let candidate = observation.topCandidates(1).first else { return nil }
                        nonisolated(unsafe) let recognized = candidate
                        return Line(text: candidate.string, box: toDesktop(observation.boundingBox),
                                    confidence: candidate.confidence,
                                    boxOfRange: { range in
                                        (try? recognized.boundingBox(for: range)).map { toDesktop($0.boundingBox) }
                                    })
                    }
                    continuation.resume(returning: lines.sorted { ($0.box.minY, $0.box.minX) < ($1.box.minY, $1.box.minX) })
                } catch {
                    continuation.resume(throwing: error)
                }
            }
        }
    }
}
