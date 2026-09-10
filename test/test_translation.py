import base64
import io
import json
import threading
import unittest
import tkinter as tk
import urllib.parse
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image

import screenshot_app as app

from screenshot_translation.models import (
    TranslationCredentials,
    TranslationDocument,
    TranslationError,
    TranslationRegion,
)
from screenshot_translation.providers import CloudImageEncoder, TencentCloudClient, TencentCloudSigner, TencentTranslationProvider
from screenshot_translation.renderer import ImageTilePlanner, LocalTranslationRenderer
from screenshot_translation.service import ImageTranslationService
from screenshot_translation.dialog import ImageTranslationDialog, TranslationCredentialDialog
from screenshot_translation.ocr import RapidOcrAdapter


def encoded_image(size=(16, 12), color="white"):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, "JPEG")
    return base64.b64encode(output.getvalue()).decode("ascii")


class FakeTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, headers, body, timeout):
        self.calls.append((url, headers, body, timeout))
        return self.response


class TencentProtocolTests(unittest.TestCase):
    def test_text_chunks_preserve_all_characters(self):
        chunks = TencentTranslationProvider._text_chunks("x" * 3601)
        self.assertEqual([len(chunk) for chunk in chunks], [1800, 1800, 1])
        self.assertEqual("".join(chunks), "x" * 3601)

    def test_double_url_encoded_image_is_accepted(self):
        data = urllib.parse.quote(urllib.parse.quote(encoded_image(), safe=""), safe="")
        document = TencentTranslationProvider.parse_image_response(
            {"Data": data, "Source": "en", "Target": "zh"},
            request_size=(16, 12),
        )
        self.assertEqual(document.translated_image.size, (16, 12))

    def test_text_translation_sends_every_chunk(self):
        transport = FakeTransport({
            "Response": {"TargetText": "T", "Source": "en", "RequestId": "r"}
        })
        provider = TencentTranslationProvider(
            TranslationCredentials("id", "key"), transport=transport
        )
        provider.text_client.minimum_interval = 0
        translations, source, request_ids = provider.translate_texts(
            ["x" * 3601], "zh"
        )
        lengths = [len(json.loads(call[2])["SourceText"]) for call in transport.calls]
        self.assertEqual(lengths, [1800, 1800, 1])
        self.assertEqual(translations, ["TTT"])
        self.assertEqual(source, "en")
        self.assertEqual(request_ids, ["r", "r", "r"])

    def test_signer_is_deterministic(self):
        headers = TencentCloudSigner.headers(
            TranslationCredentials("AKIDEXAMPLE", "SECRET"),
            "tmt",
            "tmt.tencentcloudapi.com",
            "ImageTranslateLLM",
            "2018-03-21",
            "ap-guangzhou",
            b"{}",
            timestamp=1700000000,
        )
        self.assertEqual(headers["X-TC-Timestamp"], "1700000000")
        self.assertTrue(headers["Authorization"].endswith(
            "Signature=2f995dd8d032bd93ae9b61f3d2f538c96b3a78407c4402fb8fa24bcc366150e9"
        ))

    def test_client_serializes_request_and_parses_response(self):
        transport = FakeTransport({"Response": {"TargetText": "你好"}})
        client = TencentCloudClient(
            TranslationCredentials("id", "key"),
            transport=transport,
        )
        response = client.call("TextTranslate", {"SourceText": "hello"})
        self.assertEqual(response["TargetText"], "你好")
        _, headers, body, _ = transport.calls[0]
        self.assertEqual(headers["X-TC-Action"], "TextTranslate")
        self.assertEqual(json.loads(body)["SourceText"], "hello")

    def test_client_surfaces_service_error(self):
        transport = FakeTransport({
            "Response": {
                "Error": {"Code": "AuthFailure.SignatureFailure", "Message": "bad"},
                "RequestId": "request-1",
            }
        })
        client = TencentCloudClient(
            TranslationCredentials("id", "key"),
            transport=transport,
        )
        with self.assertRaises(TranslationError) as context:
            client.call("TextTranslate", {"SourceText": "hello"})
        self.assertEqual(context.exception.request_id, "request-1")
        self.assertIn("SecretKey", str(context.exception))
        self.assertEqual(len(transport.calls), 1)

    def test_client_retries_transient_network_error(self):
        class FlakyTransport:
            def __init__(self):
                self.calls = 0

            def post(self, url, headers, body, timeout):
                self.calls += 1
                if self.calls == 1:
                    raise TranslationError("temporary", code="NetworkError")
                return {"Response": {"TargetText": "ok"}}

        transport = FlakyTransport()
        delays = []
        client = TencentCloudClient(
            TranslationCredentials("id", "key"),
            transport=transport,
            retry_delays=(0,),
            sleep_func=delays.append,
        )
        self.assertEqual(client.call("TextTranslate", {})["TargetText"], "ok")
        self.assertEqual(transport.calls, 2)
        self.assertEqual(delays, [0.0])


