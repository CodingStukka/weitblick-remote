import AppKit
import QuartzCore
import WeitblickKit

/// Shows the remote desktop and turns mouse input into RDP pointer events.
@MainActor
final class SessionView: NSView {
    weak var session: RDPSession?
    /// Runs before a mouse button press or wheel event goes out (⌘-click = Ctrl-click).
    var willSendPointerPress: (() -> Void)?
    /// False while an agent controls the session: local mouse input is dropped.
    var acceptsLocalInput = true

    private let presenter = FramePresenter()
    private let cursors = RemoteCursors()
    private var scroll = ScrollAccumulator()
    private var displayLink: CADisplayLink?
    private var framePending = false
    private var idleTicks = 0
    /// Ticks without new pixels before the display link pauses.
    private static let idleTicksBeforePause = 30

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.backgroundColor = NSColor.black.cgColor
        layer?.addSublayer(presenter.layer)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }

    override var isFlipped: Bool { true }
    override var acceptsFirstResponder: Bool { true }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    private var geometry: DesktopGeometry {
        DesktopGeometry(bounds: bounds, desktop: presenter.pixelSize ?? session?.desktopSize ?? PixelSize(width: 0, height: 0))
    }

    // MARK: Frames

    /// The session has new pixels; they are copied on the next display refresh.
    func setNeedsPresent() {
        framePending = true
        idleTicks = 0
        displayLink?.isPaused = false
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        displayLink?.invalidate()
        displayLink = nil
        guard window != nil else { return }
        let link = displayLink(target: self, selector: #selector(displayRefresh))
        link.add(to: .main, forMode: .common)
        displayLink = link
    }

    @objc private func displayRefresh(_ link: CADisplayLink) {
        guard framePending, let session else {
            idleTicks += 1
            if idleTicks >= Self.idleTicksBeforePause { link.isPaused = true }
            return
        }
        framePending = false
        if presenter.present(from: session) { updateCursorScale() }
    }

    /// Stops the display link; it retains this view.
    func tearDown() {
        displayLink?.invalidate()
        displayLink = nil
    }

    override func layout() {
        super.layout()
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        presenter.layer.frame = bounds
        CATransaction.commit()
        updateCursorScale()
    }

    // MARK: Cursor

    func apply(_ pointer: RemotePointerEvent) {
        // `.moved` (server warps the cursor) is ignored on purpose: warping the local pointer
        // would fight the user's own mouse movement.
        guard cursors.apply(pointer) else { return }
        refreshCursor()
    }

    private func updateCursorScale() {
        let scale = geometry.pointsPerPixel
        guard scale != cursors.pointsPerPixel else { return }
        cursors.pointsPerPixel = scale
        refreshCursor()
    }

    private func refreshCursor() {
        window?.invalidateCursorRects(for: self)
        if let window, window.isKeyWindow, bounds.contains(convert(window.mouseLocationOutsideOfEventStream, from: nil)) {
            cursors.current.set()
        }
    }

    override func resetCursorRects() {
        addCursorRect(bounds, cursor: cursors.current)
    }

    // MARK: Mouse

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach(removeTrackingArea)
        addTrackingArea(NSTrackingArea(rect: .zero, options: [.mouseMoved, .activeInKeyWindow, .inVisibleRect],
                                       owner: self, userInfo: nil))
    }

    private func remotePoint(_ event: NSEvent) -> RemotePoint {
        geometry.remotePoint(for: convert(event.locationInWindow, from: nil))
    }

    override func mouseMoved(with event: NSEvent) { move(event) }
    override func mouseDragged(with event: NSEvent) { move(event) }
    override func rightMouseDragged(with event: NSEvent) { move(event) }
    override func otherMouseDragged(with event: NSEvent) { move(event) }

    private func move(_ event: NSEvent) {
        guard acceptsLocalInput else { return }
        session?.sendMouseMove(to: remotePoint(event))
    }

    override func mouseDown(with event: NSEvent) { send(.left, down: true, event) }
    override func mouseUp(with event: NSEvent) { send(.left, down: false, event) }
    override func rightMouseDown(with event: NSEvent) { send(.right, down: true, event) }
    override func rightMouseUp(with event: NSEvent) { send(.right, down: false, event) }

    override func otherMouseDown(with event: NSEvent) {
        if let button = Self.otherButton(event.buttonNumber) { send(button, down: true, event) }
    }

    override func otherMouseUp(with event: NSEvent) {
        if let button = Self.otherButton(event.buttonNumber) { send(button, down: false, event) }
    }

    private static func otherButton(_ number: Int) -> MouseButton? {
        switch number {
        case 2: .middle
        case 3: .back
        case 4: .forward
        default: nil
        }
    }

    private func send(_ button: MouseButton, down: Bool, _ event: NSEvent) {
        guard acceptsLocalInput else { return }
        if down { willSendPointerPress?() }
        let point = remotePoint(event)
        session?.sendMouseMove(to: point)
        session?.sendMouseButton(button, down: down, at: point)
    }

    override func scrollWheel(with event: NSEvent) {
        guard acceptsLocalInput else { return }
        let notches = scroll.add(
            deltaX: event.scrollingDeltaX, deltaY: event.scrollingDeltaY,
            precise: event.hasPreciseScrollingDeltas, gestureBegan: event.phase.contains(.began))
        guard notches.vertical != 0 || notches.horizontal != 0 else { return }
        willSendPointerPress?()
        session?.sendMouseWheel(vertical: notches.vertical, horizontal: notches.horizontal, at: remotePoint(event))
    }
}
