"""Desktop geometry, scrolling and the desktop size for a view (ports of DesktopGeometryTests and
ScrollAccumulatorTests)."""

import unittest

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from weitblick.store.connection import DisplaySettings  # noqa: E402
from weitblick.ui.session_page import desktop_for_view  # noqa: E402
from weitblick.ui.session_view import DesktopGeometry, ScrollAccumulator  # noqa: E402


class GeometryTests(unittest.TestCase):
    def test_aspect_fit_centers(self):
        geometry = DesktopGeometry(1000, 600, 1280, 800)
        left, top, width, height = geometry.content_rect
        self.assertAlmostEqual(geometry.points_per_pixel, 0.75)
        self.assertAlmostEqual(width, 960)
        self.assertAlmostEqual(height, 600)
        self.assertAlmostEqual(left, 20)
        self.assertAlmostEqual(top, 0)

    def test_remote_point_maps_and_clamps(self):
        geometry = DesktopGeometry(640, 400, 1280, 800)
        self.assertEqual(geometry.remote_point(320, 200), (640, 400))
        self.assertEqual(geometry.remote_point(-10, 900), (0, 799))
        self.assertEqual(geometry.remote_point(639.9, 0), (1279, 0))

    def test_empty_desktop(self):
        geometry = DesktopGeometry(640, 400, 0, 0)
        self.assertEqual(geometry.content_rect, (0.0, 0.0, 640, 400))
        self.assertEqual(geometry.remote_point(10, 10), (0, 0))


class ScrollTests(unittest.TestCase):
    def test_wheel_notches(self):
        scroll = ScrollAccumulator()
        self.assertEqual(scroll.add(0, 1, precise=False), (-120, 0))  # down
        self.assertEqual(scroll.add(0, -2, precise=False), (240, 0))  # up
        self.assertEqual(scroll.add(1, 0, precise=False), (0, 120))  # right

    def test_touchpad_accumulates(self):
        scroll = ScrollAccumulator()
        self.assertEqual(scroll.add(0, 10, precise=True), (0, 0))
        self.assertEqual(scroll.add(0, 10, precise=True), (0, 0))
        self.assertEqual(scroll.add(0, 15, precise=True), (-120, 0))
        scroll.reset()
        self.assertEqual(scroll.add(0, 20, precise=True), (0, 0))

    def test_high_resolution_wheel(self):
        scroll = ScrollAccumulator()
        results = [scroll.add(0, -0.25, precise=False) for _ in range(4)]
        self.assertEqual(results[-1], (120, 0))
        self.assertEqual(sum(v for v, _h in results), 120)


class DesktopForViewTests(unittest.TestCase):
    def test_follows_window(self):
        display = DisplaySettings()
        self.assertEqual(desktop_for_view(display, 1001, 700, 2.0), (1000, 700, 100))

    def test_hidpi(self):
        display = DisplaySettings(retina=True)
        self.assertEqual(desktop_for_view(display, 1000, 700, 2.0), (2000, 1400, 200))
        self.assertEqual(desktop_for_view(display, 1000, 700, 1.25), (1250, 875, 125))

    def test_fixed_size_and_scale(self):
        display = DisplaySettings(match_screen_resolution=False, fixed_width=1921, fixed_height=1080,
                                  desktop_scale_factor=150)
        self.assertEqual(desktop_for_view(display, 800, 600, 1.0), (1920, 1080, 150))

    def test_clamps(self):
        display = DisplaySettings()
        self.assertEqual(desktop_for_view(display, 50, 9000, 1.0), (200, 8192, 100))


if __name__ == "__main__":
    unittest.main()