class TranslationModelTests(unittest.TestCase):
    def test_cloud_response_keeps_text_and_scales_regions(self):
        response = {
            "Data": encoded_image((100, 50)),
            "Source": "en",
            "Target": "zh",
            "SourceText": "Hello",
            "TargetText": "你好",
            "RequestId": "request-2",
            "TransDetails": [{
                "BoundingBox": {"X": 20, "Y": 10, "Width": 80, "Height": 30},
                "SourceLineText": "Hello",
                "TargetLineText": "你好",
            }],
        }
        document = TencentTranslationProvider.parse_image_response(
            response, request_size=(200, 100)
        )
        self.assertEqual(document.target_text, "你好")
        self.assertEqual(document.regions[0].box, (10, 5, 50, 20))
        self.assertEqual(document.request_id, "request-2")

    def test_polygon_hit_works_in_both_vertex_orders(self):
        clockwise = TranslationRegion("a", "b", ((1, 1), (9, 1), (9, 9), (1, 9)))
        counterclockwise = TranslationRegion(
            "a", "b", tuple(reversed(clockwise.polygon))
        )
        self.assertTrue(clockwise.contains((5, 5)))
        self.assertTrue(counterclockwise.contains((5, 5)))
        self.assertFalse(clockwise.contains((15, 5)))

    def test_rapidocr_new_output_is_normalized_and_sorted(self):
        class Output:
            boxes = [
                [[20, 30], [50, 30], [50, 42], [20, 42]],
                [[2, 3], [18, 3], [18, 12], [2, 12]],
            ]
            txts = ["second", "first"]
            scores = [0.8, 0.95]

        lines = RapidOcrAdapter.parse_output(Output())
        self.assertEqual([line.text for line in lines], ["first", "second"])
        self.assertEqual(lines[0].box, (2, 3, 18, 12))
        self.assertAlmostEqual(lines[0].confidence, 0.95)

    def test_rapidocr_legacy_output_is_supported(self):
        raw = [
            ([[1, 2], [9, 2], [9, 8], [1, 8]], "hello", 0.7),
        ]
        lines = RapidOcrAdapter.parse_output((raw, {"elapsed": 0.1}))
        self.assertEqual(lines[0].text, "hello")
        self.assertEqual(lines[0].box, (1, 2, 9, 8))


class FakeProvider:
    def __init__(self):
        self.image_calls = 0

    def translate_image(self, image, target, quality, progress, cancel_event):
        self.image_calls += 1
        return TranslationDocument(
            image.copy(), "en", target, "Hello", "你好",
            (TranslationRegion("Hello", "你好", ((2, 2), (30, 2), (30, 18), (2, 18))),),
            request_id=f"image-{self.image_calls}",
        )

    def translate_texts(self, texts, target, source_language, progress, cancel_event):
        return ["你好" for _ in texts], "en", ["text-1"]


