"""Shows the remote desktop and turns mouse input into RDP pointer events (port of SessionView,
FramePresenter, DesktopGeometry, ScrollAccumulator and RemoteCursors)."""

from __future__ import annotations

import math
from dataclasses import dataclass

import gi

gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, GObject, Graphene, Gsk, Gtk  # noqa: E402

from ..bridge import MouseButton  # noqa: E402
from ..rdp.session import PointerEvent, RDPSession  # noqa: E402


@dataclass(frozen=True)
class DesktopGeometry:
    """Where the remote desktop sits inside the view (aspect fit, centered) and how view
    coordinates map to remote pixels."""

    view_width: float
    view_height: float
    desktop_width: int
    desktop_height: int

    @property
    def points_per_pixel(self) -> float:
        """View units per remote pixel (0.5 for a 1:1 desktop on a 200 % screen)."""
        if self.desktop_width <= 0 or self.desktop_height <= 0:
            return 1.0
        return min(self.view_width / self.desktop_width, self.view_height / self.desktop_height)

    @property
    def content_rect(self) -> tuple[float, float, float, float]:
        if self.desktop_width <= 0 or self.desktop_height <= 0 or self.view_width <= 0 or self.view_height <= 0:
            return (0.0, 0.0, self.view_width, self.view_height)
        scale = self.points_per_pixel
        width, height = self.desktop_width * scale, self.desktop_height * scale
        return ((self.view_width - width) / 2, (self.view_height - height) / 2, width, height)

    def remote_point(self, x: float, y: float) -> tuple[int, int]:
        left, top, _w, _h = self.content_rect
        scale = self.points_per_pixel
        if scale <= 0:
            return (0, 0)
        rx = math.floor((x - left) / scale)
        ry = math.floor((y - top) / scale)
        return (min(max(rx, 0), max(self.desktop_width - 1, 0)), min(max(ry, 0), max(self.desktop_height - 1, 0)))


class ScrollAccumulator:
    """Turns GTK scroll deltas into Windows wheel units (120 per notch). Deltas already carry the
    user's natural-scrolling choice. GTK's positive dy scrolls down, RDP's positive wheel scrolls up;
    positive dx and RDP's positive horizontal wheel both scroll right."""

    NOTCH = 120
    #: Touchpad pixels per notch (Windows scrolls three lines per notch).
    PIXELS_PER_NOTCH = 30.0

    def __init__(self) -> None:
        self._vertical = 0.0
        self._horizontal = 0.0

    def reset(self) -> None:
        self._vertical = 0.0
        self._horizontal = 0.0

    def add(self, dx: float, dy: float, precise: bool) -> tuple[int, int]:
        """Adds one scroll event; returns (vertical, horizontal) whole notches in wheel units."""
        per_unit = 1 / self.PIXELS_PER_NOTCH if precise else 1.0
        self._vertical -= dy * per_unit
        self._horizontal += dx * per_unit
        vertical = int(self._vertical)  # toward zero
        horizontal = int(self._horizontal)
        self._vertical -= vertical
        self._horizontal -= horizontal
        return vertical * self.NOTCH, horizontal * self.NOTCH


class RemoteCursors:
    """Server cursor shapes by id, turned into GDK cursors at the view's scale."""

    def __init__(self) -> None:
        self._images: dict[int, object] = {}  # id -> PointerImage
        self._current_id: int | None = None
        self._hidden = False
        self.points_per_pixel = 1.0
        self._cache: dict[tuple[int, int], Gdk.Cursor] = {}

    def apply(self, event: PointerEvent) -> bool:
        """Applies a pointer event; True when the visible cursor changed."""
        if event.kind == "new" and event.image is not None:
            self._images[event.id] = event.image
            return False
        if event.kind == "free":
            self._images.pop(event.id, None)
            self._cache = {k: v for k, v in self._cache.items() if k[0] != event.id}
            return False
        if event.kind == "set":
            self._current_id, self._hidden = event.id, False
            return True
        if event.kind == "hidden":
            self._hidden = True
            return True
        if event.kind == "default":
            self._current_id, self._hidden = None, False
            return True
        return False  # "moved": warping the local pointer would fight the user's own movement

    def current(self) -> Gdk.Cursor | None:
        if self._hidden:
            return Gdk.Cursor.new_from_name("none", None)
        image = self._images.get(self._current_id) if self._current_id is not None else None
        if image is None or image.width == 0 or image.height == 0:
            return None
        scale_key = round(self.points_per_pixel * 100)
        key = (image.id, scale_key)
        if key not in self._cache:
            self._cache[key] = self._make_cursor(image)
        return self._cache[key]

    def _make_cursor(self, image) -> Gdk.Cursor:
        texture = Gdk.MemoryTexture.new(image.width, image.height, Gdk.MemoryFormat.B8G8R8A8,
                                        GLib.Bytes.new(image.bgra), image.width * 4)
        scale = self.points_per_pixel
        hot_x, hot_y = image.hotspot_x, image.hotspot_y
        if abs(scale - 1) > 0.1:
            # Shown smaller or larger than 1:1: the cursor follows, like the desktop around it.
            width, height = max(1, round(image.width * scale)), max(1, round(image.height * scale))
            pixbuf = Gdk.pixbuf_get_from_texture(texture)
            if pixbuf is not None:
                scaled = pixbuf.scale_simple(width, height, GdkPixbuf.InterpType.BILINEAR)
                texture = Gdk.Texture.new_for_pixbuf(scaled)
                hot_x, hot_y = min(round(hot_x * scale), width - 1), min(round(hot_y * scale), height - 1)
        return Gdk.Cursor.new_from_texture(texture, hot_x, hot_y, None)


