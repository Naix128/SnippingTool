import ctypes
import ctypes.wintypes
import platform
import time
import unittest
from unittest.mock import Mock

import tkinter as tk
from PIL import Image

import screenshot_app as app
from screenshot_translation.ocr import DetectedTextLine, RapidOcrUnavailable


def sample_document() -> app.OcrDocument:
    return app.OcrDocument(
        "图片文字\nHello OCR",
        (
            app.OcrLineResult(
                "图片文字",
                (
                    app.OcrWordResult("图", (10, 10, 30, 35)),
                    app.OcrWordResult("片", (31, 10, 51, 35)),
                    app.OcrWordResult("文", (52, 10, 72, 35)),
                    app.OcrWordResult("字", (73, 10, 93, 35)),
                ),
            ),
            app.OcrLineResult(
                "Hello OCR",
                (
                    app.OcrWordResult("Hello", (10, 50, 60, 75)),
                    app.OcrWordResult("OCR", (70, 50, 110, 75)),
                ),
            ),
        ),
        (200, 100),
    )


class OcrTextFormatterTests(unittest.TestCase):
    def test_removes_spaces_between_chinese_but_preserves_latin_words(self) -> None:
        self.assertEqual(
            app.OcrTextFormatter.normalize_line("图 片 文 字  Hello OCR"),
            "图片文字 Hello OCR",
        )

    def test_line_confidence_averages_known_word_scores(self) -> None:
        line = app.OcrLineResult(
            "AB",
            (
                app.OcrWordResult("A", (0, 0, 10, 10), 0.4),
                app.OcrWordResult("B", (10, 0, 20, 10), 0.8),
            ),
        )
        self.assertAlmostEqual(line.confidence, 0.6)


class WindowsOcrBackendParsingTests(unittest.TestCase):
    def test_parses_single_line_payload_and_maps_coordinates_to_source(self) -> None:
        payload = {
            "text": "图 片",
            "lines": {
                "text": "图 片",
                "words": [
                    {"text": "图", "x": 5, "y": 6, "width": 10, "height": 12},
                    {"text": "片", "x": 16, "y": 6, "width": 10, "height": 12},
                ],
            },
        }

        document = app.WindowsOcrBackend.parse_result(
            payload,
            source_size=(200, 100),
            processed_size=(100, 50),
        )

        self.assertEqual(document.text, "图片")
        self.assertEqual(document.lines[0].words[0].box, (10, 12, 30, 36))
        self.assertEqual(document.lines[0].box, (10, 12, 52, 36))

    def test_large_image_is_scaled_to_windows_ocr_limit(self) -> None:
        backend = app.WindowsOcrBackend()
        image = Image.new("RGB", (11000, 10), "white")

        prepared = backend._prepare_image(image)

        self.assertEqual(prepared.width, backend.MAX_IMAGE_DIMENSION)
        self.assertLessEqual(prepared.height, image.height)


class ConfigurableOcrBackendTests(unittest.TestCase):
    def test_rapid_result_is_converted_to_existing_document(self) -> None:
        rapid = Mock()
        rapid.is_available.return_value = True
        rapid.recognize.return_value = (
            DetectedTextLine("Hello", ((2, 3), (42, 3), (42, 18), (2, 18)), 0.9),
        )
        subject = app.ConfigurableOcrBackend.__new__(app.ConfigurableOcrBackend)
        subject.engine = "auto"
        subject.rapid = rapid
        subject.windows = Mock()

        document = subject.recognize(Image.new("RGB", (100, 50), "white"))

        self.assertEqual(document.text, "Hello")
        self.assertEqual(document.lines[0].box, (2, 3, 42, 18))
        self.assertAlmostEqual(document.lines[0].confidence, 0.9)
        subject.windows.recognize.assert_not_called()

    def test_auto_mode_falls_back_to_windows(self) -> None:
        expected = sample_document()
        subject = app.ConfigurableOcrBackend.__new__(app.ConfigurableOcrBackend)
        subject.engine = "auto"
        subject.rapid = Mock()
        subject.rapid.is_available.return_value = False
        subject.windows = Mock()
        subject.windows.recognize.return_value = expected
        self.assertIs(subject.recognize(Image.new("RGB", (20, 20))), expected)

    def test_explicit_rapid_mode_reports_missing_component(self) -> None:
        subject = app.ConfigurableOcrBackend.__new__(app.ConfigurableOcrBackend)
        subject.engine = "rapid"
        subject.rapid = Mock()
        subject.rapid.is_available.return_value = False
        subject.windows = Mock()
        with self.assertRaises(RapidOcrUnavailable):
            subject.recognize(Image.new("RGB", (20, 20)))

    def test_explicit_japanese_source_uses_matching_windows_ocr_language(self) -> None:
        expected = sample_document()
        with unittest.mock.patch.object(app, "WindowsOcrBackend") as backend_class:
            backend_class.return_value.recognize.return_value = expected
            subject = app.ConfigurableOcrBackend.__new__(app.ConfigurableOcrBackend)

            result = subject.recognize_for_language(
                Image.new("RGB", (20, 20)),
                "ja",
            )

        self.assertIs(result, expected)
        backend_class.assert_called_once_with("ja-JP", strict_language=True)


class OcrSelectionModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = app.OcrSelectionModel(sample_document())

    def test_click_hits_line_and_selects_all_its_words(self) -> None:
        line_index = self.model.line_at((40, 20))

        self.assertEqual(line_index, 0)
        self.assertEqual(len(self.model.line_words(line_index)), 4)

    def test_drag_box_selects_intersecting_words_in_reading_order(self) -> None:
        selected = self.model.words_in_box((25, 5, 80, 40))

        self.assertEqual(
            selected,
            {(0, 0), (0, 1), (0, 2), (0, 3)},
        )
        self.assertEqual(self.model.text_for(selected), "图片文字")

    def test_partial_latin_selection_preserves_word_spacing(self) -> None:
        self.assertEqual(
            self.model.text_for({(1, 0), (1, 1)}),
            "Hello OCR",
        )


class FakeOcrBackend:
    def recognize(self, image: Image.Image) -> app.OcrDocument:
        del image
        return sample_document()


class OcrResultDialogTests(unittest.TestCase):
    def test_close_cancels_startup_callbacks(self) -> None:
        root = tk.Tk()
        root.withdraw()
        dialog = app.OcrResultDialog(
            root,
            Image.new("RGB", (80, 40), "white"),
            backend=FakeOcrBackend(),
        )
        jobs = {dialog.render_job, dialog.recognition_job, dialog.sash_job}
        dialog.close()
        pending = set(root.tk.call("after", "info"))
        self.assertTrue(jobs.isdisjoint(pending))
        root.update()
        root.destroy()

    def test_background_result_populates_editable_text_and_boxes(self) -> None:
        root = tk.Tk()
        root.withdraw()
        dialog = app.OcrResultDialog(
            root,
            Image.new("RGB", (200, 100), "white"),
            backend=FakeOcrBackend(),
        )
        deadline = time.time() + 2
        while dialog.document is None and time.time() < deadline:
            dialog.update()
            time.sleep(0.01)

        self.assertIsNotNone(dialog.document)
        self.assertEqual(dialog.text.get("1.0", "end-1c"), "图片文字\nHello OCR")
        self.assertGreater(len(dialog.canvas.find_withtag("ocr_overlay")), 0)

        if platform.system() == "Windows":
            child = ctypes.wintypes.HWND(dialog.winfo_id())
            user32 = ctypes.windll.user32
            user32.GetAncestor.argtypes = [
                ctypes.wintypes.HWND,
                ctypes.wintypes.UINT,
            ]
            user32.GetAncestor.restype = ctypes.wintypes.HWND
            native_window = user32.GetAncestor(child, 2) or child
            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(native_window, ctypes.byref(rect))
            work_area = app.MonitorWorkArea.at_point(
                ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
            )
            self.assertLessEqual(rect.bottom, work_area.bottom)

        dialog.selected_words = dialog.selection_model.line_words(0)
        dialog.clipboard_clear = Mock()
        dialog.clipboard_append = Mock()
        dialog._copy_selected()
        dialog.clipboard_append.assert_called_once_with("图片文字")

        dialog.text.tag_remove(tk.SEL, "1.0", tk.END)
        dialog.clipboard_append.reset_mock()
        dialog._copy_text_selection()
        dialog.clipboard_append.assert_not_called()

        dialog.text.tag_add(tk.SEL, "2.0", "2.5")
        dialog._copy_text_selection()
        dialog.clipboard_append.assert_called_once_with("Hello")

        dialog.clipboard_append.reset_mock()
        dialog.text_context_index = "2.3"
        dialog._copy_context_line()
        dialog.clipboard_append.assert_called_once_with("Hello OCR")

        dialog.text_menu.tk_popup = Mock()
        event = Mock(x=4, y=4, x_root=100, y_root=100)
        dialog._show_text_menu(event)
        self.assertEqual(str(dialog.text_menu.entrycget("复制", "state")), "normal")
        dialog.text_menu.tk_popup.assert_called_once_with(100, 100)

        dialog.close()
        root.destroy()


if __name__ == "__main__":
    unittest.main()
