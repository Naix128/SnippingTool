import unittest
from unittest.mock import Mock

from PIL import Image

import screenshot_app as app


class CaptureActionPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.subject = app.ScreenshotApp.__new__(app.ScreenshotApp)
        self.subject.pending_screen = Image.new("RGB", (2, 2), "black")
        self.subject._receive_capture = Mock()
        self.subject.copy_last_image = Mock()
        self.subject.pin_manager = Mock()
        self.image = Image.new("RGB", (40, 30), "white")

    def test_enter_finish_action_copies_region(self) -> None:
        self.subject._finish_region_capture(
            self.image,
            "finish",
            position=(100, 200),
        )

        self.subject.copy_last_image.assert_called_once_with()
        self.subject._receive_capture.assert_called_once_with(
            self.image,
            force_save=False,
            kind="region",
            origin=(100, 200),
        )

    def test_enter_finish_action_copies_whiteboard(self) -> None:
        self.subject._finish_whiteboard(
            self.image,
            "finish",
            position=(10, 20),
        )

        self.subject.copy_last_image.assert_called_once_with()

    def test_save_does_not_copy(self) -> None:
        self.subject._finish_region_capture(self.image, "save")

        self.subject.copy_last_image.assert_not_called()
        self.subject._receive_capture.assert_called_once_with(
            self.image,
            force_save=True,
            kind="region",
            origin=None,
        )

    def test_pin_does_not_copy(self) -> None:
        self.subject._finish_region_capture(
            self.image,
            "pin",
            position=(30, 40),
        )

        self.subject.copy_last_image.assert_not_called()
        self.subject.pin_manager.pin_image.assert_called_once_with(
            self.image,
            "截图贴图",
            position=(30, 40),
        )

    def test_annotation_toolbar_has_no_duplicate_finish_button(self) -> None:
        commands = [item.command for item in app.FloatingToolbar.ACTION_ITEMS]

        self.assertIn("copy", commands)
        self.assertNotIn("finish", commands)


class CaptureArrowKeyTests(unittest.TestCase):
    def test_arrow_key_moves_pointer_before_a_region_is_selected(self) -> None:
        overlay = app.CaptureOverlay.__new__(app.CaptureOverlay)
        overlay.selection_box = None
        overlay.is_selecting = False
        overlay.active_tool = None
        overlay._move_pointer = Mock()

        result = overlay._handle_arrow_key(Mock(state=0), -1, 0)

        self.assertEqual(result, "break")
        overlay._move_pointer.assert_called_once_with(-1, 0)

    def test_arrow_key_does_not_move_pointer_during_selection_drag(self) -> None:
        overlay = app.CaptureOverlay.__new__(app.CaptureOverlay)
        overlay.selection_box = None
        overlay.is_selecting = True
        overlay.active_tool = None
        overlay._move_pointer = Mock()

        result = overlay._handle_arrow_key(Mock(state=0), 0, 1)

        self.assertEqual(result, "break")
        overlay._move_pointer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
