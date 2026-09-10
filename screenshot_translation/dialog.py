from __future__ import annotations

import ctypes
import ctypes.wintypes
import math
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from PIL import Image, ImageTk

from .models import (
    LANGUAGE_LABELS,
    TranslationCancelled,
    TranslationCredentials,
    TranslationDocument,
    language_code_for_label,
    language_label,
)

TranslateFunction = Callable[
    [Image.Image, str, str, str, int, Callable[[str], None], threading.Event],
    TranslationDocument,
]


def _parent_is_visible(parent: tk.Misc) -> bool:
    try:
        return bool(parent.winfo_viewable()) and str(parent.state()) != "withdrawn"
    except (AttributeError, tk.TclError):
        return False


def _show_modal_window(window: tk.Toplevel, parent: tk.Misc) -> None:
    if _parent_is_visible(parent):
        window.transient(parent)
    else:
        window.attributes("-topmost", True)
    window.deiconify()
    window.lift()
    window.update_idletasks()
    try:
        window.wait_visibility()
    except tk.TclError:
        return

    window.grab_set()
    window.focus_force()


def _pointer_work_area(parent: tk.Misc) -> tuple[int, int, int, int]:
    pointer_x = int(parent.winfo_pointerx())
    pointer_y = int(parent.winfo_pointery())
    if os.name == "nt":
        try:
            class MonitorInfo(ctypes.Structure):
                _fields_ = (
                    ("cbSize", ctypes.wintypes.DWORD),
                    ("rcMonitor", ctypes.wintypes.RECT),
                    ("rcWork", ctypes.wintypes.RECT),
                    ("dwFlags", ctypes.wintypes.DWORD),
                )

            user32 = ctypes.windll.user32
            user32.MonitorFromPoint.argtypes = (
                ctypes.wintypes.POINT,
                ctypes.wintypes.DWORD,
            )
            user32.MonitorFromPoint.restype = ctypes.wintypes.HANDLE
            user32.GetMonitorInfoW.argtypes = (
                ctypes.wintypes.HANDLE,
                ctypes.POINTER(MonitorInfo),
            )
            user32.GetMonitorInfoW.restype = ctypes.wintypes.BOOL
            monitor = user32.MonitorFromPoint(
                ctypes.wintypes.POINT(pointer_x, pointer_y),
                2,
            )
            info = MonitorInfo()
            info.cbSize = ctypes.sizeof(info)
            if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                return (
                    int(info.rcWork.left),
                    int(info.rcWork.top),
                    int(info.rcWork.right),
                    int(info.rcWork.bottom),
                )
        except (AttributeError, OSError, TypeError, ValueError):
            pass

    left = int(parent.winfo_vrootx())
    top = int(parent.winfo_vrooty())
    width = max(1, int(parent.winfo_vrootwidth() or parent.winfo_screenwidth()))
    height = max(1, int(parent.winfo_vrootheight() or parent.winfo_screenheight()))
    return left, top, left + width, top + height


