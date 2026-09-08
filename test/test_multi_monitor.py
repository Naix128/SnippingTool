import unittest
from unittest.mock import patch

from PIL import Image

import screenshot_app as app


class VirtualDesktopGeometryTests(unittest.TestCase):
    def test_converts_between_local_and_absolute_coordinates(self) -> None:
        geometry = app.VirtualDesktopGeometry(-1920, -200, 4480, 1640)

        self.assertEqual(geometry.origin, (-1920, -200))
        self.assertEqual(geometry.bounds, (-1920, -200, 2560, 1440))
        self.assertEqual(geometry.to_absolute_point((210, 350)), (-1710, 150))
        self.assertEqual(
            geometry.to_absolute_box((100, 50, 500, 300)),
            (-1820, -150, -1420, 100),
        )
        self.assertEqual(
            geometry.to_local_box((-1820, -150, -1420, 100)),
            (100, 50, 500, 300),
        )

    def test_tk_geometry_uses_explicit_negative_coordinates(self) -> None:
        geometry = app.VirtualDesktopGeometry(-1920, -200, 4480, 1640)

        self.assertEqual(geometry.window_geometry, "4480x1640+-1920+-200")
        self.assertEqual(
            app.PinnedImageWindow._position_geometry(-1600, -100),
            "+-1600+-100",
        )


class ScreenshotManagerMultiMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = app.ScreenshotManager(
            app.ScreenshotSettings(app.Path("screenshots"))
        )

    @patch.object(app.platform, "system", return_value="Windows")
    @patch.object(app.VirtualDesktopGeometry, "detect")
    @patch.object(app.ImageGrab, "grab")
    def test_capture_snapshot_requests_all_screens(
        self,
        grab_mock,
        detect_mock,
        system_mock,
    ) -> None:
        del system_mock
        grab_mock.return_value = Image.new("RGB", (3840, 1080), "white")
        detect_mock.return_value = app.VirtualDesktopGeometry(0, 0, 3840, 1080)

        snapshot = self.manager.capture_snapshot()

        grab_mock.assert_called_once_with(all_screens=True)
        self.assertEqual(snapshot.image.size, (3840, 1080))
        self.assertEqual(snapshot.geometry.bounds, (0, 0, 3840, 1080))

    @patch.object(app.platform, "system", return_value="Windows")
    @patch.object(app.ImageGrab, "grab")
    def test_absolute_box_capture_keeps_negative_coordinates(
        self,
        grab_mock,
        system_mock,
    ) -> None:
        del system_mock
        grab_mock.return_value = Image.new("RGB", (400, 300), "white")

        image = self.manager.grab_box((-1800, -100, -1400, 200))

        grab_mock.assert_called_once_with(
            bbox=(-1800, -100, -1400, 200),
            all_screens=True,
        )
        self.assertEqual(image.size, (400, 300))


if __name__ == "__main__":
    unittest.main()
