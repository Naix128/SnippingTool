import unittest
from types import SimpleNamespace
from unittest.mock import patch

import screenshot_app as app


class HotkeyCodecTests(unittest.TestCase):
    def test_normalizes_and_converts_combination(self) -> None:
        value = app.HotkeyCodec.normalize("shift + ctrl + a")

        self.assertEqual(value, "Ctrl+Shift+A")
        self.assertEqual(app.HotkeyCodec.to_windows(value), (0x0006, ord("A")))
        self.assertEqual(app.HotkeyCodec.to_tk_sequence(value), "<Control-Shift-A>")

    def test_supports_function_key_without_modifier(self) -> None:
        self.assertEqual(app.HotkeyCodec.to_windows("F1"), (0, 0x70))
        self.assertEqual(app.HotkeyCodec.to_tk_sequence("F1"), "<F1>")

    def test_captures_tk_event(self) -> None:
        event = SimpleNamespace(keysym="F3", state=0x0001)

        with patch.object(app.platform, "system", return_value="Linux"):
            value = app.HotkeyCodec.from_tk_event(event)

        self.assertEqual(value, "Shift+F3")

    def test_windows_ctrl_does_not_gain_false_alt_from_tk_state(self) -> None:
        event = SimpleNamespace(keysym="2", state=0x000C)

        with patch.object(app.platform, "system", return_value="Windows"), patch.object(
            app.HotkeyCodec,
            "_windows_modifiers",
            return_value=["Ctrl"],
        ):
            value = app.HotkeyCodec.from_tk_event(event)

        self.assertEqual(value, "Ctrl+2")

    def test_rejects_duplicate_shortcuts(self) -> None:
        values = app.default_hotkey_map()
        values["full"] = values["region"]

        with self.assertRaisesRegex(ValueError, "Ctrl\\+Shift\\+A"):
            app.HotkeyCodec.validate_mapping(values)

    def test_rejects_unsafe_global_letter(self) -> None:
        values = app.default_hotkey_map()
        values["region"] = "A"

        with self.assertRaises(ValueError):
            app.HotkeyCodec.validate_mapping(values)

    def test_empty_value_disables_action_and_survives_merge(self) -> None:
        merged = app.HotkeyCodec.merge_with_defaults({"paste": ""})

        self.assertEqual(merged["paste"], "")
        self.assertEqual(merged["region"], "Ctrl+Shift+A")

    def test_default_mapping_contains_every_registered_action(self) -> None:
        values = app.HotkeyCodec.validate_mapping(app.default_hotkey_map())

        self.assertEqual(
            set(values),
            {definition.identifier for definition in app.HOTKEY_ACTIONS},
        )

    def test_ocr_shortcut_is_connected_to_current_image_action(self) -> None:
        subject = app.ScreenshotApp.__new__(app.ScreenshotApp)

        action = subject._shortcut_actions()["ocr_current"]

        self.assertIs(action.__func__, app.ScreenshotApp.extract_text_from_current)


class HotkeyRegistrationStatusTests(unittest.TestCase):
    def test_reports_registered_occupied_disabled_and_window_only(self) -> None:
        subject = app.ScreenshotApp.__new__(app.ScreenshotApp)
        subject.app_config = app.PersistedAppConfig()
        subject.app_config.hotkeys["paste"] = ""
        subject.global_hotkey_manager = SimpleNamespace(
            active_ids={101},
            failed_names=["全屏截图"],
        )

        statuses = subject._hotkey_registration_statuses()

        self.assertEqual(statuses["region"], "已注册")
        self.assertEqual(statuses["full"], "被占用")
        self.assertEqual(statuses["paste"], "未设置")
        self.assertEqual(statuses["pin_current"], "窗口内")


if __name__ == "__main__":
    unittest.main()
