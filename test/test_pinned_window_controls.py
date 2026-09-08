import ctypes
import ctypes.wintypes
import platform
import unittest
from unittest.mock import Mock

import tkinter as tk
from PIL import Image

import screenshot_app as app


@unittest.skipUnless(platform.system() == "Windows", "Windows-only window controls")
class PinnedWindowControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.on_close = Mock()
        self.ocr = Mock()
        self.window = app.PinnedImageWindow(
            self.root,
            Image.new("RGB", (120, 80), "white"),
            on_close=self.on_close,
            copy_func=Mock(),
            save_func=Mock(),
            ocr_func=self.ocr,
            position=(80, 80),
        )
        self.window.window.update_idletasks()

    def tearDown(self) -> None:
        try:
            if self.window.window.winfo_exists():
                self.window.close(destroy=True)
        except tk.TclError:
            pass
        self.root.destroy()

    def test_menu_keeps_topmost_but_has_no_click_through_action(self) -> None:
        menu_types = {
            self.window.menu.entrycget(index, "label"): self.window.menu.type(index)
            for index in range(self.window.menu.index("end") + 1)
            if self.window.menu.type(index) != "separator"
        }

        self.assertEqual(menu_types["保持置顶"], "checkbutton")
        self.assertEqual(menu_types["提取文字"], "command")
        self.assertNotIn("鼠标穿透", menu_types)

    def test_extract_text_menu_uses_current_transformed_image(self) -> None:
        index = next(
            index
            for index in range(self.window.menu.index("end") + 1)
            if self.window.menu.type(index) != "separator"
            and self.window.menu.entrycget(index, "label") == "提取文字"
        )

        self.window.menu.invoke(index)
        self.root.update()

        extracted_image = self.ocr.call_args.args[0]
        self.assertEqual(extracted_image.size, (120, 80))

    def test_topmost_state_stays_synchronized(self) -> None:
        self.window._set_topmost(False)

        self.assertFalse(self.window.is_topmost)
        self.assertFalse(self.window.topmost_menu_var.get())

    def test_destroy_from_menu_hides_before_deferred_destroy(self) -> None:
        self.window.menu.invoke(self.window.menu.index("end"))

        self.assertEqual(self.window.window.state(), "withdrawn")
        self.assertTrue(self.window.window.winfo_exists())
        self.on_close.assert_not_called()

        self.root.update()

        self.assertFalse(self.window.window.winfo_exists())
        self.on_close.assert_called_once_with(self.window, True)

    def test_close_is_idempotent(self) -> None:
        self.window.close(destroy=True)
        self.window.close(destroy=True)

        self.on_close.assert_called_once_with(self.window, True)


@unittest.skipUnless(platform.system() == "Windows", "Windows-only tray menu")
class TrayMenuHandleTests(unittest.TestCase):
    def test_popup_menu_apis_accept_pointer_sized_handle(self) -> None:
        user32 = ctypes.windll.user32
        app.SystemTrayController._configure_menu_api(user32)
        menu = user32.CreatePopupMenu()
        self.assertTrue(menu)
        try:
            self.assertTrue(user32.AppendMenuW(menu, 0, 1001, "截图"))
            self.assertTrue(user32.SetMenuDefaultItem(menu, 1001, False))
            self.assertIs(
                user32.SetMenuDefaultItem.argtypes[0],
                ctypes.wintypes.HMENU,
            )
        finally:
            user32.DestroyMenu(menu)

    def test_tray_has_no_click_through_action(self) -> None:
        actions = {
            action
            for entry in app.SystemTrayController.MENU_ITEMS
            if entry is not None
            for _, action, _ in (entry,)
        }

        self.assertNotIn("clear_click_through", actions)


if __name__ == "__main__":
    unittest.main()
