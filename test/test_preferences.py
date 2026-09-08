import unittest
from unittest.mock import Mock, patch

from PIL import Image

import screenshot_app as app


class PinPreferencesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Mock()
        self.status = Mock()
        self.ocr = Mock()
        self.manager = app.PinManager(
            self.root,
            copy_func=Mock(),
            save_func=Mock(),
            status_func=self.status,
            default_opacity=0.65,
            always_on_top=False,
            restore_limit=2,
            ocr_func=self.ocr,
        )
        self.image = Image.new("RGB", (40, 30), "white")

    @patch.object(app, "PinnedImageWindow")
    def test_new_pin_receives_configured_defaults(self, window_class) -> None:
        window = Mock()
        window_class.return_value = window

        result = self.manager.pin_image(self.image, position=(10, 20))

        self.assertIs(result, window)
        self.assertEqual(window_class.call_args.kwargs["initial_opacity"], 0.65)
        self.assertFalse(window_class.call_args.kwargs["initial_topmost"])
        self.assertEqual(window_class.call_args.kwargs["max_window_size"], 12000)
        self.assertEqual(window_class.call_args.kwargs["thumbnail_size"], (180, 120))
        self.assertIs(window_class.call_args.kwargs["ocr_func"], self.ocr)

    def test_reconfigure_clamps_and_trims_restore_queue(self) -> None:
        self.manager.closed_images = [self.image.copy() for _ in range(5)]

        self.manager.configure_defaults(1.5, True, 1)

        self.assertEqual(self.manager.default_opacity, 1.0)
        self.assertTrue(self.manager.always_on_top)
        self.assertEqual(self.manager.restore_limit, 1)
        self.assertEqual(len(self.manager.closed_images), 1)

    def test_new_configuration_defaults_are_backward_compatible(self) -> None:
        config = app.PersistedAppConfig()

        self.assertEqual(config.pin_default_opacity, 100)
        self.assertTrue(config.pin_always_on_top)
        self.assertEqual(config.pin_restore_limit, 3)
        self.assertEqual(config.pin_max_size, 12000)
        self.assertEqual(config.pin_thumbnail_width, 180)
        self.assertEqual(config.pin_thumbnail_height, 120)

    def test_reconfigure_updates_pin_size_defaults(self) -> None:
        self.manager.configure_defaults(
            0.8,
            True,
            4,
            max_window_size=2400,
            thumbnail_size=(96, 72),
        )

        self.assertEqual(self.manager.max_window_size, 2400)
        self.assertEqual(self.manager.thumbnail_size, (96, 72))

    def test_pin_scale_is_limited_by_configured_maximum(self) -> None:
        window = app.PinnedImageWindow.__new__(app.PinnedImageWindow)
        window.image = Image.new("RGB", (4000, 2000), "white")
        window.max_window_size = 1000

        self.assertEqual(window._clamp_scale(2.0), 0.25)

if __name__ == "__main__":
    unittest.main()
