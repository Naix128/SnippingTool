import unittest

import screenshot_app as app


class CaptureMagnifierZoomTests(unittest.TestCase):
    def test_compact_panel_dimensions_are_stable(self) -> None:
        magnifier = app.CaptureMagnifier()

        panel_width = magnifier.PREVIEW_WIDTH + magnifier.BORDER * 2
        panel_height = (
            magnifier.PREVIEW_HEIGHT
            + magnifier.FOOTER_HEIGHT
            + magnifier.BORDER * 2
        )

        self.assertEqual((panel_width, panel_height), (276, 320))

    def test_wheel_zoom_clamps_to_supported_levels(self) -> None:
        magnifier = app.CaptureMagnifier()

        for _ in range(20):
            magnifier.adjust_zoom(1)
        self.assertEqual(magnifier.zoom_index, len(magnifier.ZOOM_SAMPLES) - 1)
        self.assertFalse(magnifier.adjust_zoom(1))

        for _ in range(20):
            magnifier.adjust_zoom(-1)
        self.assertEqual(magnifier.zoom_index, 0)
        self.assertFalse(magnifier.adjust_zoom(-1))

    def test_default_zoom_uses_seventeen_by_eleven_sample(self) -> None:
        magnifier = app.CaptureMagnifier()

        self.assertEqual(
            magnifier.ZOOM_SAMPLES[magnifier.zoom_index],
            (17, 11),
        )

    def test_disabled_magnifier_can_still_be_forced_temporarily(self) -> None:
        overlay = app.CaptureOverlay.__new__(app.CaptureOverlay)
        overlay.show_magnifier = False
        overlay.force_magnifier = False
        overlay.selection_box = None
        overlay.is_selecting = False
        overlay.is_drawing = False
        overlay.is_transforming = False

        self.assertFalse(overlay._magnifier_is_visible())

        overlay.force_magnifier = True
        self.assertTrue(overlay._magnifier_is_visible())


if __name__ == "__main__":
    unittest.main()