class TranslationServiceTests(unittest.TestCase):
    def test_selected_source_language_is_used_for_local_ocr(self):
        line = SimpleNamespace(text="Bonjour", box=(5, 5, 60, 25))
        ocr = Mock()
        ocr.recognize_for_language.return_value = SimpleNamespace(lines=(line,))
        service = ImageTranslationService(FakeProvider(), ocr_backend=ocr)

        service.translate(
            Image.new("RGB", (80, 40), "white"),
            "en",
            mode="local",
            source_language="fr",
        )

        ocr.recognize_for_language.assert_called_once()
        self.assertEqual(ocr.recognize_for_language.call_args.args[1], "fr")
        ocr.recognize.assert_not_called()

    def test_local_mode_uses_ocr_and_never_uploads_image(self):
        provider = FakeProvider()
        line = SimpleNamespace(text="Hello", box=(5, 5, 50, 25))
        ocr = SimpleNamespace(recognize=lambda image: SimpleNamespace(lines=(line,)))

        document = ImageTranslationService(provider, ocr_backend=ocr).translate(
            Image.new("RGB", (80, 40), "white"),
            "zh",
            mode="local",
        )

        self.assertEqual(document.target_text, "你好")
        self.assertEqual(provider.image_calls, 0)

    def test_smart_mode_falls_back_for_unusable_cloud_image(self):
        provider = FakeProvider()
        provider.translate_image = lambda *args: (_ for _ in ()).throw(
            TranslationError("large", code="ImageTooLarge")
        )
        line = SimpleNamespace(text="Hello", box=(5, 5, 50, 25))
        ocr = SimpleNamespace(recognize=lambda image: SimpleNamespace(lines=(line,)))
        messages = []
        document = ImageTranslationService(provider, ocr_backend=ocr).translate(
            Image.new("RGB", (80, 40), "white"),
            "zh",
            mode="smart",
            progress=messages.append,
        )
        self.assertEqual(document.target_text, "你好")
        self.assertTrue(any("仅上传文字" in message for message in messages))

    def test_local_renderer_changes_only_target_image(self):
        source = Image.new("RGB", (120, 50), "white")
        region = TranslationRegion(
            "Hello", "OK", ((10, 10), (100, 10), (100, 40), (10, 40))
        )
        output = LocalTranslationRenderer().render(source, (region,), "en")
        self.assertEqual(source.getpixel((20, 20)), (255, 255, 255))
        self.assertNotEqual(output.tobytes(), source.tobytes())

    def test_cloud_encoder_flattens_transparency(self):
        image = Image.new("RGBA", (20, 20), (255, 0, 0, 0))
        encoded, size, image_format = CloudImageEncoder.encode(image)
        decoded = Image.open(io.BytesIO(base64.b64decode(encoded))).convert("RGB")
        self.assertEqual(size, (20, 20))
        self.assertEqual(image_format, "PNG")
        self.assertEqual(decoded.getpixel((0, 0)), (255, 255, 255))

    def test_cloud_result_is_cached(self):
        provider = FakeProvider()
        service = ImageTranslationService(provider, cache_size=2)
        image = Image.new("RGB", (80, 40), "white")
        first = service.translate(image, "zh", mode="cloud")
        second = service.translate(image, "zh", mode="cloud")
        self.assertIs(first, second)
        self.assertEqual(provider.image_calls, 1)

    def test_privacy_mode_uses_local_ocr_and_text_only_api(self):
        line = SimpleNamespace(text="Hello", box=(5, 5, 50, 25))
        ocr = SimpleNamespace(recognize=lambda image: SimpleNamespace(lines=(line,)))
        service = ImageTranslationService(FakeProvider(), ocr_backend=ocr)
        document = service.translate(
            Image.new("RGB", (80, 40), "white"), "zh", mode="privacy"
        )
        self.assertEqual(document.source_text, "Hello")
        self.assertEqual(document.target_text, "你好")
        self.assertEqual(document.regions[0].box, (5, 5, 50, 25))

    def test_tall_image_tiles_cover_source_without_gaps(self):
        planner = ImageTilePlanner(max_tile_height=1000, search_radius=30)
        boxes = planner.plan(Image.new("RGB", (120, 2600), "white"))
        self.assertGreater(len(boxes), 1)
        self.assertEqual(boxes[0][1], 0)
        self.assertEqual(boxes[-1][3], 2600)
        self.assertTrue(all(left[3] == right[1] for left, right in zip(boxes, boxes[1:])))


class TranslationWindowVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def test_credential_dialog_is_visible_with_hidden_parent(self):
        dialog = TranslationCredentialDialog(self.root)
        dialog.update()
        self.assertEqual(dialog.state(), "normal")
        self.assertEqual(dialog.winfo_viewable(), 1)
        self.assertTrue(bool(dialog.attributes("-topmost")))
        self.assertIs(self.root.grab_current(), dialog)
        dialog._cancel()
        self.root.update_idletasks()
        self.assertIsNone(self.root.grab_current())

    def test_result_dialog_close_cancels_startup_callbacks(self):
        image = Image.new("RGB", (80, 50), "white")
        dialog = ImageTranslationDialog(
            self.root,
            image,
            lambda *args: TranslationDocument(image, "en", "zh", "", "", ()),
            confirm_cloud_func=lambda mode: True,
        )
        jobs = {dialog.render_job, dialog.startup_job, dialog.sash_job}
        dialog.close()
        pending = set(self.root.tk.call("after", "info"))
        self.assertTrue(jobs.isdisjoint(pending))
        self.root.update()

    def test_default_local_mode_never_requests_cloud_confirmation(self):
        image = Image.new("RGB", (80, 50), "white")
        confirm_local = Mock(return_value=False)
        confirm_cloud = Mock(return_value=False)
        dialog = ImageTranslationDialog(
            self.root,
            image,
            lambda *args: TranslationDocument(image, "en", "zh", "", "", ()),
            confirm_cloud_func=confirm_cloud,
            confirm_local_func=confirm_local,
        )
        if dialog.startup_job:
            dialog.after_cancel(dialog.startup_job)
            dialog.startup_job = None

        dialog._start_translation()

        confirm_local.assert_called_once_with("auto", "zh")
        confirm_cloud.assert_not_called()
        self.assertEqual(dialog.status_var.get(), "已取消本地翻译")
        dialog.close()

    def test_cancel_translation_stops_result_polling(self):
        image = Image.new("RGB", (80, 50), "white")
        dialog = ImageTranslationDialog(
            self.root,
            image,
            lambda *args: TranslationDocument(image, "en", "zh", "", "", ()),
            confirm_local_func=lambda source, target: False,
        )
        if dialog.startup_job:
            dialog.after_cancel(dialog.startup_job)
            dialog.startup_job = None
        dialog.poll_job = dialog.after(5000, lambda: None)
        pending_job = dialog.poll_job

        dialog._cancel_task()

        self.assertIsNone(dialog.poll_job)
        self.assertNotIn(pending_job, set(self.root.tk.call("after", "info")))
        dialog.close()

    def test_translated_and_source_text_support_character_copy_menu(self):
        image = Image.new("RGB", (120, 50), "white")
        document = TranslationDocument(
            image,
            "en",
            "zh",
            "Hello world",
            "你好世界",
            (
                TranslationRegion(
                    "Hello world",
                    "你好世界",
                    ((2, 2), (100, 2), (100, 30), (2, 30)),
                ),
            ),
        )
        dialog = ImageTranslationDialog(
            self.root,
            image,
            lambda *args: document,
            confirm_local_func=lambda source, target: False,
        )
        if dialog.startup_job:
            dialog.after_cancel(dialog.startup_job)
            dialog.startup_job = None
        dialog._set_document(document)
        dialog.clipboard_clear = Mock()
        dialog.clipboard_append = Mock()

        dialog.target_text.tag_add(tk.SEL, "1.0", "1.2")
        dialog._copy_widget_selection(dialog.target_text)
        dialog.clipboard_append.assert_called_once_with("你好")

        dialog.clipboard_append.reset_mock()
        dialog.source_text.tag_add(tk.SEL, "1.0", "1.5")
        dialog._copy_widget_selection(dialog.source_text)
        dialog.clipboard_append.assert_called_once_with("Hello")

        dialog.clipboard_append.reset_mock()
        dialog.text_context_widget = dialog.target_text
        dialog.text_context_index = "1.1"
        dialog._copy_context_line()
        dialog.clipboard_append.assert_called_once_with("你好世界")

        dialog.text_menu.tk_popup = Mock()
        event = Mock(x=5, y=5, x_root=120, y_root=140)
        dialog._show_text_menu(dialog.target_text, event)
        self.assertEqual(str(dialog.text_menu.entrycget("复制", "state")), "normal")
        dialog.text_menu.tk_popup.assert_called_once_with(120, 140)
        dialog.close()


class TranslationRoutingTests(unittest.TestCase):
    def test_app_routes_local_mode_without_creating_tencent_service(self):
        subject = app.ScreenshotApp.__new__(app.ScreenshotApp)
        local_service = Mock()
        cloud_service = Mock()
        subject._local_translation_service_for_current_config = Mock(
            return_value=local_service
        )
        subject._translation_service_for_current_config = Mock(
            return_value=cloud_service
        )

        subject._perform_image_translation(
            Image.new("RGB", (10, 10), "white"),
            "auto",
            "zh",
            "local",
            0,
            Mock(),
            threading.Event(),
        )

        local_service.translate.assert_called_once()
        self.assertEqual(
            local_service.translate.call_args.kwargs["source_language"],
            "auto",
        )
        cloud_service.translate.assert_not_called()

    def test_app_maps_tencent_smart_to_existing_smart_service_mode(self):
        subject = app.ScreenshotApp.__new__(app.ScreenshotApp)
        cloud_service = Mock()
        subject._translation_service_for_current_config = Mock(
            return_value=cloud_service
        )

        subject._perform_image_translation(
            Image.new("RGB", (10, 10), "white"),
            "auto",
            "zh",
            "tencent_smart",
            0,
            Mock(),
            threading.Event(),
        )

        self.assertEqual(cloud_service.translate.call_args.args[2], "smart")
