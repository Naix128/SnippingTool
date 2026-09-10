import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from screenshot_translation.local import (
    LocalCTranslateProvider,
    LocalModelManager,
    LocalModelSpec,
)
from screenshot_translation.models import TranslationError


class LocalModelManagerTests(unittest.TestCase):
    @staticmethod
    def _model_archive(source: str = "en", target: str = "zh") -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            root = f"translate-{source}_{target}-test"
            archive.writestr(
                f"{root}/metadata.json",
                json.dumps({"from_code": source, "to_code": target}),
            )
            archive.writestr(f"{root}/sentencepiece.model", b"tokenizer")
            archive.writestr(f"{root}/model/model.bin", b"weights")
            archive.writestr(f"{root}/model/config.json", b"{}")
        return output.getvalue()

    def test_verified_archive_is_installed_in_pair_directory(self) -> None:
        archive_data = self._model_archive()
        spec = LocalModelSpec(
            "en",
            "zh",
            "model.argosmodel",
            hashlib.sha256(archive_data).hexdigest(),
            len(archive_data),
            ("https://example.invalid/model",),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "model.argosmodel"
            archive_path.write_bytes(archive_data)
            manager = LocalModelManager(root)
            destination = root / "en_zh"

            manager._verify_archive(archive_path, spec)
            installed = manager._install_archive(archive_path, destination, spec)

            self.assertEqual(installed, destination)
            self.assertTrue(manager.is_installed("en", "zh"))

    def test_ensure_spec_downloads_and_installs_selected_model(self) -> None:
        archive_data = self._model_archive("ja", "en")

        class Response(io.BytesIO):
            headers = {"Content-Length": str(len(archive_data))}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        requests = []

        def open_model(request, timeout):
            requests.append((request.full_url, timeout))
            return Response(archive_data)

        spec = LocalModelSpec(
            "ja",
            "en",
            "ja_en.argosmodel",
            hashlib.sha256(archive_data).hexdigest(),
            len(archive_data),
            ("https://example.test/ja_en.argosmodel",),
        )
        with tempfile.TemporaryDirectory() as temporary:
            manager = LocalModelManager(Path(temporary), opener=open_model)
            installed = manager.ensure_spec(spec)

            self.assertTrue(manager.is_installed("ja", "en"))
            self.assertEqual(installed, Path(temporary) / "ja_en")
        self.assertEqual(requests[0][0], spec.urls[0])

    def test_checksum_mismatch_is_rejected(self) -> None:
        spec = LocalModelSpec("en", "zh", "bad", "0" * 64, 3, ())
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "model"
            path.write_bytes(b"bad")
            with self.assertRaisesRegex(TranslationError, "校验失败"):
                LocalModelManager._verify_archive(path, spec)

    def test_catalog_keeps_latest_version_and_plans_english_pivot(self) -> None:
        payload = [
            {
                "from_code": "ja",
                "to_code": "en",
                "from_name": "Japanese",
                "to_name": "English",
                "package_version": "1.0",
                "links": ["https://example.test/translate-ja_en-1_0.argosmodel"],
            },
            {
                "from_code": "ja",
                "to_code": "en",
                "from_name": "Japanese",
                "to_name": "English",
                "package_version": "1.2",
                "links": ["https://example.test/translate-ja_en-1_2.argosmodel"],
            },
            {
                "from_code": "en",
                "to_code": "fr",
                "from_name": "English",
                "to_name": "French",
                "package_version": "1.5",
                "links": ["https://example.test/translate-en_fr-1_5.argosmodel"],
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            manager = LocalModelManager(Path(temporary))
            manager._catalog = manager._parse_catalog(
                {
                    "index": payload,
                    "files": [
                        {
                            "type": "file",
                            "path": "translate-en_fr-1_5.argosmodel",
                            "size": 65_000_000,
                            "lfs": {"oid": "a" * 64},
                        }
                    ],
                }
            )
            route = manager.resolve_route("ja", "fr")

        self.assertEqual(
            [(spec.source_language, spec.target_language) for spec in route],
            [("ja", "en"), ("en", "fr")],
        )
        self.assertEqual(route[0].package_version, "1.2")
        self.assertEqual(route[1].download_size, 65_000_000)
        self.assertEqual(route[1].sha256, "a" * 64)

    def test_archive_without_published_checksum_still_requires_valid_zip(self) -> None:
        archive_data = self._model_archive("ja", "en")
        spec = LocalModelSpec("ja", "en", "model.argosmodel", "", 0, ())
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "model.argosmodel"
            path.write_bytes(archive_data)
            LocalModelManager._verify_archive(path, spec)

    def test_archive_path_traversal_is_rejected(self) -> None:
        for unsafe_name in ("../outside.txt", "..\\outside.txt", "C:/outside.txt"):
            with self.subTest(unsafe_name=unsafe_name):
                output = io.BytesIO()
                with zipfile.ZipFile(output, "w") as archive:
                    archive.writestr(unsafe_name, "unsafe")
                output.seek(0)
                with tempfile.TemporaryDirectory() as temporary:
                    with zipfile.ZipFile(output) as archive:
                        member = archive.infolist()[0]
                        with self.assertRaisesRegex(TranslationError, "不安全路径"):
                            LocalModelManager._extract_member(
                                archive,
                                member,
                                Path(temporary),
                            )


class FakeModelManager:
    def resolve_route(self, source: str, target: str):
        if (source, target) != ("en", "zh"):
            raise TranslationError("unsupported")
        return (LocalModelSpec("en", "zh", "fake", "", 0, ()),)

    def ensure_spec(self, *args, **kwargs) -> Path:
        return Path("fake-model")


class FakeTokenizer:
    def encode(self, value: str, out_type=str):
        del out_type
        return value.split()

    def decode_pieces(self, pieces):
        return " ".join(pieces)


class FakeTranslator:
    def translate_batch(self, batches, **kwargs):
        del kwargs
        return [SimpleNamespace(hypotheses=[["译", "文"]]) for _ in batches]


class LocalCTranslateProviderTests(unittest.TestCase):
    def test_detects_english_and_chinese_for_supported_targets(self) -> None:
        self.assertEqual(
            LocalCTranslateProvider.detect_source_language("Hello world", "zh"),
            "en",
        )
        self.assertEqual(
            LocalCTranslateProvider.detect_source_language("你好，世界", "en"),
            "zh",
        )

    def test_detects_japanese_script(self) -> None:
        self.assertEqual(
            LocalCTranslateProvider.detect_source_language("こんにちは", "zh"),
            "ja",
        )

    @patch.object(LocalCTranslateProvider, "is_available", return_value=True)
    def test_translates_each_ocr_line_without_cloud_api(self, available) -> None:
        del available
        provider = LocalCTranslateProvider(FakeModelManager())
        provider._engines[("en", "zh")] = (FakeTranslator(), FakeTokenizer())

        translated, source, request_ids = provider.translate_texts(
            ["Save image", "Open settings"],
            "zh",
        )

        self.assertEqual(source, "en")
        self.assertEqual(translated, ["译文", "译文"])
        self.assertEqual(request_ids, [])


if __name__ == "__main__":
    unittest.main()
