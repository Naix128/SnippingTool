import unittest

import tkinter as tk
from tkinter import ttk

import screenshot_app as app


class PreferencesTabStyleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.dialog = app.PreferencesDialog(
            self.root,
            app.PersistedAppConfig(),
            "ready",
        )
        self.dialog.update_idletasks()

    def tearDown(self) -> None:
        self.dialog.destroy()
        self.root.destroy()

    def test_selected_tabs_keep_padding_and_have_no_focus_box(self) -> None:
        style = ttk.Style(self.dialog)

        for style_name in ("Prefs.TNotebook.Tab", "PrefsSub.TNotebook.Tab"):
            selected_padding = tuple(
                str(value)
                for value in style.lookup(style_name, "padding", ("selected",))
            )
            normal_padding = tuple(
                str(value) for value in style.lookup(style_name, "padding")
            )

            self.assertEqual(selected_padding, normal_padding)
            self.assertNotIn("Notebook.focus", str(style.layout(style_name)))


if __name__ == "__main__":
    unittest.main()
