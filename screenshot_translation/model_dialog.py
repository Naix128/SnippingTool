from __future__ import annotations

import queue
import re
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, Optional

from .dialog import _pointer_work_area
from .local import LocalModelManager, LocalModelSpec
from .models import TranslationCancelled, language_label


class LocalModelManagerDialog(tk.Toplevel):
    WIDTH = 860
    HEIGHT = 570
    MIN_WIDTH = 720
    MIN_HEIGHT = 460

    def __init__(
        self,
        parent: tk.Misc,
        manager: LocalModelManager,
        open_folder_func: Callable[[], None],
        on_change: Optional[Callable[[], None]] = None,
        on_close: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(parent)
        self.parent = parent
        self.manager = manager
        self.open_folder_func = open_folder_func
        self.on_change = on_change
        self.on_close_callback = on_close
        self.catalog: tuple[LocalModelSpec, ...] = ()
        self.item_specs: dict[str, LocalModelSpec] = {}
        self.result_queue: queue.SimpleQueue[tuple[int, str, object]] = queue.SimpleQueue()
        self.generation = 0
        self.cancel_event = threading.Event()
        self.poll_job: Optional[str] = None
        self.closed = False
        self.busy = False

        self.source_var = tk.StringVar(value="全部")
        self.target_var = tk.StringVar(value="全部")
        self.status_var = tk.StringVar(value="正在载入模型目录...")
        self.progress_var = tk.DoubleVar(value=0)

        self.title("本地翻译模型")
        self.geometry(f"{self.WIDTH}x{self.HEIGHT}")
        self.minsize(self.MIN_WIDTH, self.MIN_HEIGHT)
        self.transient(parent)
        self._build_ui()
        self._center()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda event: self.close())
        self.lift()
        self.focus_force()
        self._load_catalog(refresh=False)

    def _build_ui(self) -> None:
        shell = ttk.Frame(self, padding=(16, 14))
        shell.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(shell)
        header.pack(fill=tk.X, pady=(0, 12))
        ttk.Label(
            header,
            text="本地翻译模型",
            font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(side=tk.LEFT)
        ttk.Label(
            header,
            text="OPUS-MT / Argos",
            foreground="#667685",
        ).pack(side=tk.LEFT, padx=(12, 0))
        self.refresh_button = ttk.Button(
            header,
            text="刷新目录",
            command=lambda: self._load_catalog(refresh=True),
        )
        self.refresh_button.pack(side=tk.RIGHT)

        filters = ttk.Frame(shell)
        filters.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(filters, text="源语言").pack(side=tk.LEFT)
        self.source_combo = ttk.Combobox(
            filters,
            textvariable=self.source_var,
            values=("全部",),
            state="readonly",
            width=22,
        )
        self.source_combo.pack(side=tk.LEFT, padx=(8, 18))
        ttk.Label(filters, text="目标语言").pack(side=tk.LEFT)
        self.target_combo = ttk.Combobox(
            filters,
            textvariable=self.target_var,
            values=("全部",),
            state="readonly",
            width=22,
        )
        self.target_combo.pack(side=tk.LEFT, padx=(8, 0))
        self.source_combo.bind("<<ComboboxSelected>>", self._apply_filters)
        self.target_combo.bind("<<ComboboxSelected>>", self._apply_filters)

        table_frame = ttk.Frame(shell)
        table_frame.pack(fill=tk.BOTH, expand=True)
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.table = ttk.Treeview(
            table_frame,
            columns=("source", "target", "version", "size", "status"),
            show="headings",
            selectmode="extended",
            height=14,
        )
        for column, title in (
            ("source", "源语言"),
            ("target", "目标语言"),
            ("version", "版本"),
            ("size", "下载大小"),
            ("status", "状态"),
        ):
            self.table.heading(column, text=title)
        self.table.column("source", width=180, anchor=tk.W, stretch=True)
        self.table.column("target", width=180, anchor=tk.W, stretch=True)
        self.table.column("version", width=80, anchor=tk.CENTER, stretch=False)
        self.table.column("size", width=110, anchor=tk.CENTER, stretch=False)
        self.table.column("status", width=100, anchor=tk.CENTER, stretch=False)
        self.table.tag_configure("installed", foreground="#16835A")
        self.table.tag_configure("available", foreground="#344554")
        scrollbar = ttk.Scrollbar(
            table_frame,
            orient=tk.VERTICAL,
            command=self.table.yview,
        )
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.grid(row=0, column=0, sticky=tk.NSEW)
        scrollbar.grid(row=0, column=1, sticky=tk.NS)
        self.table.bind("<Double-Button-1>", lambda event: self._download_selected())
        self.table.bind("<Control-a>", self._select_all_visible)
        self.table.bind("<Return>", lambda event: self._download_selected())
        self.table.bind("<Delete>", lambda event: self._remove_selected())

        progress_row = ttk.Frame(shell)
        progress_row.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(
            progress_row,
            textvariable=self.status_var,
            foreground="#667685",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.progress = ttk.Progressbar(
            progress_row,
            variable=self.progress_var,
            maximum=100,
            length=220,
        )
        self.progress.pack(side=tk.RIGHT)

        actions = ttk.Frame(shell)
        actions.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(
            actions,
            text="打开模型目录",
            command=self.open_folder_func,
        ).pack(side=tk.LEFT)
        self.remove_button = ttk.Button(
            actions,
            text="删除所选",
            command=self._remove_selected,
        )
        self.remove_button.pack(side=tk.LEFT, padx=(8, 0))
        self.cancel_button = ttk.Button(
            actions,
            text="取消任务",
            state="disabled",
            command=self._cancel_operation,
        )
        self.cancel_button.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(actions, text="关闭", command=self.close).pack(side=tk.RIGHT)
        self.download_button = ttk.Button(
            actions,
            text="下载所选",
            command=self._download_selected,
        )
        self.download_button.pack(side=tk.RIGHT, padx=(0, 8))

    def _load_catalog(self, refresh: bool) -> None:
        if self.busy:
            return

        def load(progress, cancel_event):
            return self.manager.catalog(refresh, progress, cancel_event)

        self._run_operation("正在载入模型目录...", load, "catalog")

    def _download_selected(self) -> None:
        if self.busy:
            return
        selected = self._selected_specs()
        pending = [
            spec
            for spec in selected
            if not self.manager.is_installed(spec.source_language, spec.target_language)
        ]
        if not pending:
            self.status_var.set("请选择尚未安装的模型。")
            return
        known_size = sum(spec.download_size for spec in pending if spec.download_size)
        size_text = (
            f"，已知大小约 {known_size / (1024 * 1024):.0f} MB"
            if known_size
            else ""
        )
        if not messagebox.askokcancel(
            "下载本地翻译模型",
            f"将下载 {len(pending)} 个模型{size_text}。下载后可完全离线使用。",
            parent=self,
        ):
            return

        def download(progress, cancel_event):
            installed = []
            for index, spec in enumerate(pending, start=1):
                progress(
                    f"正在处理 {self._language_text(spec.source_language)} → "
                    f"{self._language_text(spec.target_language)} "
                    f"({index}/{len(pending)})"
                )
                self.manager.ensure_spec(spec, progress, cancel_event)
                installed.append(spec)
            return tuple(installed)

        self._run_operation("正在准备下载...", download, "download")

    def _remove_selected(self) -> None:
        if self.busy:
            return
        selected = [
            spec
            for spec in self._selected_specs()
            if self.manager.is_installed(spec.source_language, spec.target_language)
        ]
        if not selected:
            self.status_var.set("请选择已经安装的模型。")
            return
        if not messagebox.askyesno(
            "删除本地翻译模型",
            f"确定删除选中的 {len(selected)} 个模型吗？",
            parent=self,
        ):
            return
        try:
            for spec in selected:
                self.manager.remove(spec.source_language, spec.target_language)
        except OSError as exc:
            messagebox.showerror("删除模型失败", str(exc), parent=self)
            return
        self._apply_filters()
        self.status_var.set(f"已删除 {len(selected)} 个模型。")
        if self.on_change:
            self.on_change()

    def _run_operation(self, message: str, function, result_kind: str) -> None:
        self.generation += 1
        generation = self.generation
        self.cancel_event.set()
        self.cancel_event = threading.Event()
        cancel_event = self.cancel_event
        self.status_var.set(message)
        self.progress_var.set(0)
        self._set_busy(True)

        def progress(value: str) -> None:
            self.result_queue.put((generation, "progress", value))

        def run() -> None:
            try:
                value = function(progress, cancel_event)
            except Exception as exc:
                self.result_queue.put((generation, "error", exc))
            else:
                self.result_queue.put((generation, result_kind, value))

        threading.Thread(
            target=run,
            name="translation-model-manager",
            daemon=True,
        ).start()
        self.poll_job = self.after(60, self._poll_results)

    def _poll_results(self) -> None:
        self.poll_job = None
        if self.closed:
            return
        terminal = False
        while True:
            try:
                generation, kind, value = self.result_queue.get_nowait()
            except queue.Empty:
                break
            if generation != self.generation:
                continue
            if kind == "progress":
                self._show_progress(str(value))
                continue
            terminal = True
            self._handle_result(kind, value)
        if not terminal and self.busy:
            self.poll_job = self.after(60, self._poll_results)

    def _handle_result(self, kind: str, value: object) -> None:
        self._set_busy(False)
        if kind == "catalog":
            self.catalog = tuple(value) if isinstance(value, tuple) else ()
            self._update_filter_values()
            self._apply_filters()
            if self.manager.last_catalog_error:
                self.status_var.set(
                    f"目录刷新失败，当前显示 {len(self.catalog)} 个缓存/内置模型。"
                )
            else:
                self.status_var.set(f"已载入 {len(self.catalog)} 个模型。")
            return
        if kind == "download":
            installed = tuple(value) if isinstance(value, tuple) else ()
            self._apply_filters()
            self.progress_var.set(100)
            self.status_var.set(f"已安装 {len(installed)} 个模型。")
            if self.on_change:
                self.on_change()
            return
        if isinstance(value, TranslationCancelled):
            self.status_var.set("任务已取消。")
            return
        self.status_var.set("操作失败。")
        messagebox.showerror("模型操作失败", str(value), parent=self)

    def _show_progress(self, message: str) -> None:
        self.status_var.set(message)
        match = re.search(r"(\d{1,3})%", message)
        if match:
            self.progress_var.set(max(0, min(100, int(match.group(1)))))

    def _update_filter_values(self) -> None:
        source_codes = sorted({spec.source_language for spec in self.catalog})
        target_codes = sorted({spec.target_language for spec in self.catalog})
        self.source_combo.configure(
            values=("全部", *(self._language_option(code) for code in source_codes))
        )
        self.target_combo.configure(
            values=("全部", *(self._language_option(code) for code in target_codes))
        )

    def _apply_filters(self, event: Optional[tk.Event] = None) -> None:
        del event
        source = self._code_from_option(self.source_var.get())
        target = self._code_from_option(self.target_var.get())
        selected_pairs = {
            (spec.source_language, spec.target_language)
            for spec in self._selected_specs()
        }
        self.table.delete(*self.table.get_children())
        self.item_specs.clear()
        for index, spec in enumerate(self.catalog):
            if source and spec.source_language != source:
                continue
            if target and spec.target_language != target:
                continue
            installed = self.manager.is_installed(
                spec.source_language,
                spec.target_language,
            )
            size = spec.download_size or (
                self.manager.installed_size(
                    spec.source_language,
                    spec.target_language,
                )
                if installed
                else 0
            )
            item_id = f"model_{index}"
            self.item_specs[item_id] = spec
            self.table.insert(
                "",
                tk.END,
                iid=item_id,
                values=(
                    self._language_text(spec.source_language),
                    self._language_text(spec.target_language),
                    spec.package_version or "-",
                    f"{size / (1024 * 1024):.1f} MB" if size else "下载时获取",
                    "已安装" if installed else "可下载",
                ),
                tags=("installed" if installed else "available",),
            )
            if (spec.source_language, spec.target_language) in selected_pairs:
                self.table.selection_add(item_id)

    def _selected_specs(self) -> list[LocalModelSpec]:
        return [
            self.item_specs[item]
            for item in self.table.selection()
            if item in self.item_specs
        ]

    def _select_all_visible(self, event: Optional[tk.Event] = None) -> str:
        del event
        children = self.table.get_children()
        if children:
            self.table.selection_set(children)
        return "break"

    def _cancel_operation(self) -> None:
        if not self.busy:
            return
        self.cancel_event.set()
        self.status_var.set("正在取消任务...")

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.refresh_button.configure(state=state)
        self.download_button.configure(state=state)
        self.remove_button.configure(state=state)
        self.cancel_button.configure(state="normal" if busy else "disabled")

    @staticmethod
    def _language_text(code: str) -> str:
        return f"{language_label(code)} ({code})"

    @classmethod
    def _language_option(cls, code: str) -> str:
        return cls._language_text(code)

    @staticmethod
    def _code_from_option(value: str) -> str:
        if value == "全部":
            return ""
        match = re.search(r"\(([a-z-]{2,8})\)$", value)
        return match.group(1) if match else ""

    def _center(self) -> None:
        self.update_idletasks()
        left, top, right, bottom = _pointer_work_area(self.parent)
        width = min(self.WIDTH, max(1, right - left))
        height = min(self.HEIGHT, max(1, bottom - top))
        x = max(left, min(self.parent.winfo_pointerx() - width // 2, right - width))
        y = max(top, min(self.parent.winfo_pointery() - height // 2, bottom - height))
        self.geometry(f"{width}x{height}+{x}+{y}")

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.cancel_event.set()
        self.generation += 1
        if self.poll_job:
            try:
                self.after_cancel(self.poll_job)
            except tk.TclError:
                pass
            self.poll_job = None
        self.destroy()
        if self.on_close_callback:
            self.on_close_callback()