class TranslationCredentialDialog(tk.Toplevel):
    """Collects cloud credentials without writing them to app settings."""

    def __init__(
        self,
        parent: tk.Misc,
        current: Optional[TranslationCredentials] = None,
    ) -> None:
        super().__init__(parent)
        self.title("腾讯云翻译密钥")
        self.geometry("500x285")
        self.resizable(False, False)
        self.result: object = None
        self.secret_id_var = tk.StringVar(value=current.secret_id if current else "")
        self.secret_key_var = tk.StringVar(value=current.secret_key if current else "")
        self.show_key_var = tk.BooleanVar(value=False)

        body = ttk.Frame(self, padding=(22, 18))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(1, weight=1)
        ttk.Label(
            body,
            text="腾讯云机器翻译",
            font=("Microsoft YaHei UI", 13, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 18))
        ttk.Label(body, text="SecretId").grid(row=1, column=0, sticky=tk.W, pady=6)
        self.secret_id_entry = ttk.Entry(body, textvariable=self.secret_id_var)
        self.secret_id_entry.grid(
            row=1,
            column=1,
            sticky=tk.EW,
            padx=(14, 0),
            pady=6,
            ipady=4,
        )
        ttk.Label(body, text="SecretKey").grid(row=2, column=0, sticky=tk.W, pady=6)
        self.secret_key_entry = ttk.Entry(
            body,
            textvariable=self.secret_key_var,
            show="●",
        )
        self.secret_key_entry.grid(
            row=2,
            column=1,
            sticky=tk.EW,
            padx=(14, 0),
            pady=6,
            ipady=4,
        )
        ttk.Checkbutton(
            body,
            text="显示 SecretKey",
            variable=self.show_key_var,
            command=self._toggle_key_visibility,
        ).grid(row=3, column=1, sticky=tk.W, padx=(14, 0), pady=(2, 0))

        actions = ttk.Frame(body)
        actions.grid(row=4, column=0, columnspan=2, sticky=tk.EW, pady=(24, 0))
        ttk.Button(actions, text="清除密钥", command=self._clear).pack(side=tk.LEFT)
        ttk.Button(actions, text="取消", command=self._cancel).pack(side=tk.RIGHT)
        ttk.Button(actions, text="保存", command=self._save).pack(
            side=tk.RIGHT,
            padx=(0, 8),
        )

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda event: self._cancel())
        self.bind("<Return>", lambda event: self._save())
        self._center(parent)
        _show_modal_window(self, parent)
        self.secret_id_entry.focus_set()

    @classmethod
    def ask(
        cls,
        parent: tk.Misc,
        current: Optional[TranslationCredentials] = None,
    ) -> object:
        dialog = cls(parent, current)
        parent.wait_window(dialog)
        return dialog.result

    def _toggle_key_visibility(self) -> None:
        self.secret_key_entry.configure(show="" if self.show_key_var.get() else "●")

    def _save(self) -> None:
        credentials = TranslationCredentials(
            self.secret_id_var.get().strip(),
            self.secret_key_var.get().strip(),
        )
        if not credentials.complete:
            messagebox.showwarning(
                "密钥不完整",
                "请输入 SecretId 和 SecretKey。",
                parent=self,
            )
            return
        self.result = credentials
        self.destroy()

    def _clear(self) -> None:
        if not messagebox.askyesno(
            "清除翻译密钥",
            "确定从 Windows 凭据库中清除腾讯云翻译密钥吗？",
            parent=self,
        ):
            return
        self.result = False
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()

    def _center(self, parent: tk.Misc) -> None:
        self.update_idletasks()
        width = max(1, self.winfo_width())
        height = max(1, self.winfo_height())
        left, top, right, bottom = _pointer_work_area(parent)
        x = parent.winfo_pointerx() - width // 2
        y = parent.winfo_pointery() - height // 2
        x = max(left, min(x, right - width))
        y = max(top, min(y, bottom - height))
        self.geometry(f"{width}x{height}+{x}+{y}")


