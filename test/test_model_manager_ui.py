import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock

from screenshot_translation.local import LocalModelManager, LocalModelSpec
from screenshot_translation.model_dialog import LocalModelManagerDialog


class LocalModelManagerDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.root.withdraw()
        self.manager = LocalModelManager(Path(self.temporary.name))
        specs = (
            LocalModelSpec(
                "en",
                "zh",
                "en_zh.argosmodel",
                "",
                70_000_000,
                ("https://example.test/en_zh",),
                "English",
                "Chinese",
                "1.9",
            ),
            LocalModelSpec(
                "ja",
                "en",
                "ja_en.argosmodel",
                "",
                0,
                ("https://example.test/ja_en",),
                "Japanese",
                "English",
                "1.1",
            ),
        )
        self.manager._catalog = {
            (spec.source_language, spec.target_language): spec for spec in specs
        }
        self.closed = Mock()
        self.dialog = LocalModelManagerDialog(
            self.root,
            self.manager,
            open_folder_func=Mock(),
            on_close=self.closed,
        )
        deadline = time.time() + 2
        while self.dialog.busy and time.time() < deadline:
            self.dialog.update()
            time.sleep(0.01)

    def tearDown(self) -> None:
        try:
            if self.dialog.winfo_exists():
                self.dialog.close()
        except tk.TclError:
            pass
        self.root.destroy()
        self.temporary.cleanup()

    def test_catalog_populates_multi_select_table_and_filters(self) -> None:
        self.assertEqual(len(self.dialog.table.get_children()), 2)
        self.assertEqual(str(self.dialog.table.cget("selectmode")), "extended")
        self.assertIn("日语 (ja)", self.dialog.source_combo.cget("values"))

        self.dialog.source_var.set("日语 (ja)")
        self.dialog._apply_filters()

        rows = self.dialog.table.get_children()
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.dialog.table.item(rows[0], "values")[0], "日语 (ja)")
        self.assertEqual(self.dialog._select_all_visible(), "break")
        self.assertEqual(len(self.dialog.table.selection()), 1)

    def test_close_cancels_polling_and_notifies_owner(self) -> None:
        self.dialog.poll_job = self.dialog.after(5000, lambda: None)
        pending = self.dialog.poll_job

        self.dialog.close()

        self.assertNotIn(pending, set(self.root.tk.call("after", "info")))
        self.closed.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