_BUTTONS = {1: MouseButton.LEFT, 2: MouseButton.MIDDLE, 3: MouseButton.RIGHT, 8: MouseButton.X1, 9: MouseButton.X2}


class SessionView(Gtk.Widget):
    """The remote desktop, aspect-fit on black. Frames are copied on the next paint after the
    session signals new pixels, so the view never decodes faster than the display refreshes."""

    __gtype_name__ = "WBSessionView"
    __gsignals__ = {
        # The view's size or scale changed (the session page asks for a new remote resolution).
        "geometry-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        # A mouse button or wheel event is about to go out.
        "pointer-pressed": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__()
        self.set_focusable(True)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.session: RDPSession | None = None
        #: False while an agent controls the session: local mouse input is dropped.
        self.accepts_local_input = True
        self._handlers: list[int] = []
        self._texture: Gdk.Texture | None = None
        self._desktop = (0, 0)
        self._frame_pending = False
        self._scroll = ScrollAccumulator()
        self._cursors = RemoteCursors()
        self._last_point: tuple[int, int] | None = None
        self._last_size = (0, 0, 0.0)

        legacy = Gtk.EventControllerLegacy()
        legacy.connect("event", self._on_event)
        self.add_controller(legacy)
        self.connect("notify::scale-factor", lambda *_: self.emit("geometry-changed"))
        self.connect("map", self._on_map)

    # MARK: Session

    def set_session(self, session: RDPSession | None) -> None:
        if self.session is not None:
            for handler in self._handlers:
                self.session.disconnect(handler)
        self._handlers = []
        self.session = session
        if session is not None:
            self._handlers = [
                session.connect("frame-ready", self._on_frame_ready),
                session.connect("desktop-resized", self._on_frame_ready),
                session.connect("pointer", self._on_pointer),
            ]
            self._frame_pending = True
            self.queue_draw()

    @property
    def desktop_size(self) -> tuple[int, int]:
        return self._desktop

    def backing_scale(self) -> float:
        """Device pixels per view unit (fractional scaling included)."""
        native = self.get_native()
        surface = native.get_surface() if native is not None else None
        if surface is not None:
            try:
                return float(surface.get_scale())
            except (AttributeError, TypeError):
                pass
        return float(self.get_scale_factor())

    def geometry(self) -> DesktopGeometry:
        width, height = self._desktop
        if (width, height) == (0, 0) and self.session is not None:
            width, height = self.session.desktop_size
        return DesktopGeometry(self.get_width(), self.get_height(), width, height)

    def _on_frame_ready(self, *_args) -> None:
        self._frame_pending = True
        self.queue_draw()

    def _on_map(self, *_args) -> None:
        # Frames that came while the tab was hidden were never taken; take the current picture.
        self._frame_pending = True
        self.queue_draw()

    def _take_frame(self) -> None:
        self._frame_pending = False
        if self.session is None:
            return
        frame = self.session.acquire_frame()
        if frame is None:
            return
        # X: some codecs leave the alpha byte empty; the desktop is always opaque.
        self._texture = Gdk.MemoryTexture.new(frame.width, frame.height, Gdk.MemoryFormat.B8G8R8X8,
                                              GLib.Bytes.new(frame.data), frame.stride)
        if self._desktop != (frame.width, frame.height):
            self._desktop = (frame.width, frame.height)
            self._update_cursor_scale()

    # MARK: Drawing

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        if self._frame_pending:
            self._take_frame()
        width, height = self.get_width(), self.get_height()
        black = Gdk.RGBA()
        black.parse("black")
        snapshot.append_color(black, Graphene.Rect().init(0, 0, width, height))
        if self._texture is None:
            return
        left, top, w, h = self.geometry().content_rect
        scale = self.backing_scale()
        # 1:1 in device pixels: sharp; otherwise smooth.
        exact = abs(w * scale - self._desktop[0]) < 0.5 and abs(h * scale - self._desktop[1]) < 0.5
        filter_ = Gsk.ScalingFilter.NEAREST if exact else Gsk.ScalingFilter.LINEAR
        snapshot.append_scaled_texture(self._texture, filter_, Graphene.Rect().init(left, top, w, h))

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        size = (width, height, self.backing_scale())
        if size != self._last_size:
            self._last_size = size
            self._update_cursor_scale()
            self.emit("geometry-changed")

    def do_measure(self, orientation, for_size):
        return (100, 400, -1, -1)

    # MARK: Cursor

    def _on_pointer(self, _session, event: PointerEvent) -> None:
        if self._cursors.apply(event):
            self.set_cursor(self._cursors.current())

    def _update_cursor_scale(self) -> None:
        scale = self.geometry().points_per_pixel
        if abs(scale - self._cursors.points_per_pixel) > 0.01:
            self._cursors.points_per_pixel = scale
            self.set_cursor(self._cursors.current())

    # MARK: Mouse

    def _widget_point(self, event: Gdk.Event) -> tuple[float, float] | None:
        found, x, y = event.get_position()
        native = self.get_native()
        if not found or native is None:
            return None
        offset_x, offset_y = native.get_surface_transform()
        point = native.compute_point(self, Graphene.Point().init(x - offset_x, y - offset_y))
        if isinstance(point, tuple):  # (ok, point)
            ok, point = point
            if not ok:
                return None
        return (point.x, point.y)

    def _remote_point(self, event: Gdk.Event) -> tuple[int, int] | None:
        point = self._widget_point(event)
        if point is None:
            return None
        return self.geometry().remote_point(*point)

    def _on_event(self, _controller, event: Gdk.Event | None) -> bool:
        if event is None:  # event types PyGObject can't wrap (crossing, focus)
            return False
        kind = event.get_event_type()
        if kind not in (Gdk.EventType.MOTION_NOTIFY, Gdk.EventType.BUTTON_PRESS, Gdk.EventType.BUTTON_RELEASE,
                        Gdk.EventType.SCROLL):
            return False
        if kind == Gdk.EventType.BUTTON_PRESS and not self.has_focus():
            self.grab_focus()
        session = self.session
        if session is None or not session.is_connected or not self.accepts_local_input:
            return kind != Gdk.EventType.MOTION_NOTIFY
        point = self._remote_point(event)
        if point is None:
            return False
        if kind == Gdk.EventType.MOTION_NOTIFY:
            if point != self._last_point:
                self._last_point = point
                session.send_mouse_move(*point)
            return True
        if kind == Gdk.EventType.SCROLL:
            return self._scroll_event(session, event, point)
        button = _BUTTONS.get(event.get_button())
        if button is None:
            return True
        down = kind == Gdk.EventType.BUTTON_PRESS
        if down:
            self.emit("pointer-pressed")
        session.send_mouse_move(*point)
        session.send_mouse_button(button, down, *point)
        return True

    def _scroll_event(self, session: RDPSession, event: Gdk.Event, point: tuple[int, int]) -> bool:
        direction = event.get_direction()
        if direction == Gdk.ScrollDirection.SMOOTH:
            dx, dy = event.get_deltas()
            precise = event.get_unit() == Gdk.ScrollUnit.SURFACE
            if event.is_stop():
                self._scroll.reset()
                return True
        else:
            dx, dy, precise = {
                Gdk.ScrollDirection.UP: (0, -1), Gdk.ScrollDirection.DOWN: (0, 1),
                Gdk.ScrollDirection.LEFT: (-1, 0), Gdk.ScrollDirection.RIGHT: (1, 0),
            }.get(direction, (0, 0)) + (False,)
        vertical, horizontal = self._scroll.add(dx, dy, precise)
        if vertical or horizontal:
            self.emit("pointer-pressed")
            session.send_mouse_wheel(vertical, horizontal, *point)
        return True