class ImageTranslationDialog(tk.Toplevel):
    WIDTH = 1180
    HEIGHT = 760
    MIN_WIDTH = 900
    MIN_HEIGHT = 580
    MARGIN = 16
    COMPARE_GAP = 24
    ACCENT = "#087EA4"
    MODE_LABELS = {
        "local": "本地离线（免费）",
        "tencent_smart": "腾讯同版式（智能回退）",
        "cloud": "腾讯同版式",
        "privacy": "腾讯仅传文字",
    }

    def __init__(
        self,
        parent: tk.Misc,
        image: Image.Image,
        translate_func: TranslateFunction,
        source_language: str = "auto",
        target_language: str = "zh",
        translation_mode: str = "local",
        quality_mode: int = 0,
        title: str = "图片翻译",
        copy_image_func: Optional[Callable[[Image.Image], None]] = None,
        pin_image_func: Optional[Callable[[Image.Image, str], None]] = None,
        confirm_cloud_func: Optional[Callable[[str], bool]] = None,
        confirm_local_func: Optional[Callable[[str, str], bool]] = None,
        on_close: Optional[Callable[[], None]] = None,
        status_func: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(parent)
        self.parent = parent
        self.source_image = image.convert("RGB")
        self.translate_func = translate_func
        self.copy_image_func = copy_image_func
        self.pin_image_func = pin_image_func
        self.confirm_cloud_func = confirm_cloud_func
        self.confirm_local_func = confirm_local_func
        self.on_close_callback = on_close
        self.status_func = status_func
        self.quality_mode = 1 if int(quality_mode) == 1 else 0
        self.document: Optional[TranslationDocument] = None

        normalized_mode = (
            translation_mode if translation_mode in self.MODE_LABELS else "local"
        )
        self.display_mode = tk.StringVar(value="original")
        self.source_var = tk.StringVar(value=language_label(source_language))
        self.target_var = tk.StringVar(value=language_label(target_language))
        self.translation_mode_var = tk.StringVar(
            value=self.MODE_LABELS[normalized_mode]
        )
        self.status_var = tk.StringVar(value="准备翻译")
        self.language_var = tk.StringVar(
            value=language_label(source_language) + " → " + language_label(target_language)
        )
        self.zoom_var = tk.StringVar(value="100%")
        self.fit_mode = True
        self.render_scale = 1.0
        self.image_origin = (self.MARGIN, self.MARGIN)
        self.photo: Optional[ImageTk.PhotoImage] = None
        self.render_job: Optional[str] = None
        self.poll_job: Optional[str] = None
        self.startup_job: Optional[str] = None
        self.sash_job: Optional[str] = None
        self.result_queue: queue.SimpleQueue[tuple[int, str, object]] = queue.SimpleQueue()
        self.generation = 0
        self.cancel_event = threading.Event()
        self.closed = False
        self.selected_region: Optional[int] = None
        self.hovered_region: Optional[int] = None
        self.source_tags: list[str] = []
        self.target_tags: list[str] = []
        self.text_context_widget: Optional[tk.Text] = None
        self.text_context_index = "1.0"

        self.title(f"图片翻译 - {title}")
        self.geometry(f"{self.WIDTH}x{self.HEIGHT}")
        self.minsize(self.MIN_WIDTH, self.MIN_HEIGHT)
        self.configure(bg="#F3F5F7")
        self._build_ui()
        self._center_at_pointer()
        if not _parent_is_visible(parent):
            self.attributes("-topmost", True)
        self.deiconify()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda event: self.close())
        self.render_job = self.after(80, self._render_image)
        self.startup_job = self.after(120, self._start_translation)
        self.sash_job = self.after(140, self._set_initial_sash)
        self.lift()
        self.focus_force()

    def _build_ui(self) -> None:
        shell = ttk.Frame(self, padding=(14, 12))
        shell.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(shell)
        header.pack(fill=tk.X, pady=(0, 9))
        ttk.Label(
            header,
            text="图片翻译",
            font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(side=tk.LEFT)
        ttk.Label(
            header,
            textvariable=self.status_var,
            foreground="#667685",
        ).pack(side=tk.LEFT, padx=(12, 0))
        self.translate_button = ttk.Button(
            header,
            text="重新翻译",
            command=self._start_translation,
        )
        self.translate_button.pack(side=tk.RIGHT)

        controls = ttk.Frame(shell)
        controls.pack(fill=tk.X, pady=(0, 9))
        self.source_combo = ttk.Combobox(
            controls,
            textvariable=self.source_var,
            values=("自动检测", *LANGUAGE_LABELS.values()),
            state="readonly",
            width=18,
        )
        self.source_combo.pack(side=tk.LEFT)
        ttk.Label(controls, text="→").pack(side=tk.LEFT, padx=7)
        self.target_combo = ttk.Combobox(
            controls,
            textvariable=self.target_var,
            values=tuple(LANGUAGE_LABELS.values()),
            state="readonly",
            width=18,
        )
        self.target_combo.pack(side=tk.LEFT)
        self.mode_combo = ttk.Combobox(
            controls,
            textvariable=self.translation_mode_var,
            values=tuple(self.MODE_LABELS.values()),
            state="readonly",
            width=24,
        )
        self.mode_combo.pack(side=tk.LEFT, padx=(10, 0))

        display_controls = ttk.Frame(controls)
        display_controls.pack(side=tk.RIGHT)
        for value, label in (
            ("original", "原图"),
            ("translated", "译图"),
            ("compare", "对照"),
        ):
            ttk.Radiobutton(
                display_controls,
                text=label,
                value=value,
                variable=self.display_mode,
                command=self._render_image,
            ).pack(side=tk.LEFT, padx=(0, 5))

        self.panes = ttk.Panedwindow(shell, orient=tk.HORIZONTAL)
        self.panes.pack(fill=tk.BOTH, expand=True)
        left = ttk.Frame(self.panes, width=760)
        right = ttk.Frame(self.panes, width=360)
        self.panes.add(left, weight=3)
        self.panes.add(right, weight=2)

        self._build_image_panel(left)
        self._build_text_panel(right)
        self._build_footer(shell)

    def _build_image_panel(self, parent: ttk.Frame) -> None:
        image_toolbar = ttk.Frame(parent)
        image_toolbar.pack(fill=tk.X, pady=(0, 7))
        ttk.Label(
            image_toolbar,
            textvariable=self.language_var,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side=tk.LEFT)
        ttk.Button(
            image_toolbar,
            text="+",
            width=3,
            command=lambda: self._zoom(1.2),
        ).pack(side=tk.RIGHT)
        ttk.Button(
            image_toolbar,
            text="适应",
            width=6,
            command=self._fit_image,
        ).pack(side=tk.RIGHT, padx=5)
        ttk.Button(
            image_toolbar,
            text="-",
            width=3,
            command=lambda: self._zoom(0.83),
        ).pack(side=tk.RIGHT)
        ttk.Label(
            image_toolbar,
            textvariable=self.zoom_var,
            width=7,
            anchor=tk.E,
        ).pack(side=tk.RIGHT, padx=(0, 8))

        canvas_frame = ttk.Frame(parent)
        canvas_frame.pack(fill=tk.BOTH, expand=True)
        canvas_frame.rowconfigure(0, weight=1)
        canvas_frame.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(
            canvas_frame,
            bg="#20272E",
            highlightthickness=0,
            takefocus=True,
        )
        x_scroll = ttk.Scrollbar(
            canvas_frame,
            orient=tk.HORIZONTAL,
            command=self.canvas.xview,
        )
        y_scroll = ttk.Scrollbar(
            canvas_frame,
            orient=tk.VERTICAL,
            command=self.canvas.yview,
        )
        self.canvas.configure(
            xscrollcommand=x_scroll.set,
            yscrollcommand=y_scroll.set,
        )
        self.canvas.grid(row=0, column=0, sticky=tk.NSEW)
        y_scroll.grid(row=0, column=1, sticky=tk.NS)
        x_scroll.grid(row=1, column=0, sticky=tk.EW)
        self.canvas.bind("<Configure>", self._schedule_render)
        self.canvas.bind("<Button-1>", self._on_image_click)
        self.canvas.bind("<Double-Button-1>", self._on_image_double_click)
        self.canvas.bind("<Motion>", self._on_image_motion)
        self.canvas.bind("<Leave>", self._on_image_leave)
        self.canvas.bind("<MouseWheel>", self._on_image_wheel)
        self.canvas.bind(
            "<Control-c>",
            lambda event: self._copy_selected_target(),
        )
        self.image_menu = tk.Menu(self.canvas, tearoff=False)
        self.image_menu.add_command(
            label="复制译文",
            command=self._copy_selected_target,
        )
        self.image_menu.add_command(
            label="复制原文",
            command=self._copy_selected_source,
        )
        self.canvas.bind("<Button-3>", self._show_image_menu)

    def _build_text_panel(self, parent: ttk.Frame) -> None:
        header = ttk.Frame(parent)
        header.pack(fill=tk.X, padx=(12, 0), pady=(0, 7))
        ttk.Label(header, text="文本").pack(side=tk.LEFT)
        self.copy_target_button = ttk.Button(
            header,
            text="复制全部译文",
            state="disabled",
            command=self._copy_all_target,
        )
        self.copy_target_button.pack(side=tk.RIGHT)
        self.text_notebook = ttk.Notebook(parent)
        self.text_notebook.pack(fill=tk.BOTH, expand=True, padx=(12, 0))
        self.target_text = self._add_text_tab("译文")
        self.source_text = self._add_text_tab("原文")
        self.text_menu = tk.Menu(self.text_notebook, tearoff=False)
        self.text_menu.add_command(label="复制", command=self._copy_text_selection)
        self.text_menu.add_command(
            label="复制当前行",
            command=self._copy_context_line,
        )
        self.text_menu.add_command(
            label="复制全部",
            command=self._copy_context_all,
        )
        self.text_menu.add_separator()
        self.text_menu.add_command(label="全选", command=self._select_all_context_text)

    def _build_footer(self, parent: ttk.Frame) -> None:
        footer = ttk.Frame(parent)
        footer.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(footer, text="关闭", command=self.close).pack(side=tk.RIGHT)
        self.pin_button = ttk.Button(
            footer, text="贴到屏幕", state="disabled", command=self._pin_image
        )
        self.pin_button.pack(side=tk.RIGHT, padx=(0, 7))
        self.save_button = ttk.Button(
            footer, text="保存译图", state="disabled", command=self._save_image
        )
        self.save_button.pack(side=tk.RIGHT, padx=(0, 7))
        self.copy_image_button = ttk.Button(
            footer, text="复制译图", state="disabled", command=self._copy_image
        )
        self.copy_image_button.pack(side=tk.RIGHT, padx=(0, 7))
        self.cancel_button = ttk.Button(
            footer, text="取消任务", state="disabled", command=self._cancel_task
        )
        self.cancel_button.pack(side=tk.LEFT)

    def _add_text_tab(self, label: str) -> tk.Text:
        frame = ttk.Frame(self.text_notebook)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        widget = tk.Text(
            frame,
            wrap=tk.WORD,
            state="disabled",
            exportselection=False,
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground="#C9D3DC",
            bg="#FFFFFF",
            fg="#17212B",
            selectbackground="#B9DDEA",
            font=("Microsoft YaHei UI", 11),
            padx=12,
            pady=12,
        )
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=widget.yview)
        widget.configure(yscrollcommand=scrollbar.set)
        widget.grid(row=0, column=0, sticky=tk.NSEW)
        scrollbar.grid(row=0, column=1, sticky=tk.NS)
        widget.tag_configure("selected_region", background="#D7EEF5")
        widget.bind(
            "<Button-1>",
            lambda event, text_widget=widget: self._on_text_click(text_widget, event),
        )
        widget.bind(
            "<Button-3>",
            lambda event, text_widget=widget: self._show_text_menu(text_widget, event),
        )
        widget.bind(
            "<Control-c>",
            lambda event, text_widget=widget: self._copy_widget_selection(text_widget),
        )
        self.text_notebook.add(frame, text=label)
        return widget

    def _selected_mode(self) -> str:
        # Map the visible label back to a stable mode.
        selected = self.translation_mode_var.get()
        for code, label in self.MODE_LABELS.items():
            if label == selected:
                return code
        return "local"

    def _start_translation(self) -> None:
        self.startup_job = None
        if self.closed:
            return
        mode = self._selected_mode()
        source = language_code_for_label(self.source_var.get(), fallback="auto")
        target = language_code_for_label(self.target_var.get())
        if mode == "local" and self.confirm_local_func:
            if not self.confirm_local_func(source, target):
                self.status_var.set("已取消本地翻译")
                return
        elif mode != "local" and self.confirm_cloud_func:
            if not self.confirm_cloud_func(mode):
                self.status_var.set("已取消腾讯云翻译")
                return
        self.generation += 1
        generation = self.generation
        self.cancel_event.set()
        self.cancel_event = threading.Event()
        cancel_event = self.cancel_event
        self.status_var.set("正在准备翻译...")
        self.translate_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self._launch_worker(generation, source, target, mode, cancel_event)

    def _launch_worker(self, generation, source, target, mode, cancel_event) -> None:
        def progress(message: str) -> None:
            self.result_queue.put((generation, "progress", message))

        thread = threading.Thread(
            target=lambda: self._run_translation(
                generation,
                source,
                target,
                mode,
                cancel_event,
                progress,
            ),
            name="screenshot-translation",
            daemon=True,
        )
        thread.start()
        self.poll_job = self.after(50, self._poll_result)

    def _run_translation(
        self,
        generation,
        source,
        target,
        mode,
        cancel_event,
        progress,
    ) -> None:
        try:
            value = self.translate_func(
                self.source_image.copy(),
                source,
                target,
                mode,
                self.quality_mode,
                progress,
                cancel_event,
            )
        except Exception as exc:
            self.result_queue.put((generation, "error", exc))
        else:
            self.result_queue.put((generation, "result", value))

    def _poll_result(self) -> None:
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
                self.status_var.set(str(value))
                continue
            terminal = True
            self._handle_terminal_result(kind, value)
        if not terminal:
            self.poll_job = self.after(50, self._poll_result)

    def _handle_terminal_result(self, kind: str, value: object) -> None:
        self.translate_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        if kind == "result" and isinstance(value, TranslationDocument):
            self._set_document(value)
            return
        if isinstance(value, TranslationCancelled):
            self.status_var.set("已取消翻译")
            return
        self.status_var.set("翻译失败")
        messagebox.showerror("图片翻译失败", str(value), parent=self)
        if self.status_func:
            self.status_func(f"图片翻译失败：{value}")

    def _set_document(self, document: TranslationDocument) -> None:
        self.document = document
        self.selected_region = None
        self.hovered_region = None
        self.display_mode.set("translated")
        self.language_var.set(
            f"{language_label(document.source_language)} → "
            f"{language_label(document.target_language)}"
        )
        self.status_var.set(f"已翻译 {len(document.regions)} 个文字区域")
        self._populate_texts()
        for button in (
            self.copy_target_button,
            self.copy_image_button,
            self.save_button,
            self.pin_button,
        ):
            button.configure(state="normal")
        self.fit_mode = True
        self._render_image()
        if self.status_func:
            self.status_func(self.status_var.get())

    def _populate_texts(self) -> None:
        if not self.document:
            return
        sources = [item.source_text for item in self.document.regions]
        targets = [item.target_text for item in self.document.regions]
        self.source_tags = self._fill_text(self.source_text, sources, "source_region_")
        self.target_tags = self._fill_text(self.target_text, targets, "target_region_")

    @staticmethod
    def _fill_text(widget: tk.Text, values: list[str], prefix: str) -> list[str]:
        widget.configure(state="normal")
        widget.delete("1.0", tk.END)
        tags = []
        for index, value in enumerate(values):
            start = widget.index(tk.END + "-1c")
            widget.insert(tk.END, value)
            end = widget.index(tk.END + "-1c")
            tag = f"{prefix}{index}"
            widget.tag_add(tag, start, end)
            tags.append(tag)
            if index < len(values) - 1:
                widget.insert(tk.END, "\n")
        widget.configure(state="disabled")
        return tags

    def _cancel_task(self) -> None:
        self.cancel_event.set()
        self.generation += 1
        if self.poll_job:
            try:
                self.after_cancel(self.poll_job)
            except tk.TclError:
                pass
            self.poll_job = None
        self.cancel_button.configure(state="disabled")
        self.translate_button.configure(state="normal")
        self.status_var.set("已取消翻译")

    def _logical_size(self) -> tuple[int, int]:
        if self.display_mode.get() != "compare" or not self.document:
            return self._active_image().size
        target = self.document.translated_image
        return (
            self.source_image.width + self.COMPARE_GAP + target.width,
            max(self.source_image.height, target.height),
        )

    def _active_image(self) -> Image.Image:
        if self.display_mode.get() == "translated" and self.document:
            return self.document.translated_image
        return self.source_image

    def _schedule_render(self, event: Optional[tk.Event] = None) -> None:
        if not self.fit_mode or self.closed:
            return
        if self.render_job:
            try:
                self.after_cancel(self.render_job)
            except tk.TclError:
                pass
        self.render_job = self.after(70, self._render_image)

    def _render_image(self) -> None:
        self.render_job = None
        if self.closed:
            return
        canvas_width = max(360, self.canvas.winfo_width())
        canvas_height = max(300, self.canvas.winfo_height())
        logical_width, logical_height = self._logical_size()
        if self.fit_mode:
            self.render_scale = min(
                2.0,
                max(0.02, min(
                    (canvas_width - self.MARGIN * 2) / max(1, logical_width),
                    (canvas_height - self.MARGIN * 2) / max(1, logical_height),
                )),
            )
        pixel_limit = math.sqrt(42_000_000 / max(1, logical_width * logical_height))
        self.render_scale = max(0.02, min(4.0, pixel_limit, self.render_scale))
        self._install_preview(canvas_width, canvas_height)

    def _install_preview(self, canvas_width: int, canvas_height: int) -> None:
        preview = self._make_preview(self.render_scale)
        self.photo = ImageTk.PhotoImage(preview)
        x = max(self.MARGIN, (canvas_width - preview.width) // 2)
        y = max(self.MARGIN, (canvas_height - preview.height) // 2)
        if preview.width >= canvas_width:
            x = self.MARGIN
        if preview.height >= canvas_height:
            y = self.MARGIN
        self.image_origin = (x, y)
        self.canvas.delete("all")
        self.canvas.create_image(x, y, image=self.photo, anchor=tk.NW)
        self.canvas.configure(
            scrollregion=(
                0,
                0,
                max(canvas_width, x + preview.width + self.MARGIN),
                max(canvas_height, y + preview.height + self.MARGIN),
            )
        )
        self.zoom_var.set(f"{int(round(self.render_scale * 100))}%")
        self._draw_overlays()

    def _make_preview(self, scale: float) -> Image.Image:
        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")

        def resized(image: Image.Image) -> Image.Image:
            size = (
                max(1, int(round(image.width * scale))),
                max(1, int(round(image.height * scale))),
            )
            return image.resize(size, resampling) if size != image.size else image.copy()

        if self.display_mode.get() != "compare" or not self.document:
            return resized(self._active_image())
        source = resized(self.source_image)
        target = resized(self.document.translated_image)
        gap = max(4, int(round(self.COMPARE_GAP * scale)))
        preview = Image.new(
            "RGB",
            (source.width + gap + target.width, max(source.height, target.height)),
            "#20272E",
        )
        preview.paste(source, (0, 0))
        preview.paste(target, (source.width + gap, 0))
        return preview

    def _region_offset_x(self) -> int:
        if self.display_mode.get() == "compare" and self.document:
            return self.source_image.width + self.COMPARE_GAP
        return 0

    def _draw_overlays(self) -> None:
        self.canvas.delete("translation_overlay")
        if not self.document:
            return
        offset_x = self._region_offset_x()
        for index, region in enumerate(self.document.regions):
            points = []
            for x, y in region.polygon:
                points.extend((
                    self.image_origin[0] + (x + offset_x) * self.render_scale,
                    self.image_origin[1] + y * self.render_scale,
                ))
            if len(points) >= 6:
                self._draw_region(index, points)

    def _draw_region(self, index: int, points: list[float]) -> None:
        index = int(index)
        selected = index == self.selected_region
        hovered = index == self.hovered_region
        self.canvas.create_polygon(
            *points,
            fill=self.ACCENT if selected else "",
            stipple="gray50" if selected else "",
            outline="#00C8F0" if hovered else "#19A7CE",
            width=2 if selected or hovered else 1,
            tags=("translation_overlay",),
        )

    def _region_at_event(self, event: tk.Event) -> Optional[int]:
        if not self.document:
            return None
        x = (self.canvas.canvasx(event.x) - self.image_origin[0]) / self.render_scale
        y = (self.canvas.canvasy(event.y) - self.image_origin[1]) / self.render_scale
        if self.display_mode.get() == "compare":
            x -= self.source_image.width + self.COMPARE_GAP
            if x < 0:
                return None
        return self.document.region_at((int(round(x)), int(round(y))))

    def _on_image_click(self, event: tk.Event) -> str:
        self._select_region(self._region_at_event(event))
        self.canvas.focus_set()
        return "break"

    def _fit_image(self) -> None:
        self.fit_mode = True
        self._render_image()

    def _zoom(self, factor: float) -> None:
        self.fit_mode = False
        self.render_scale = max(0.02, min(4.0, self.render_scale * factor))
        self._render_image()

    def _on_image_double_click(self, event: tk.Event) -> str:
        self._select_region(self._region_at_event(event))
        return self._copy_selected_target()

    def _on_image_motion(self, event: tk.Event) -> None:
        index = self._region_at_event(event)
        if index == self.hovered_region:
            return
        self.hovered_region = index
        self.canvas.configure(cursor="hand2" if index is not None else "arrow")
        self._draw_overlays()

    def _on_image_leave(self, event: Optional[tk.Event] = None) -> None:
        self.hovered_region = None
        self._draw_overlays()

    def _on_image_wheel(self, event: tk.Event) -> str:
        if event.state & 0x0004:
            self._zoom(1.15 if event.delta > 0 else 0.87)
        elif event.state & 0x0001:
            self.canvas.xview_scroll(-1 if event.delta > 0 else 1, "units")
        else:
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def _show_image_menu(self, event: tk.Event) -> str:
        self._select_region(self._region_at_event(event))
        try:
            self.image_menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                self.image_menu.grab_release()
            except tk.TclError:
                pass
        return "break"

    def _on_text_click(self, widget: tk.Text, event: tk.Event) -> None:
        position = widget.index(f"@{event.x},{event.y}")
        for tag in widget.tag_names(position):
            if tag.startswith(("source_region_", "target_region_")):
                try:
                    self._select_region(int(tag.rsplit("_", 1)[1]))
                except ValueError:
                    pass
                break

    def _select_region(self, index: Optional[int]) -> None:
        self.selected_region = index
        for widget in (self.source_text, self.target_text):
            widget.tag_remove("selected_region", "1.0", tk.END)
        if index is not None:
            self._select_text_tag(self.source_text, self.source_tags, index)
            self._select_text_tag(self.target_text, self.target_tags, index)
        self._draw_overlays()

    @staticmethod
    def _select_text_tag(widget: tk.Text, tags: list[str], index: int) -> None:
        if not 0 <= index < len(tags):
            return
        ranges = widget.tag_ranges(tags[index])
        if len(ranges) == 2:
            widget.tag_add("selected_region", ranges[0], ranges[1])
            widget.see(ranges[0])

    def _selected_region_text(self, target: bool) -> str:
        if not self.document or self.selected_region is None:
            return ""
        if not 0 <= self.selected_region < len(self.document.regions):
            return ""
        region = self.document.regions[self.selected_region]
        return region.target_text if target else region.source_text

    def _copy_selected_target(self) -> str:
        return self._copy_text(self._selected_region_text(True), "译文已复制")

    def _copy_selected_source(self) -> str:
        return self._copy_text(self._selected_region_text(False), "原文已复制")

    def _copy_all_target(self) -> str:
        text = self.document.target_text if self.document else ""
        return self._copy_text(text, "全部译文已复制")

    def _copy_widget_selection(self, widget: tk.Text) -> str:
        try:
            text = widget.get(tk.SEL_FIRST, tk.SEL_LAST)
        except tk.TclError:
            text = ""
        label = "译文" if widget is self.target_text else "原文"
        return self._copy_text(text, f"已复制选中{label}")

    def _copy_text_selection(self) -> str:
        if self.text_context_widget is None:
            return "break"
        return self._copy_widget_selection(self.text_context_widget)

    def _copy_context_line(self) -> str:
        widget = self.text_context_widget
        if widget is None:
            return "break"
        start = widget.index(f"{self.text_context_index} linestart")
        end = widget.index(f"{self.text_context_index} lineend")
        return self._copy_text(widget.get(start, end), "已复制当前行")

    def _copy_context_all(self) -> str:
        widget = self.text_context_widget
        if widget is None:
            return "break"
        label = "译文" if widget is self.target_text else "原文"
        return self._copy_text(
            widget.get("1.0", tk.END + "-1c"),
            f"已复制全部{label}",
        )

    def _select_all_context_text(self) -> str:
        widget = self.text_context_widget
        if widget is None:
            return "break"
        widget.tag_add(tk.SEL, "1.0", tk.END + "-1c")
        widget.mark_set(tk.INSERT, "1.0")
        widget.see("1.0")
        return "break"

    def _show_text_menu(self, widget: tk.Text, event: tk.Event) -> str:
        widget.focus_set()
        self.text_context_widget = widget
        self.text_context_index = widget.index(f"@{event.x},{event.y}")
        try:
            has_selection = bool(widget.get(tk.SEL_FIRST, tk.SEL_LAST))
        except tk.TclError:
            has_selection = False
        self.text_menu.entryconfigure(
            "复制",
            state="normal" if has_selection else "disabled",
        )
        try:
            self.text_menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                self.text_menu.grab_release()
            except tk.TclError:
                pass
        return "break"

    def _copy_text(self, text: str, message: str) -> str:
        if not text.strip():
            self.status_var.set("没有可复制的文字")
            return "break"
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update_idletasks()
        self.status_var.set(message)
        if self.status_func:
            self.status_func(message)
        return "break"

    def _copy_image(self) -> None:
        if not self.document or not self.copy_image_func:
            return
        try:
            self.copy_image_func(self.document.translated_image)
            self.status_var.set("译图已复制")
        except Exception as exc:
            messagebox.showerror("复制译图失败", str(exc), parent=self)

    def _pin_image(self) -> None:
        if not self.document or not self.pin_image_func:
            return
        self.pin_image_func(self.document.translated_image.copy(), "翻译贴图")
        self.status_var.set("译图已贴到屏幕")

    def _save_image(self) -> None:
        if not self.document:
            return
        target = filedialog.asksaveasfilename(
            parent=self,
            title="保存译图",
            defaultextension=".png",
            filetypes=(("PNG 图片", "*.png"), ("JPEG 图片", "*.jpg *.jpeg")),
            initialfile="translated.png",
        )
        if not target:
            return
        path = Path(target)
        options = {"quality": 92} if path.suffix.lower() in {".jpg", ".jpeg"} else {}
        self.document.translated_image.convert("RGB").save(path, **options)
        self.status_var.set(f"译图已保存：{path.name}")

    def _set_initial_sash(self) -> None:
        self.sash_job = None
        if self.closed:
            return
        try:
            self.panes.sashpos(0, max(520, int(self.panes.winfo_width() * 0.67)))
        except tk.TclError:
            pass

    def _center_at_pointer(self) -> None:
        self.update_idletasks()
        left, top, right, bottom = _pointer_work_area(self.parent)
        work_width = max(1, right - left)
        work_height = max(1, bottom - top)
        width = min(self.WIDTH, max(self.MIN_WIDTH, work_width - 40))
        height = min(self.HEIGHT, max(self.MIN_HEIGHT, work_height - 80))
        width = min(width, work_width)
        height = min(height, work_height)
        x = max(left, min(self.winfo_pointerx() - width // 2, right - width))
        y = max(top, min(self.winfo_pointery() - height // 2, bottom - height))
        self.geometry(f"{width}x{height}+{x}+{y}")

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.cancel_event.set()
        self.generation += 1
        for job in (self.poll_job, self.render_job, self.startup_job, self.sash_job):
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        self.poll_job = None
        self.render_job = None
        self.startup_job = None
        self.sash_job = None
        self.destroy()
        if self.on_close_callback:
            self.on_close_callback()
