import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import screenshot_app as app


class OutputNamePolicyTests(unittest.TestCase):
    def test_renders_supported_date_tokens(self) -> None:
        moment = datetime(2026, 9, 7, 18, 5, 9)

        stem = app.OutputNamePolicy.render_stem(
            "Shot_{yyyy}-{MM}-{dd}_{HH}-{mm}-{ss}",
            moment,
        )

        self.assertEqual(stem, "Shot_2026-09-07_18-05-09")

    def test_removes_extension_and_invalid_filename_characters(self) -> None:
        stem = app.OutputNamePolicy.render_stem("capture:part?.png")

        self.assertEqual(stem, "capture_part_")

    def test_next_target_uses_selected_format(self) -> None:
        target = app.OutputNamePolicy.next_target(
            Path("test") / "not-created",
            "Capture_{yyyy}",
            "jpeg",
            datetime(2026, 1, 2, 3, 4, 5),
        )

        self.assertEqual(target.name, "Capture_2026.jpg")


class ScreenshotManagerOutputTests(unittest.TestCase):
    def test_explicit_jpeg_target_uses_quality_setting(self) -> None:
        settings = app.ScreenshotSettings(
            output_dir=Path("test"),
            image_format="PNG",
            image_quality=87,
        )
        manager = app.ScreenshotManager(settings)
        image = Mock()
        converted = Mock()
        image.convert.return_value = converted
        target = Path("test") / "mock-output.jpg"

        result = manager.save(image, target)

        self.assertEqual(result, target)
        converted.save.assert_called_once_with(
            target,
            "JPEG",
            quality=87,
            optimize=True,
        )

    def test_automatic_save_uses_configured_pattern_and_extension(self) -> None:
        settings = app.ScreenshotSettings(
            output_dir=Path("test"),
            image_format="JPEG",
            image_quality=90,
            filename_pattern="Auto_{yyyy}",
        )
        manager = app.ScreenshotManager(settings)
        image = Mock()
        image.convert.return_value = Mock()

        target = manager.save(image)

        self.assertEqual(target.parent, Path("test"))
        self.assertTrue(target.name.startswith("Auto_"))
        self.assertEqual(target.suffix, ".jpg")


class PreferencesFeatureDefaultsTests(unittest.TestCase):
    def test_new_preferences_have_backward_compatible_defaults(self) -> None:
        config = app.PersistedAppConfig()

        self.assertEqual(config.image_format, "PNG")
        self.assertEqual(config.capture_mask_opacity, 55)
        self.assertEqual(config.capture_border_width, 2)
        self.assertTrue(config.capture_show_handles)
        self.assertTrue(config.capture_show_magnifier)
        self.assertFalse(config.capture_show_crosshair)
        self.assertFalse(config.launch_at_startup)
        self.assertEqual(config.pin_max_size, 12000)
        self.assertEqual(
            (config.pin_thumbnail_width, config.pin_thumbnail_height),
            (180, 120),
        )
        self.assertEqual(config.translation_mode, "local")
        self.assertEqual(config.translation_source, "auto")

    def test_legacy_smart_translation_default_migrates_to_local(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "settings.json"
            config_path.write_text(
                json.dumps({"translation_mode": "smart"}),
                encoding="utf-8",
            )
            with patch.object(app.ConfigStore, "CONFIG_FILE", config_path):
                config = app.ConfigStore.load()

        self.assertEqual(config.translation_mode, "local")

    def test_invalid_theme_color_falls_back_to_project_accent(self) -> None:
        self.assertEqual(
            app.AppTheme.normalize_accent("not-a-color"),
            app.AppTheme.DEFAULT_ACCENT,
        )

    def test_capture_options_map_all_visual_preferences(self) -> None:
        subject = app.ScreenshotApp.__new__(app.ScreenshotApp)
        subject.app_config = app.PersistedAppConfig(
            accent_color="#2563EB",
            capture_mask_opacity=70,
            capture_border_width=4,
            capture_show_handles=False,
            capture_show_crosshair=True,
            capture_show_magnifier=False,
            magnifier_zoom_index=5,
        )

        self.assertEqual(
            subject._capture_overlay_display_options(),
            {
                "accent_color": "#2563EB",
                "mask_opacity": 70,
                "border_width": 4,
                "show_handles": False,
                "show_crosshair": True,
                "show_magnifier": False,
                "magnifier_zoom_index": 5,
            },
        )

    @patch.object(app.sys, "frozen", False, create=True)
    def test_source_startup_command_quotes_python_and_script(self) -> None:
        command = app.WindowsStartupManager.command()

        self.assertIn('" "', command)
        self.assertTrue(command.endswith('screenshot_app.py"'))


if __name__ == "__main__":
    unittest.main()
