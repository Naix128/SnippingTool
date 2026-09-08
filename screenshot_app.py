from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import html as html_lib
import io
import json
import math
import os
import platform
import queue
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import (
    Image,
    ImageChops,
    ImageColor,
    ImageDraw,
    ImageEnhance,
    ImageFilter,
    ImageFont,
    ImageGrab,
    ImageOps,
    ImageStat,
    ImageTk,
)

Box = Tuple[int, int, int, int]
Point = Tuple[int, int]


def enable_high_dpi() -> None:
    """让截图坐标和 Tkinter 鼠标坐标在 Windows 缩放下尽量一致。"""
    if platform.system() != "Windows":
        return

    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = ctypes.wintypes.BOOL
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


@dataclass(frozen=True)
class VirtualDesktopGeometry:
    """Windows 虚拟桌面的物理像素边界，可包含负坐标原点。"""

    left: int
    top: int
    width: int
    height: int

    SM_XVIRTUALSCREEN = 76
    SM_YVIRTUALSCREEN = 77
    SM_CXVIRTUALSCREEN = 78
    SM_CYVIRTUALSCREEN = 79

    @classmethod
    def detect(
        cls,
        fallback_size: Optional[Tuple[int, int]] = None,
    ) -> "VirtualDesktopGeometry":
        if platform.system() == "Windows":
            user32 = ctypes.windll.user32
            left = int(user32.GetSystemMetrics(cls.SM_XVIRTUALSCREEN))
            top = int(user32.GetSystemMetrics(cls.SM_YVIRTUALSCREEN))
            width = int(user32.GetSystemMetrics(cls.SM_CXVIRTUALSCREEN))
            height = int(user32.GetSystemMetrics(cls.SM_CYVIRTUALSCREEN))
            if width > 0 and height > 0:
                return cls(left, top, width, height)

        width, height = fallback_size or (1, 1)
        return cls(0, 0, max(1, int(width)), max(1, int(height)))

    @property
    def origin(self) -> Point:
        return self.left, self.top

    @property
    def size(self) -> Tuple[int, int]:
        return self.width, self.height

    @property
    def bounds(self) -> Box:
        return self.left, self.top, self.left + self.width, self.top + self.height

    @property
    def window_geometry(self) -> str:
        return f"{self.width}x{self.height}+{self.left}+{self.top}"

    def to_absolute_point(self, point: Point) -> Point:
        return self.left + int(point[0]), self.top + int(point[1])

    def to_absolute_box(self, box: Box) -> Box:
        return (
            self.left + int(box[0]),
            self.top + int(box[1]),
            self.left + int(box[2]),
            self.top + int(box[3]),
        )

    def to_local_box(self, box: Box) -> Box:
        return (
            int(box[0]) - self.left,
            int(box[1]) - self.top,
            int(box[2]) - self.left,
            int(box[3]) - self.top,
        )


@dataclass(frozen=True)
class ScreenSnapshot:
    image: Image.Image
    geometry: VirtualDesktopGeometry


@dataclass(frozen=True)
class MonitorWorkArea:
    left: int
    top: int
    right: int
    bottom: int

    @classmethod
    def at_point(cls, point: Point) -> "MonitorWorkArea":
        desktop = VirtualDesktopGeometry.detect()
        fallback = cls(*desktop.bounds)
        if platform.system() != "Windows":
            return fallback

        class MonitorInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.wintypes.DWORD),
                ("rcMonitor", ctypes.wintypes.RECT),
                ("rcWork", ctypes.wintypes.RECT),
                ("dwFlags", ctypes.wintypes.DWORD),
            ]

        try:
            user32 = ctypes.windll.user32
            user32.MonitorFromPoint.argtypes = [
                ctypes.wintypes.POINT,
                ctypes.wintypes.DWORD,
            ]
            user32.MonitorFromPoint.restype = ctypes.wintypes.HANDLE
            user32.GetMonitorInfoW.argtypes = [
                ctypes.wintypes.HANDLE,
                ctypes.POINTER(MonitorInfo),
            ]
            user32.GetMonitorInfoW.restype = ctypes.wintypes.BOOL
            monitor = user32.MonitorFromPoint(
                ctypes.wintypes.POINT(int(point[0]), int(point[1])),
                2,
            )
            info = MonitorInfo()
            info.cbSize = ctypes.sizeof(MonitorInfo)
            if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                return cls(
                    info.rcWork.left,
                    info.rcWork.top,
                    info.rcWork.right,
                    info.rcWork.bottom,
                )
        except (AttributeError, OSError, ctypes.ArgumentError):
            pass
        return fallback

    @property
    def width(self) -> int:
        return max(1, self.right - self.left)

    @property
    def height(self) -> int:
        return max(1, self.bottom - self.top)


@dataclass
class ScreenshotSettings:
    output_dir: Path
    auto_save: bool = True
    image_format: str = "PNG"
    image_quality: int = 92
    filename_pattern: str = "Screenshot_{yyyy}-{MM}-{dd}_{HH}-{mm}-{ss}"


class AppTheme:
    """集中维护可选强调色和派生色，供主界面与首选项共用。"""

    DEFAULT_ACCENT = "#087EA4"
    ACCENTS = (
        "#087EA4",
        "#2563EB",
        "#16835A",
        "#C23B4A",
        "#7C3AED",
    )

    @classmethod
    def normalize_accent(cls, value: object) -> str:
        candidate = str(value or "").strip().upper()
        try:
            ImageColor.getrgb(candidate)
        except ValueError:
            return cls.DEFAULT_ACCENT
        return candidate if re.fullmatch(r"#[0-9A-F]{6}", candidate) else cls.DEFAULT_ACCENT

    @staticmethod
    def blend(color: str, target: str, ratio: float) -> str:
        source_rgb = ImageColor.getrgb(color)
        target_rgb = ImageColor.getrgb(target)
        amount = max(0.0, min(1.0, float(ratio)))
        mixed = tuple(
            int(round(source + (destination - source) * amount))
            for source, destination in zip(source_rgb, target_rgb)
        )
        return "#{:02X}{:02X}{:02X}".format(*mixed)


class OutputNamePolicy:
    """渲染安全文件名并根据输出格式选择扩展名。"""

    DEFAULT_PATTERN = "Screenshot_{yyyy}-{MM}-{dd}_{HH}-{mm}-{ss}"
    DEFAULT_QUICK_PATTERN = "Quick_{yyyy}-{MM}-{dd}_{HH}-{mm}-{ss}"
    FORMAT_EXTENSIONS = {"PNG": ".png", "JPEG": ".jpg"}
    EXTENSION_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG"}
    INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

    @classmethod
    def normalize_format(cls, value: object) -> str:
        candidate = str(value or "").strip().upper()
        return candidate if candidate in cls.FORMAT_EXTENSIONS else "PNG"

    @classmethod
    def render_stem(
        cls,
        pattern: object,
        moment: Optional[datetime] = None,
    ) -> str:
        now = moment or datetime.now()
        value = str(pattern or "").strip() or cls.DEFAULT_PATTERN
        replacements = {
            "{yyyy}": f"{now.year:04d}",
            "{MM}": f"{now.month:02d}",
            "{dd}": f"{now.day:02d}",
            "{HH}": f"{now.hour:02d}",
            "{mm}": f"{now.minute:02d}",
            "{ss}": f"{now.second:02d}",
        }
        for token, replacement in replacements.items():
            value = value.replace(token, replacement)
        value = cls.INVALID_FILENAME.sub("_", value).strip(" .")
        suffix = Path(value).suffix.lower()
        if suffix in cls.EXTENSION_FORMATS:
            value = value[: -len(suffix)].rstrip(" .")
        return (value or "Screenshot")[:180]

    @classmethod
    def next_target(
        cls,
        directory: Path,
        pattern: object,
        image_format: object,
        moment: Optional[datetime] = None,
    ) -> Path:
        normalized_format = cls.normalize_format(image_format)
        stem = cls.render_stem(pattern, moment)
        extension = cls.FORMAT_EXTENSIONS[normalized_format]
        target = directory / f"{stem}{extension}"
        suffix = 2
        while target.exists():
            target = directory / f"{stem}_{suffix}{extension}"
            suffix += 1
        return target


@dataclass(frozen=True)
class OcrWordResult:
    text: str
    box: Box


@dataclass(frozen=True)
class OcrLineResult:
    text: str
    words: Tuple[OcrWordResult, ...]

    @property
    def box(self) -> Box:
        if not self.words:
            return 0, 0, 0, 0
        return (
            min(word.box[0] for word in self.words),
            min(word.box[1] for word in self.words),
            max(word.box[2] for word in self.words),
            max(word.box[3] for word in self.words),
        )


@dataclass(frozen=True)
class OcrDocument:
    text: str
    lines: Tuple[OcrLineResult, ...]
    image_size: Tuple[int, int]

    @property
    def word_count(self) -> int:
        return sum(len(line.words) for line in self.lines)


class OcrTextFormatter:
    """整理系统 OCR 在中文字符之间插入的多余空格。"""

    CJK = r"\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF"

    @classmethod
    def normalize_line(cls, value: object) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        text = re.sub(rf"(?<=[{cls.CJK}])\s+(?=[{cls.CJK}])", "", text)
        text = re.sub(r"\s+([，。！？；：、,.!?;:）)】》])", r"\1", text)
        text = re.sub(r"([（(【《])\s+", r"\1", text)
        return text

    @classmethod
    def join_words(cls, words: List[str]) -> str:
        return cls.normalize_line(" ".join(word for word in words if word.strip()))


class OcrSelectionModel:
    """在 OCR 坐标中完成点击命中、矩形框选和文本排序。"""

    def __init__(self, document: OcrDocument) -> None:
        self.document = document

    def line_at(self, point: Point, padding: int = 4) -> Optional[int]:
        x, y = point
        for line_index, line in enumerate(self.document.lines):
            left, top, right, bottom = line.box
            if left - padding <= x <= right + padding and top - padding <= y <= bottom + padding:
                return line_index
        return None

    def words_in_box(self, box: Box) -> set[Tuple[int, int]]:
        left, top, right, bottom = ScreenshotManager._normalize_box(box)
        selected: set[Tuple[int, int]] = set()
        for line_index, line in enumerate(self.document.lines):
            for word_index, word in enumerate(line.words):
                word_left, word_top, word_right, word_bottom = word.box
                if (
                    word_right >= left
                    and word_left <= right
                    and word_bottom >= top
                    and word_top <= bottom
                ):
                    selected.add((line_index, word_index))
        return selected

    def line_words(self, line_index: int) -> set[Tuple[int, int]]:
        if not 0 <= line_index < len(self.document.lines):
            return set()
        return {
            (line_index, word_index)
            for word_index in range(len(self.document.lines[line_index].words))
        }

    def all_words(self) -> set[Tuple[int, int]]:
        return {
            (line_index, word_index)
            for line_index, line in enumerate(self.document.lines)
            for word_index in range(len(line.words))
        }

    def text_for(self, selected: set[Tuple[int, int]]) -> str:
        lines: List[str] = []
        selected_by_line: dict[int, List[int]] = {}
        for line_index, word_index in sorted(selected):
            selected_by_line.setdefault(line_index, []).append(word_index)
        for line_index, word_indices in selected_by_line.items():
            if not 0 <= line_index < len(self.document.lines):
                continue
            line = self.document.lines[line_index]
            valid_indices = [
                index for index in word_indices if 0 <= index < len(line.words)
            ]
            if not valid_indices:
                continue
            if len(valid_indices) == len(line.words):
                lines.append(line.text)
            else:
                lines.append(
                    OcrTextFormatter.join_words(
                        [line.words[index].text for index in valid_indices]
                    )
                )
        return "\n".join(lines)

    def line_words(self, line_index: int) -> set[Tuple[int, int]]:
        if not 0 <= line_index < len(self.document.lines):
            return set()
        return {
            (line_index, word_index)
            for word_index in range(len(self.document.lines[line_index].words))
        }

    def all_words(self) -> set[Tuple[int, int]]:
        return {
            (line_index, word_index)
            for line_index, line in enumerate(self.document.lines)
            for word_index in range(len(line.words))
        }

    def text_for(self, selection: set[Tuple[int, int]]) -> str:
        grouped: dict[int, List[str]] = {}
        for line_index, word_index in sorted(selection):
            if not 0 <= line_index < len(self.document.lines):
                continue
            line = self.document.lines[line_index]
            if not 0 <= word_index < len(line.words):
                continue
            grouped.setdefault(line_index, []).append(line.words[word_index].text)
        return "\n".join(
            OcrTextFormatter.join_words(grouped[line_index])
            for line_index in sorted(grouped)
        )


class WindowsOcrBackend:
    """通过 Windows.Media.Ocr 提取文字和逐词像素坐标。"""

    MAX_IMAGE_DIMENSION = 10000
    POWERSHELL_SCRIPT = r'''
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]
$script:asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq "AsTask" -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1
})[0]
function Await-WinRt($operation, [Type]$resultType) {
    $task = $script:asTask.MakeGenericMethod($resultType).Invoke($null, @($operation))
    $task.Wait()
    return $task.Result
}
$path = [IO.Path]::GetFullPath($env:SCREENSHOT_OCR_IMAGE)
$tag = if ($env:SCREENSHOT_OCR_LANGUAGE) { $env:SCREENSHOT_OCR_LANGUAGE } else { "zh-CN" }
$language = [Windows.Globalization.Language]::new($tag)
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
if ($null -eq $engine) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
}
if ($null -eq $engine) {
    throw "No installed Windows OCR language is available."
}
$file = Await-WinRt ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
$stream = Await-WinRt ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await-WinRt ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await-WinRt ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$result = Await-WinRt ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$lines = @()
foreach ($line in $result.Lines) {
    $words = @()
    foreach ($word in $line.Words) {
        $rect = $word.BoundingRect
        $words += [PSCustomObject]@{
            text = $word.Text
            x = [double]$rect.X
            y = [double]$rect.Y
            width = [double]$rect.Width
            height = [double]$rect.Height
        }
    }
    $lines += [PSCustomObject]@{ text = $line.Text; words = @($words) }
}
[PSCustomObject]@{
    text = $result.Text
    lines = @($lines)
    maxImageDimension = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension
} | ConvertTo-Json -Depth 6 -Compress
$bitmap.Dispose()
$stream.Dispose()
'''

    def __init__(self, language: str = "zh-CN", timeout_seconds: int = 60) -> None:
        self.language = language
        self.timeout_seconds = max(5, int(timeout_seconds))

    def recognize(self, image: Image.Image) -> OcrDocument:
        if platform.system() != "Windows":
            raise RuntimeError("图片文字识别目前仅支持 Windows。")
        source = image.convert("RGB")
        prepared = self._prepare_image(source)
        temporary_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="screenshot_ocr_",
                suffix=".png",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                prepared.save(temporary, "PNG")
            payload = self._run_powershell(temporary_path)
            return self.parse_result(payload, source.size, prepared.size)
        finally:
            if temporary_path:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _prepare_image(self, image: Image.Image) -> Image.Image:
        longest = max(image.width, image.height)
        if longest <= self.MAX_IMAGE_DIMENSION:
            return image
        scale = self.MAX_IMAGE_DIMENSION / longest
        size = (
            max(1, int(round(image.width * scale))),
            max(1, int(round(image.height * scale))),
        )
        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        return image.resize(size, resampling)

    def _run_powershell(self, image_path: Path) -> dict:
        encoded = base64.b64encode(
            self.POWERSHELL_SCRIPT.encode("utf-16-le")
        ).decode("ascii")
        environment = os.environ.copy()
        environment["SCREENSHOT_OCR_IMAGE"] = str(image_path.resolve())
        environment["SCREENSHOT_OCR_LANGUAGE"] = self.language
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(detail or "Windows OCR 调用失败。")
        json_line = next(
            (
                line.lstrip("\ufeff")
                for line in reversed(completed.stdout.splitlines())
                if line.lstrip("\ufeff").startswith("{")
            ),
            "",
        )
        if not json_line:
            raise RuntimeError("Windows OCR 没有返回识别结果。")
        try:
            value = json.loads(json_line)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Windows OCR 返回了无效数据。") from exc
        if not isinstance(value, dict):
            raise RuntimeError("Windows OCR 返回的数据格式不正确。")
        return value

    @classmethod
    def parse_result(
        cls,
        payload: dict,
        source_size: Tuple[int, int],
        processed_size: Tuple[int, int],
    ) -> OcrDocument:
        scale_x = source_size[0] / max(1, processed_size[0])
        scale_y = source_size[1] / max(1, processed_size[1])
        raw_lines = payload.get("lines", [])
        if isinstance(raw_lines, dict):
            raw_lines = [raw_lines]
        lines: List[OcrLineResult] = []
        for raw_line in raw_lines if isinstance(raw_lines, list) else []:
            if not isinstance(raw_line, dict):
                continue
            raw_words = raw_line.get("words", [])
            if isinstance(raw_words, dict):
                raw_words = [raw_words]
            words: List[OcrWordResult] = []
            for raw_word in raw_words if isinstance(raw_words, list) else []:
                if not isinstance(raw_word, dict):
                    continue
                text = str(raw_word.get("text", "")).strip()
                if not text:
                    continue
                try:
                    left = int(round(float(raw_word.get("x", 0)) * scale_x))
                    top = int(round(float(raw_word.get("y", 0)) * scale_y))
                    right = int(
                        round(
                            (float(raw_word.get("x", 0)) + float(raw_word.get("width", 0)))
                            * scale_x
                        )
                    )
                    bottom = int(
                        round(
                            (float(raw_word.get("y", 0)) + float(raw_word.get("height", 0)))
                            * scale_y
                        )
                    )
                except (TypeError, ValueError):
                    continue
                box = (
                    max(0, min(source_size[0], left)),
                    max(0, min(source_size[1], top)),
                    max(0, min(source_size[0], right)),
                    max(0, min(source_size[1], bottom)),
                )
                words.append(OcrWordResult(text, box))
            if not words:
                continue
            line_text = OcrTextFormatter.normalize_line(raw_line.get("text", ""))
            if not line_text:
                line_text = OcrTextFormatter.join_words([word.text for word in words])
            lines.append(OcrLineResult(line_text, tuple(words)))
        text = "\n".join(line.text for line in lines)
        return OcrDocument(text, tuple(lines), source_size)


class ScreenshotManager:
    """集中处理截图、裁剪、保存和剪贴板复制。"""

    def __init__(self, settings: ScreenshotSettings) -> None:
        self.settings = settings

    def capture_screen(self) -> Image.Image:
        return self.capture_snapshot().image

    def capture_snapshot(self) -> ScreenSnapshot:
        if platform.system() == "Windows":
            image = ImageGrab.grab(all_screens=True).convert("RGB")
        else:
            image = ImageGrab.grab().convert("RGB")

        geometry = VirtualDesktopGeometry.detect(image.size)
        if geometry.size != image.size:
            geometry = VirtualDesktopGeometry(
                geometry.left,
                geometry.top,
                image.width,
                image.height,
            )
        return ScreenSnapshot(image, geometry)

    @staticmethod
    def grab_box(box: Box) -> Image.Image:
        normalized = ScreenshotManager._normalize_box(box)
        if platform.system() == "Windows":
            return ImageGrab.grab(bbox=normalized, all_screens=True).convert("RGB")
        return ImageGrab.grab(bbox=normalized).convert("RGB")

    def crop(self, image: Image.Image, box: Box) -> Image.Image:
        left, top, right, bottom = self._normalize_box(box)
        if right - left < 2 or bottom - top < 2:
            raise ValueError("截图区域太小，请重新框选。")
        return image.crop((left, top, right, bottom))

    def save(self, image: Image.Image, target: Optional[Path] = None) -> Path:
        self.settings.output_dir.mkdir(parents=True, exist_ok=True)

        if target is None:
            target = OutputNamePolicy.next_target(
                self.settings.output_dir,
                self.settings.filename_pattern,
                self.settings.image_format,
            )
        else:
            target = Path(target)
            if not target.suffix:
                target = target.with_suffix(
                    OutputNamePolicy.FORMAT_EXTENSIONS[
                        OutputNamePolicy.normalize_format(self.settings.image_format)
                    ]
                )
            target.parent.mkdir(parents=True, exist_ok=True)

        image_format = OutputNamePolicy.EXTENSION_FORMATS.get(
            target.suffix.lower(),
            OutputNamePolicy.normalize_format(self.settings.image_format),
        )
        save_options = {}
        if image_format == "JPEG":
            save_options["quality"] = max(1, min(100, int(self.settings.image_quality)))
            save_options["optimize"] = True
        image.convert("RGB").save(target, image_format, **save_options)
        return target

    def copy_to_clipboard(self, image: Image.Image) -> None:
        ClipboardHelper.copy_image(image)

    @staticmethod
    def _normalize_box(box: Box) -> Box:
        x1, y1, x2, y2 = box
        left, right = sorted((int(x1), int(x2)))
        top, bottom = sorted((int(y1), int(y2)))
        return left, top, right, bottom


class ClipboardHelper:
    """Windows 图片剪贴板写入器。"""

    CF_DIB = 8
    GMEM_MOVEABLE = 0x0002

    @classmethod
    def copy_image(cls, image: Image.Image) -> None:
        if platform.system() != "Windows":
            raise RuntimeError("当前剪贴板复制图片功能仅支持 Windows。")

        bmp_bytes = cls._to_dib_bytes(image)
        cls._write_windows_clipboard(bmp_bytes)

    @staticmethod
    def _to_dib_bytes(image: Image.Image) -> bytes:
        output = io.BytesIO()
        image.convert("RGB").save(output, "BMP")
        data = output.getvalue()
        return data[14:]

    @classmethod
    def _write_windows_clipboard(cls, data: bytes) -> None:
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32

        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.restype = ctypes.c_int
        kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
        kernel32.GlobalFree.restype = ctypes.c_void_p

        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.OpenClipboard.restype = ctypes.c_int
        user32.EmptyClipboard.argtypes = []
        user32.EmptyClipboard.restype = ctypes.c_int
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = ctypes.c_int

        handle = kernel32.GlobalAlloc(cls.GMEM_MOVEABLE, len(data))
        if not handle:
            raise RuntimeError("申请剪贴板内存失败。")

        locked = kernel32.GlobalLock(handle)
        if not locked:
            kernel32.GlobalFree(handle)
            raise RuntimeError("锁定剪贴板内存失败。")

        ctypes.memmove(locked, data, len(data))
        kernel32.GlobalUnlock(handle)

        if not user32.OpenClipboard(None):
            kernel32.GlobalFree(handle)
            raise RuntimeError("无法打开系统剪贴板，请稍后再试。")

        try:
            user32.EmptyClipboard()
            if not user32.SetClipboardData(cls.CF_DIB, handle):
                raise RuntimeError("写入系统剪贴板失败。")
            handle = None
        finally:
            user32.CloseClipboard()
            if handle:
                kernel32.GlobalFree(handle)


@dataclass
class ToolbarItem:
    command: str
    tooltip: str
    kind: str = "action"


class CaptureActionPolicy:
    """统一截图完成动作的复制语义。"""

    COPY_ACTIONS = frozenset({"copy", "finish"})

    @classmethod
    def copies_to_clipboard(cls, action: str) -> bool:
        return action in cls.COPY_ACTIONS


class WindowsPhysicalKeyMonitor:
    """轮询 Windows 物理按键，避免字符键被输入法组合过程吞掉。"""

    POLL_INTERVAL_MS = 15
    VK_CONTROL = 0x11
    VK_MENU = 0x12
    VK_LWIN = 0x5B
    VK_RWIN = 0x5C
    BLOCKING_KEYS = (VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN)

    def __init__(
        self,
        widget: tk.Misc,
        virtual_key: int,
        callback: Callable[[], object],
        active_when: Optional[Callable[[], bool]] = None,
        key_state: Optional[Callable[[int], bool]] = None,
    ) -> None:
        self.widget = widget
        self.virtual_key = int(virtual_key)
        self.callback = callback
        self.active_when = active_when or self._focus_is_within_widget
        self.key_state = key_state or self._windows_key_down
        self._uses_windows_state = key_state is None
        self._was_down = False
        self._running = False
        self._job: Optional[str] = None

    def start(self) -> bool:
        if self._running:
            return True
        if self._uses_windows_state and platform.system() != "Windows":
            return False
        try:
            self._was_down = bool(self.key_state(self.virtual_key))
            self._running = True
            self._schedule()
        except (AttributeError, OSError, tk.TclError):
            self._running = False
            self._job = None
            return False
        return True

    def stop(self) -> None:
        self._running = False
        if not self._job:
            return
        try:
            self.widget.after_cancel(self._job)
        except tk.TclError:
            pass
        self._job = None

    def poll_once(self) -> bool:
        is_down = bool(self.key_state(self.virtual_key))
        is_new_press = is_down and not self._was_down
        self._was_down = is_down
        if not is_new_press:
            return False
        if not self.active_when():
            return False
        if any(self.key_state(key) for key in self.BLOCKING_KEYS):
            return False
        self.callback()
        return True

    def _schedule(self) -> None:
        self._job = self.widget.after(self.POLL_INTERVAL_MS, self._poll)

    def _poll(self) -> None:
        self._job = None
        if not self._running:
            return
        try:
            self.poll_once()
        except (AttributeError, OSError, tk.TclError):
            self._running = False
            return
        if self._running:
            self._schedule()

    def _focus_is_within_widget(self) -> bool:
        try:
            focused = self.widget.focus_displayof()
        except tk.TclError:
            return False
        if focused is None:
            return False
        widget_path = str(self.widget)
        focused_path = str(focused)
        return focused_path == widget_path or focused_path.startswith(f"{widget_path}.")

    @staticmethod
    def _windows_key_down(virtual_key: int) -> bool:
        return bool(ctypes.windll.user32.GetAsyncKeyState(int(virtual_key)) & 0x8000)


@dataclass
class OverlayButton:
    item: ToolbarItem
    bounds: Box
    enabled: bool = True


@dataclass
class Annotation:
    tool: str
    points: List[Point]
    text: str = ""
    color: str = "#ff3b30"
    width: int = 3
    font_size: int = 22
    canvas_ids: List[int] = field(default_factory=list)


class AnnotationRenderer:
    """把覆盖层里的标注合成到最终截图上。"""

    @classmethod
    def render(
        cls, screen_image: Image.Image, box: Box, annotations: List[Annotation]
    ) -> Image.Image:
        left, top, right, bottom = ScreenshotManager._normalize_box(box)
        image = screen_image.crop((left, top, right, bottom)).convert("RGB")
        draw = ImageDraw.Draw(image)

        for annotation in annotations:
            points = [(x - left, y - top) for x, y in annotation.points]
            if annotation.tool == "rect" and len(points) >= 2:
                shape_box = ScreenshotManager._normalize_box(
                    (points[0][0], points[0][1], points[1][0], points[1][1])
                )
                draw.rectangle(shape_box, outline=annotation.color, width=annotation.width)
            elif annotation.tool == "ellipse" and len(points) >= 2:
                shape_box = ScreenshotManager._normalize_box(
                    (points[0][0], points[0][1], points[1][0], points[1][1])
                )
                draw.ellipse(shape_box, outline=annotation.color, width=annotation.width)
            elif annotation.tool == "arrow" and len(points) >= 2:
                cls._draw_arrow(draw, points[0], points[-1], annotation.color, annotation.width)
            elif annotation.tool == "line" and len(points) >= 2:
                draw.line([points[0], points[-1]], fill=annotation.color, width=annotation.width)
            elif annotation.tool == "marker" and len(points) >= 2:
                overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
                overlay_draw = ImageDraw.Draw(overlay)
                marker_color = cls._rgba(annotation.color, 105)
                overlay_draw.line(
                    points,
                    fill=marker_color,
                    width=max(8, annotation.width * 4),
                    joint="curve",
                )
                image.paste(Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB"))
            elif annotation.tool == "pen" and len(points) >= 2:
                draw.line(points, fill=annotation.color, width=annotation.width, joint="curve")
            elif annotation.tool == "mosaic" and len(points) >= 2:
                cls._apply_mosaic(image, points[:2], block_size=max(6, annotation.width * 3))
            elif annotation.tool == "blur" and len(points) >= 2:
                cls._apply_blur(image, points[:2], radius=max(4, annotation.width * 2))
            elif annotation.tool == "text" and points and annotation.text:
                font = cls._load_font(annotation.font_size)
                x, y = points[0]
                max_width = None
                if len(points) >= 2:
                    max_width = max(20, points[1][0] - points[0][0])
                text = cls._wrap_text_to_width(draw, annotation.text, font, max_width)
                draw.multiline_text(
                    (x, y),
                    text,
                    fill=annotation.color,
                    font=font,
                    spacing=4,
                )

        return image

    @staticmethod
    def _draw_arrow(
        draw: ImageDraw.ImageDraw,
        start: Point,
        end: Point,
        color: str,
        width: int,
    ) -> None:
        draw.line([start, end], fill=color, width=width)

        angle = math.atan2(end[1] - start[1], end[0] - start[0])
        head_length = max(12, width * 4)
        spread = math.pi / 7
        left = (
            end[0] - head_length * math.cos(angle - spread),
            end[1] - head_length * math.sin(angle - spread),
        )
        right = (
            end[0] - head_length * math.cos(angle + spread),
            end[1] - head_length * math.sin(angle + spread),
        )
        draw.polygon([end, left, right], fill=color)

    @staticmethod
    def _apply_mosaic(
        image: Image.Image, points: List[Point], block_size: int = 12
    ) -> None:
        left, top, right, bottom = ScreenshotManager._normalize_box(
            (points[0][0], points[0][1], points[1][0], points[1][1])
        )
        left = max(0, min(image.width, left))
        top = max(0, min(image.height, top))
        right = max(0, min(image.width, right))
        bottom = max(0, min(image.height, bottom))

        if right - left < 2 or bottom - top < 2:
            return

        region = image.crop((left, top, right, bottom))
        small_size = (
            max(1, region.width // block_size),
            max(1, region.height // block_size),
        )
        resampling = getattr(getattr(Image, "Resampling", Image), "BILINEAR")
        nearest = getattr(getattr(Image, "Resampling", Image), "NEAREST")
        small = region.resize(small_size, resampling)
        pixelated = small.resize(region.size, nearest)
        image.paste(pixelated, (left, top, right, bottom))

    @staticmethod
    def _apply_blur(
        image: Image.Image, points: List[Point], radius: int = 8
    ) -> None:
        left, top, right, bottom = ScreenshotManager._normalize_box(
            (points[0][0], points[0][1], points[1][0], points[1][1])
        )
        left = max(0, min(image.width, left))
        top = max(0, min(image.height, top))
        right = max(0, min(image.width, right))
        bottom = max(0, min(image.height, bottom))

        if right - left < 2 or bottom - top < 2:
            return

        region = image.crop((left, top, right, bottom))
        image.paste(region.filter(ImageFilter.GaussianBlur(radius=radius)), (left, top))

    @staticmethod
    def _rgba(color: str, alpha: int) -> Tuple[int, int, int, int]:
        value = color.strip().lstrip("#")
        if len(value) != 6:
            return 255, 59, 48, alpha
        return (
            int(value[0:2], 16),
            int(value[2:4], 16),
            int(value[4:6], 16),
            alpha,
        )

    @classmethod
    def _wrap_text_to_width(
        cls,
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.ImageFont,
        max_width: Optional[int],
    ) -> str:
        if not max_width:
            return text

        wrapped_lines: List[str] = []
        for paragraph in text.splitlines() or [""]:
            line = ""
            for char in paragraph:
                candidate = f"{line}{char}"
                if line and cls._text_width(draw, candidate, font) > max_width:
                    wrapped_lines.append(line)
                    line = char
                else:
                    line = candidate
            wrapped_lines.append(line)
        return "\n".join(wrapped_lines)

    @staticmethod
    def _text_width(
        draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont
    ) -> int:
        if hasattr(draw, "textlength"):
            return int(draw.textlength(text, font=font))
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]

    @staticmethod
    def _load_font(size: int) -> ImageFont.ImageFont:
        font_names = ["msyh.ttc", "simhei.ttf", "arial.ttf"]
        windir = Path(os.environ.get("WINDIR", "C:/Windows"))

        for font_name in font_names:
            font_path = windir / "Fonts" / font_name
            if font_path.exists():
                return ImageFont.truetype(str(font_path), size)

        return ImageFont.load_default()


class SelectionTracker:
    """参考 MFC CRectTracker 的思路，管理选区、命中测试、移动和缩放。"""

    HANDLE_SIZE = 8
    HANDLE_MARGIN = 5
    HANDLE_NAMES = ("nw", "n", "ne", "e", "se", "s", "sw", "w")

    def __init__(self, screen_width: int, screen_height: int, min_size: int) -> None:
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.min_size = min_size
        self.box: Optional[Box] = None

    def set_from_points(self, start: Point, end: Point) -> bool:
        box = self._clip_box(ScreenshotManager._normalize_box((*start, *end)))
        if self._too_small(box):
            self.box = None
            return False

        self.box = box
        return True

    def set_box(self, box: Box) -> None:
        self.box = self._clip_box(ScreenshotManager._normalize_box(box))

    def clear(self) -> None:
        self.box = None

    def hit_test(self, point: Point) -> Optional[str]:
        if not self.box:
            return None

        x, y = point
        for handle_name, bounds in self.handle_bounds().items():
            left, top, right, bottom = bounds
            if left <= x <= right and top <= y <= bottom:
                return handle_name

        left, top, right, bottom = self.box
        if left <= x <= right and top <= y <= bottom:
            return "move"

        return None

    def handle_bounds(self) -> dict[str, Box]:
        return {
            name: (
                center[0] - self.HANDLE_SIZE // 2 - self.HANDLE_MARGIN,
                center[1] - self.HANDLE_SIZE // 2 - self.HANDLE_MARGIN,
                center[0] + self.HANDLE_SIZE // 2 + self.HANDLE_MARGIN,
                center[1] + self.HANDLE_SIZE // 2 + self.HANDLE_MARGIN,
            )
            for name, center in self.handle_centers().items()
        }

    def handle_centers(self) -> dict[str, Point]:
        if not self.box:
            return {}

        left, top, right, bottom = self.box
        center_x = left + (right - left) // 2
        center_y = top + (bottom - top) // 2
        return {
            "nw": (left, top),
            "n": (center_x, top),
            "ne": (right, top),
            "e": (right, center_y),
            "se": (right, bottom),
            "s": (center_x, bottom),
            "sw": (left, bottom),
            "w": (left, center_y),
        }

    def move_from_drag(self, original_box: Box, drag_start: Point, point: Point) -> Tuple[Box, int, int]:
        left, top, right, bottom = original_box
        width = right - left
        height = bottom - top
        requested_dx = point[0] - drag_start[0]
        requested_dy = point[1] - drag_start[1]

        new_left = self._clamp(left + requested_dx, 0, self.screen_width - width)
        new_top = self._clamp(top + requested_dy, 0, self.screen_height - height)
        moved_box = (new_left, new_top, new_left + width, new_top + height)
        self.box = moved_box
        return moved_box, new_left - left, new_top - top

    def resize_from_drag(self, handle_name: str, original_box: Box, drag_start: Point, point: Point) -> Box:
        left, top, right, bottom = original_box
        dx = point[0] - drag_start[0]
        dy = point[1] - drag_start[1]

        if "w" in handle_name:
            left = self._clamp(left + dx, 0, right - self.min_size)
        if "e" in handle_name:
            right = self._clamp(right + dx, left + self.min_size, self.screen_width)
        if "n" in handle_name:
            top = self._clamp(top + dy, 0, bottom - self.min_size)
        if "s" in handle_name:
            bottom = self._clamp(bottom + dy, top + self.min_size, self.screen_height)

        resized_box = (left, top, right, bottom)
        self.box = resized_box
        return resized_box

    def cursor_for_hit(self, hit: Optional[str], active_tool: Optional[str]) -> str:
        if active_tool:
            return "xterm" if active_tool == "text" else "crosshair"
        if hit == "move":
            return "fleur"
        if hit in {"n", "s"}:
            return "sb_v_double_arrow"
        if hit in {"e", "w"}:
            return "sb_h_double_arrow"
        if hit in {"nw", "ne", "se", "sw"}:
            return "sizing"
        return "crosshair"

    def _clip_box(self, box: Box) -> Box:
        left, top, right, bottom = box
        return (
            self._clamp(left, 0, self.screen_width),
            self._clamp(top, 0, self.screen_height),
            self._clamp(right, 0, self.screen_width),
            self._clamp(bottom, 0, self.screen_height),
        )

    def _too_small(self, box: Box) -> bool:
        left, top, right, bottom = box
        return right - left < self.min_size or bottom - top < self.min_size

    @staticmethod
    def _clamp(value: int, minimum: int, maximum: int) -> int:
        if maximum < minimum:
            return minimum
        return max(minimum, min(maximum, int(value)))


@dataclass(frozen=True)
class WindowCandidate:
    """截图开始前缓存的一个可见顶层窗口。"""

    hwnd: int
    box: Box


class WindowCandidateDetector:
    """用 Win32 窗口层级为框选提供窗口/原生控件候选框。"""

    CWP_SKIPINVISIBLE = 0x0001
    CWP_SKIPDISABLED = 0x0002
    CWP_SKIPTRANSPARENT = 0x0004
    DWMWA_EXTENDED_FRAME_BOUNDS = 9
    DWMWA_CLOAKED = 14

    def __init__(
        self,
        screen_size: Tuple[int, int],
        screen_origin: Point = (0, 0),
    ) -> None:
        self.screen_box: Box = (0, 0, screen_size[0], screen_size[1])
        self.geometry = VirtualDesktopGeometry(
            int(screen_origin[0]),
            int(screen_origin[1]),
            int(screen_size[0]),
            int(screen_size[1]),
        )
        self.windows: List[WindowCandidate] = []
        if platform.system() == "Windows":
            self.windows = self._snapshot_windows()

    def candidates_at(self, point: Point) -> List[Box]:
        """返回鼠标下从最深原生控件到顶层窗口的候选矩形。"""
        if platform.system() != "Windows":
            return [self.screen_box]

        top_level = next(
            (candidate for candidate in self.windows if self._contains(candidate.box, point)),
            None,
        )
        if top_level is None:
            return [self.screen_box]

        hierarchy = self._child_hierarchy(top_level.hwnd, point)
        boxes: List[Box] = []
        for hwnd in reversed(hierarchy):
            box = self._window_box(hwnd)
            if box and self._contains(box, point) and box not in boxes:
                boxes.append(box)

        if top_level.box not in boxes:
            boxes.append(top_level.box)
        if self.screen_box not in boxes:
            boxes.append(self.screen_box)
        return boxes

    def _snapshot_windows(self) -> List[WindowCandidate]:
        user32 = ctypes.windll.user32
        callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_bool,
            ctypes.wintypes.HWND,
            ctypes.wintypes.LPARAM,
        )
        user32.EnumWindows.argtypes = [callback_type, ctypes.wintypes.LPARAM]
        user32.EnumWindows.restype = ctypes.wintypes.BOOL
        user32.IsWindowVisible.argtypes = [ctypes.wintypes.HWND]
        user32.IsWindowVisible.restype = ctypes.wintypes.BOOL
        user32.IsIconic.argtypes = [ctypes.wintypes.HWND]
        user32.IsIconic.restype = ctypes.wintypes.BOOL
        windows: List[WindowCandidate] = []

        @callback_type
        def collect(hwnd: int, lparam: int) -> bool:
            del lparam
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            if self._is_cloaked(hwnd):
                return True
            box = self._window_box(hwnd)
            if not box or not self._intersects_screen(box):
                return True
            left, top, right, bottom = box
            if right - left >= 5 and bottom - top >= 5:
                windows.append(WindowCandidate(int(hwnd), box))
            return True

        user32.EnumWindows(collect, 0)
        return windows

    def _child_hierarchy(self, hwnd: int, point: Point) -> List[int]:
        user32 = ctypes.windll.user32
        user32.ScreenToClient.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.POINTER(ctypes.wintypes.POINT),
        ]
        user32.ScreenToClient.restype = ctypes.wintypes.BOOL
        user32.ChildWindowFromPointEx.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.wintypes.POINT,
            ctypes.wintypes.UINT,
        ]
        user32.ChildWindowFromPointEx.restype = ctypes.wintypes.HWND
        hierarchy = [hwnd]
        parent = hwnd
        flags = (
            self.CWP_SKIPINVISIBLE
            | self.CWP_SKIPDISABLED
            | self.CWP_SKIPTRANSPARENT
        )

        absolute_point = self.geometry.to_absolute_point(point)
        for _ in range(12):
            client_point = ctypes.wintypes.POINT(*absolute_point)
            if not user32.ScreenToClient(parent, ctypes.byref(client_point)):
                break
            child = user32.ChildWindowFromPointEx(parent, client_point, flags)
            if not child or int(child) == int(parent):
                break
            child_box = self._window_box(int(child))
            if not child_box or not self._contains(child_box, point):
                break
            hierarchy.append(int(child))
            parent = int(child)
        return hierarchy

    def _window_box(self, hwnd: int) -> Optional[Box]:
        rect = ctypes.wintypes.RECT()
        resolved = False
        try:
            dwmapi = ctypes.windll.dwmapi
            dwmapi.DwmGetWindowAttribute.argtypes = [
                ctypes.wintypes.HWND,
                ctypes.wintypes.DWORD,
                ctypes.c_void_p,
                ctypes.wintypes.DWORD,
            ]
            dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
            resolved = (
                dwmapi.DwmGetWindowAttribute(
                    ctypes.wintypes.HWND(hwnd),
                    self.DWMWA_EXTENDED_FRAME_BOUNDS,
                    ctypes.byref(rect),
                    ctypes.sizeof(rect),
                )
                == 0
            )
        except Exception:
            resolved = False

        if not resolved:
            user32 = ctypes.windll.user32
            user32.GetWindowRect.argtypes = [
                ctypes.wintypes.HWND,
                ctypes.POINTER(ctypes.wintypes.RECT),
            ]
            user32.GetWindowRect.restype = ctypes.wintypes.BOOL
            if not user32.GetWindowRect(ctypes.wintypes.HWND(hwnd), ctypes.byref(rect)):
                return None

        screen_left, screen_top, screen_right, screen_bottom = self.geometry.bounds
        left = max(screen_left, int(rect.left))
        top = max(screen_top, int(rect.top))
        right = min(screen_right, int(rect.right))
        bottom = min(screen_bottom, int(rect.bottom))
        if right - left < 2 or bottom - top < 2:
            return None
        return self.geometry.to_local_box((left, top, right, bottom))

    def _is_cloaked(self, hwnd: int) -> bool:
        try:
            cloaked = ctypes.wintypes.DWORD()
            dwmapi = ctypes.windll.dwmapi
            dwmapi.DwmGetWindowAttribute.argtypes = [
                ctypes.wintypes.HWND,
                ctypes.wintypes.DWORD,
                ctypes.c_void_p,
                ctypes.wintypes.DWORD,
            ]
            dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
            result = dwmapi.DwmGetWindowAttribute(
                ctypes.wintypes.HWND(hwnd),
                self.DWMWA_CLOAKED,
                ctypes.byref(cloaked),
                ctypes.sizeof(cloaked),
            )
            return result == 0 and bool(cloaked.value)
        except Exception:
            return False

    def _intersects_screen(self, box: Box) -> bool:
        left, top, right, bottom = box
        screen_left, screen_top, screen_right, screen_bottom = self.screen_box
        return not (
            right <= screen_left
            or bottom <= screen_top
            or left >= screen_right
            or top >= screen_bottom
        )

    @staticmethod
    def _contains(box: Box, point: Point) -> bool:
        left, top, right, bottom = box
        return left <= point[0] < right and top <= point[1] < bottom


class CaptureMagnifier:
    """在截图覆盖层绘制像素级放大镜，不创建额外窗口。"""

    PREVIEW_WIDTH = 272
    PREVIEW_HEIGHT = 176
    ZOOM_SAMPLES = (
        (25, 17),
        (21, 13),
        (17, 11),
        (15, 9),
        (13, 9),
        (11, 7),
        (9, 7),
    )
    DEFAULT_ZOOM_INDEX = 2
    BORDER = 2
    FOOTER_HEIGHT = 140
    OFFSET = 24
    SCREEN_MARGIN = 8

    def __init__(self) -> None:
        self.photo: Optional[ImageTk.PhotoImage] = None
        self.last_bounds: Optional[Box] = None
        self.zoom_index = self.DEFAULT_ZOOM_INDEX

    def adjust_zoom(self, direction: int) -> bool:
        next_index = max(
            0,
            min(len(self.ZOOM_SAMPLES) - 1, self.zoom_index + int(direction)),
        )
        if next_index == self.zoom_index:
            return False
        self.zoom_index = next_index
        return True

    @property
    def zoom_text(self) -> str:
        columns, _ = self.ZOOM_SAMPLES[self.zoom_index]
        ratio = self.PREVIEW_WIDTH / max(1, columns)
        return f"{ratio:.0f}×"

    def draw(
        self,
        canvas: tk.Canvas,
        image: Image.Image,
        point: Point,
        color_format: str,
        screen_origin: Point = (0, 0),
        tag: str = "pointer_info",
        notice: str = "",
    ) -> None:
        x = max(0, min(image.width - 1, int(point[0])))
        y = max(0, min(image.height - 1, int(point[1])))
        sample_columns, sample_rows = self.ZOOM_SAMPLES[self.zoom_index]
        left = max(
            0,
            min(image.width - sample_columns, x - sample_columns // 2),
        )
        top = max(
            0,
            min(image.height - sample_rows, y - sample_rows // 2),
        )
        right = min(image.width, left + sample_columns)
        bottom = min(image.height, top + sample_rows)

        crop = image.crop((left, top, right, bottom)).convert("RGB")
        nearest = getattr(getattr(Image, "Resampling", Image), "NEAREST")
        zoom_width = self.PREVIEW_WIDTH
        zoom_height = self.PREVIEW_HEIGHT
        crop = crop.resize((zoom_width, zoom_height), nearest)
        self.photo = ImageTk.PhotoImage(crop)

        panel_width = zoom_width + self.BORDER * 2
        panel_height = zoom_height + self.FOOTER_HEIGHT + self.BORDER * 2
        panel_x = x + self.OFFSET
        panel_y = y + self.OFFSET
        canvas_width = max(1, int(canvas.winfo_width()))
        canvas_height = max(1, int(canvas.winfo_height()))
        if panel_x + panel_width + self.SCREEN_MARGIN > canvas_width:
            panel_x = x - panel_width - self.OFFSET
        if panel_y + panel_height + self.SCREEN_MARGIN > canvas_height:
            panel_y = y - panel_height - self.OFFSET
        panel_x = max(
            self.SCREEN_MARGIN,
            min(panel_x, max(self.SCREEN_MARGIN, canvas_width - panel_width - self.SCREEN_MARGIN)),
        )
        panel_y = max(
            self.SCREEN_MARGIN,
            min(panel_y, max(self.SCREEN_MARGIN, canvas_height - panel_height - self.SCREEN_MARGIN)),
        )
        self.last_bounds = (
            panel_x,
            panel_y,
            panel_x + panel_width,
            panel_y + panel_height,
        )

        canvas.create_rectangle(
            self.last_bounds,
            fill="#111820",
            outline="#101010",
            width=self.BORDER,
            tags=(tag,),
        )
        image_x = panel_x + self.BORDER
        image_y = panel_y + self.BORDER
        canvas.create_image(
            image_x,
            image_y,
            image=self.photo,
            anchor=tk.NW,
            tags=(tag,),
        )

        rendered_pixel_width = zoom_width / max(1, right - left)
        rendered_pixel_height = zoom_height / max(1, bottom - top)
        center_x = image_x + int(round((x - left + 0.5) * rendered_pixel_width))
        center_y = image_y + int(round((y - top + 0.5) * rendered_pixel_height))
        canvas.create_line(
            image_x,
            center_y,
            image_x + zoom_width,
            center_y,
            fill="#19b5d1",
            width=2,
            tags=(tag,),
        )
        canvas.create_line(
            center_x,
            image_y,
            center_x,
            image_y + zoom_height,
            fill="#19b5d1",
            width=2,
            tags=(tag,),
        )
        cell_width = max(4, int(round(rendered_pixel_width)))
        cell_height = max(4, int(round(rendered_pixel_height)))
        cell_x = center_x - cell_width // 2
        cell_y = center_y - cell_height // 2
        canvas.create_rectangle(
            cell_x,
            cell_y,
            cell_x + cell_width,
            cell_y + cell_height,
            outline="#111111",
            width=3,
            tags=(tag,),
        )
        canvas.create_rectangle(
            cell_x + 2,
            cell_y + 2,
            cell_x + cell_width - 2,
            cell_y + cell_height - 2,
            outline="#ffffff",
            width=1,
            tags=(tag,),
        )

        red, green, blue = image.getpixel((x, y))[:3]
        rgb_text = f"{red}, {green}, {blue}"
        hex_text = f"#{red:02X}{green:02X}{blue:02X}"
        if color_format == "rgb":
            primary_color_text = rgb_text
            secondary_color_text = hex_text
        else:
            primary_color_text = hex_text
            secondary_color_text = f"RGB {rgb_text}"
        footer_top = image_y + zoom_height
        canvas.create_line(
            image_x,
            footer_top,
            image_x + zoom_width,
            footer_top,
            fill="#31414d",
            width=2,
            tags=(tag,),
        )
        canvas.create_text(
            panel_x + panel_width // 2,
            footer_top + 10,
            anchor=tk.N,
            text=f"({x + screen_origin[0]} , {y + screen_origin[1]})",
            fill="#ffffff",
            font=("Microsoft YaHei UI", 16, "bold"),
            tags=(tag,),
        )
        swatch_left = image_x + 26
        swatch_top = footer_top + 42
        canvas.create_rectangle(
            swatch_left,
            swatch_top,
            swatch_left + 30,
            swatch_top + 30,
            fill=f"#{red:02x}{green:02x}{blue:02x}",
            outline="#ffffff",
            width=2,
            tags=(tag,),
        )
        canvas.create_text(
            swatch_left + 42,
            swatch_top - 2,
            anchor=tk.NW,
            text=primary_color_text,
            fill="#ffffff",
            font=("Microsoft YaHei UI", 14, "bold"),
            tags=(tag,),
        )
        canvas.create_text(
            swatch_left + 42,
            swatch_top + 20,
            anchor=tk.NW,
            text=secondary_color_text,
            fill="#9fb2bf",
            font=("Microsoft YaHei UI", 9),
            tags=(tag,),
        )
        segment_y = footer_top + 84
        segment_width = 64
        segment_height = 27
        segment_left = panel_x + (panel_width - segment_width * 2) // 2
        for index, mode in enumerate(("rgb", "hex")):
            selected = color_format == mode
            left_x = segment_left + index * segment_width
            canvas.create_rectangle(
                left_x,
                segment_y,
                left_x + segment_width,
                segment_y + segment_height,
                fill="#0b93bd" if selected else "#1d2a33",
                outline="#dbe9ef" if selected else "#60727e",
                width=1,
                tags=(tag,),
            )
            canvas.create_text(
                left_x + segment_width // 2,
                segment_y + segment_height // 2,
                text=mode.upper(),
                fill="#ffffff" if selected else "#aebdc6",
                font=("Segoe UI", 9, "bold"),
                tags=(tag,),
            )
        hint_y = footer_top + 126
        if notice:
            canvas.create_text(
                panel_x + panel_width // 2,
                hint_y,
                text=notice,
                fill="#6ee7a8",
                font=("Microsoft YaHei UI", 9),
                tags=(tag,),
            )
        else:
            self._draw_shortcut_hints(
                canvas,
                image_x,
                hint_y,
                zoom_width,
                tag,
            )
        cursor_radius = 18
        for width, color in ((6, "#111111"), (2, "#ffffff")):
            canvas.create_line(
                x - cursor_radius,
                y,
                x + cursor_radius,
                y,
                fill=color,
                width=width,
                capstyle=tk.ROUND,
                tags=(tag,),
            )
            canvas.create_line(
                x,
                y - cursor_radius,
                x,
                y + cursor_radius,
                fill=color,
                width=width,
                capstyle=tk.ROUND,
                tags=(tag,),
            )

    def _draw_shortcut_hints(
        self,
        canvas: tk.Canvas,
        left: int,
        center_y: int,
        width: int,
        tag: str,
    ) -> None:
        """用紧凑图标和键帽标明取色快捷操作。"""
        muted = "#91a8b6"
        key_fill = "#1d2a33"
        key_outline = "#60727e"

        copy_x = left + 14
        canvas.create_rectangle(
            copy_x + 4,
            center_y - 7,
            copy_x + 14,
            center_y + 4,
            outline=muted,
            width=1,
            tags=(tag,),
        )
        canvas.create_rectangle(
            copy_x,
            center_y - 3,
            copy_x + 10,
            center_y + 8,
            outline=muted,
            width=1,
            tags=(tag,),
        )
        self._draw_keycap(canvas, copy_x + 31, center_y, "C", tag, key_fill, key_outline)

        canvas.create_text(
            left + width // 2,
            center_y,
            text=self.zoom_text,
            fill=muted,
            font=("Segoe UI", 9, "bold"),
            tags=(tag,),
        )

        shift_x = left + width - 54
        self._draw_keycap(
            canvas,
            shift_x,
            center_y,
            "Shift",
            tag,
            key_fill,
            key_outline,
            key_width=43,
        )
        swap_x = shift_x - 48
        canvas.create_line(
            swap_x,
            center_y - 4,
            swap_x + 22,
            center_y - 4,
            fill=muted,
            width=1,
            arrow=tk.LAST,
            tags=(tag,),
        )
        canvas.create_line(
            swap_x + 22,
            center_y + 4,
            swap_x,
            center_y + 4,
            fill=muted,
            width=1,
            arrow=tk.LAST,
            tags=(tag,),
        )

    @staticmethod
    def _draw_keycap(
        canvas: tk.Canvas,
        center_x: int,
        center_y: int,
        text: str,
        tag: str,
        fill: str,
        outline: str,
        key_width: int = 20,
    ) -> None:
        half_width = key_width // 2
        canvas.create_rectangle(
            center_x - half_width,
            center_y - 9,
            center_x + half_width,
            center_y + 9,
            fill=fill,
            outline=outline,
            width=1,
            tags=(tag,),
        )
        canvas.create_text(
            center_x,
            center_y,
            text=text,
            fill="#dbe9ef",
            font=("Segoe UI", 8, "bold"),
            tags=(tag,),
        )


class ToolbarIconFactory:
    """用高分辨率位图绘制工具栏图标，缩小后边缘比 Tk 原生线条更平滑。"""

    SCALE = 4

    @classmethod
    def make(
        cls,
        command: str,
        size: int,
        stroke: str,
        active_color: str,
        enabled: bool,
    ) -> Image.Image:
        canvas_size = size * cls.SCALE
        image = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        s = cls.SCALE
        disabled_color = "#aeb6c2"
        color = cls._rgba(stroke if enabled else disabled_color)
        accent = cls._rgba(active_color)
        red = cls._rgba("#d93025" if enabled else disabled_color)
        green = cls._rgba("#15803d" if enabled else disabled_color)

        def p(x: float, y: float) -> Tuple[int, int]:
            return int(round(x * s)), int(round(y * s))

        def line(points: List[Tuple[float, float]], fill=color, width: float = 1.7) -> None:
            draw.line(
                [p(x, y) for x, y in points],
                fill=fill,
                width=max(1, int(round(width * s))),
                joint="curve",
            )

        def closed(points: List[Tuple[float, float]], outline=color, width: float = 1.7, fill=None) -> None:
            scaled = [p(x, y) for x, y in points]
            if fill is not None:
                draw.polygon(scaled, fill=fill)
            draw.line(
                scaled + [scaled[0]],
                fill=outline,
                width=max(1, int(round(width * s))),
                joint="curve",
            )

        def rect(box: Tuple[float, float, float, float], outline=color, width: float = 1.7, fill=None) -> None:
            draw.rectangle(
                tuple(int(round(v * s)) for v in box),
                outline=outline,
                width=max(1, int(round(width * s))),
                fill=fill,
            )

        def oval(box: Tuple[float, float, float, float], outline=color, width: float = 1.7, fill=None) -> None:
            draw.ellipse(
                tuple(int(round(v * s)) for v in box),
                outline=outline,
                width=max(1, int(round(width * s))),
                fill=fill,
            )

        icon = command.split(":", 1)[0]
        if icon == "width":
            cls._draw_width_icon(draw, command, size, color, s)
        elif icon == "rect":
            rect((4.5, 4.5, 17.5, 17.5))
        elif icon == "ellipse":
            oval((3.5, 5, 18.5, 17))
        elif icon == "line":
            line([(3.5, 15.5), (8, 9), (12, 13), (18.5, 5.5)])
        elif icon == "arrow":
            line([(4, 18), (17.5, 4.5)])
            cls._arrow_head(draw, p(17.5, 4.5), p(10.5, 6), p(16, 11.5), color, 2 * s)
        elif icon == "pen":
            closed([(5, 15.5), (13, 6), (16.5, 9.5), (8, 19)], outline=color)
            line([(12.2, 7), (15.5, 10.3)])
            closed([(5, 15.5), (4, 20), (8, 19)], outline=color, width=1.4)
        elif icon == "marker":
            closed([(5, 14.5), (14, 5.5), (18.5, 10), (9.5, 19)], outline=color)
            line([(13, 7), (17, 11)])
            draw.rounded_rectangle(
                (4 * s, 18.5 * s, 13 * s, 20.5 * s),
                radius=s,
                fill=accent,
            )
        elif icon == "mosaic":
            cell = 4 * s
            gap = 1 * s
            start = 4 * s
            for row in range(3):
                for col in range(3):
                    x = start + col * (cell + gap)
                    y = start + row * (cell + gap)
                    fill = color if (row + col) % 2 == 0 else cls._rgba("#9ca3af")
                    draw.rectangle((x, y, x + cell, y + cell), fill=fill)
        elif icon == "blur":
            oval((4, 4, 18, 18))
            line([(7, 8), (15, 8)], width=1.2)
            line([(6, 12), (16, 12)], width=1.2)
            line([(8, 16), (14, 16)], width=1.2)
        elif icon == "text":
            line([(5, 5), (17, 5)])
            line([(11, 5), (11, 18)])
            line([(7.5, 18), (14.5, 18)])
        elif icon == "clear":
            cls._draw_eraser(draw, color, s)
        elif icon == "undo":
            cls._draw_undo(draw, size, color, s, reverse=False)
        elif icon == "redo":
            cls._draw_undo(draw, size, color, s, reverse=True)
        elif icon == "cancel":
            cls._draw_cancel(draw, red, s)
        elif icon == "pin":
            cls._draw_pin(draw, color, s)
        elif icon == "copy":
            rect((8, 4, 18, 14))
            rect((4, 8, 14, 18))
        elif icon == "save":
            closed([(4, 4), (16, 4), (19, 7), (19, 19), (4, 19)], outline=color)
            rect((7, 4, 15, 9), outline=color, width=1.4)
            rect((7, 13, 16, 19), outline=color, width=1.4)
            line([(13.5, 5.5), (13.5, 8)], width=1.2)
        elif icon == "finish":
            line([(4, 12), (9, 17), (18.5, 5.5)], fill=green, width=2.4)

        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        return image.resize((size, size), resample)

    @classmethod
    def _draw_width_icon(
        cls,
        draw: ImageDraw.ImageDraw,
        command: str,
        size: int,
        color: Tuple[int, int, int, int],
        scale: int,
    ) -> None:
        try:
            width = int(command.split(":", 1)[1])
        except (IndexError, ValueError):
            width = 2
        center = size // 2 * scale
        if width <= 2:
            radius = max(1, width) * scale
            draw.ellipse(
                (center - radius, center - radius, center + radius, center + radius),
                fill=color,
            )
            return
        draw.line(
            [(4 * scale, center), ((size - 4) * scale, center)],
            fill=color,
            width=width * scale,
        )

    @staticmethod
    def _draw_eraser(
        draw: ImageDraw.ImageDraw,
        color: Tuple[int, int, int, int],
        scale: int,
    ) -> None:
        def p(x: float, y: float) -> Tuple[int, int]:
            return int(round(x * scale)), int(round(y * scale))

        body = [p(4.5, 15), p(12.5, 6.5), p(18.5, 12.5), p(10.5, 20.5)]
        draw.line(body + [body[0]], fill=color, width=7, joint="curve")
        draw.line([p(9.5, 9.5), p(15.5, 15.5)], fill=color, width=7)
        draw.line([p(4, 20.5), p(18.5, 20.5)], fill=color, width=5)

    @classmethod
    def _draw_undo(
        cls,
        draw: ImageDraw.ImageDraw,
        size: int,
        color: Tuple[int, int, int, int],
        scale: int,
        reverse: bool,
    ) -> None:
        def sample_cubic(
            p0: Tuple[float, float],
            p1: Tuple[float, float],
            p2: Tuple[float, float],
            p3: Tuple[float, float],
            steps: int = 14,
        ) -> List[Tuple[float, float]]:
            points: List[Tuple[float, float]] = []
            for index in range(steps + 1):
                t = index / steps
                inv = 1 - t
                x = (
                    inv**3 * p0[0]
                    + 3 * inv**2 * t * p1[0]
                    + 3 * inv * t**2 * p2[0]
                    + t**3 * p3[0]
                )
                y = (
                    inv**3 * p0[1]
                    + 3 * inv**2 * t * p1[1]
                    + 3 * inv * t**2 * p2[1]
                    + t**3 * p3[1]
                )
                points.append((x, y))
            return points

        def mirror(points: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
            if not reverse:
                return points
            return [(size - x, y) for x, y in points]

        def scaled(points: List[Tuple[float, float]]) -> List[Tuple[int, int]]:
            return [
                (int(round(x * scale)), int(round(y * scale)))
                for x, y in mirror(points)
            ]

        curve = sample_cubic((18, 16.5), (19, 9), (14, 6), (8, 9.5))
        draw.line(scaled(curve), fill=color, width=2 * scale, joint="curve")
        draw.polygon(scaled([(6, 9.5), (11.5, 6), (10.5, 13)]), fill=color)

    @staticmethod
    def _draw_pin(
        draw: ImageDraw.ImageDraw,
        color: Tuple[int, int, int, int],
        scale: int,
    ) -> None:
        def p(x: float, y: float) -> Tuple[int, int]:
            return int(round(x * scale)), int(round(y * scale))

        draw.rounded_rectangle(
            (7 * scale, 3.5 * scale, 17 * scale, 7 * scale),
            radius=scale,
            fill=color,
        )
        draw.polygon(
            [
                p(9, 7),
                p(15, 7),
                p(14.5, 10.5),
                p(18.5, 14),
                p(13, 14),
                p(13, 19),
                p(11, 21),
                p(11, 14),
                p(5.5, 14),
                p(9.5, 10.5),
            ],
            fill=color,
        )

    @staticmethod
    def _draw_cancel(
        draw: ImageDraw.ImageDraw,
        color: Tuple[int, int, int, int],
        scale: int,
    ) -> None:
        def p(x: float, y: float) -> Tuple[int, int]:
            return int(round(x * scale)), int(round(y * scale))

        draw.line([p(7, 7), p(15, 15)], fill=color, width=2 * scale)
        draw.line([p(15, 7), p(7, 15)], fill=color, width=2 * scale)

    @staticmethod
    def _arrow_head(
        draw: ImageDraw.ImageDraw,
        tip: Tuple[int, int],
        left: Tuple[int, int],
        right: Tuple[int, int],
        color: Tuple[int, int, int, int],
        width: int,
    ) -> None:
        draw.line([tip, left], fill=color, width=width)
        draw.line([tip, right], fill=color, width=width)

    @staticmethod
    def _rgba(color: str) -> Tuple[int, int, int, int]:
        try:
            rgb = ImageColor.getrgb(color)
        except ValueError:
            rgb = (17, 24, 39)
        return rgb[0], rgb[1], rgb[2], 255


class FloatingToolbar:
    """两行悬浮工具条：上排工具和动作，下排线宽和调色板。"""

    BUTTON_SIZE = 22
    GAP = 3
    SECTION_GAP = 8
    PADDING_X = 5
    PADDING_Y = 3
    ROW_GAP = 2
    COLOR_CELL = 9
    COLOR_GAP = 1
    PALETTE_COLUMNS = 8
    HEIGHT = PADDING_Y * 2 + BUTTON_SIZE * 2 + ROW_GAP

    TOOL_ITEMS = [
        ToolbarItem("rect", "矩形标注", "tool"),
        ToolbarItem("ellipse", "椭圆标注", "tool"),
        ToolbarItem("line", "直线标注", "tool"),
        ToolbarItem("arrow", "箭头标注", "tool"),
        ToolbarItem("pen", "画笔", "tool"),
        ToolbarItem("marker", "高亮笔", "tool"),
        ToolbarItem("mosaic", "马赛克", "tool"),
        ToolbarItem("blur", "模糊", "tool"),
        ToolbarItem("text", "文字标注", "tool"),
    ]
    ACTION_ITEMS = [
        ToolbarItem("clear", "清空标注"),
        ToolbarItem("undo", "撤销"),
        ToolbarItem("redo", "重做"),
        ToolbarItem("cancel", "取消"),
        ToolbarItem("pin", "贴到屏幕"),
        ToolbarItem("save", "保存"),
        ToolbarItem("copy", "复制到剪贴板"),
    ]
    WIDTH_ITEMS = [
        ToolbarItem("width:1", "极细线", "width"),
        ToolbarItem("width:2", "细线", "width"),
        ToolbarItem("width:4", "中线", "width"),
        ToolbarItem("width:7", "粗线", "width"),
    ]
    COLOR_ITEMS = [
        ToolbarItem("color:#ff3b30", "红色", "color"),
        ToolbarItem("color:#ffffff", "白色", "color"),
        ToolbarItem("color:#111827", "黑色", "color"),
        ToolbarItem("color:#6b7280", "灰色", "color"),
        ToolbarItem("color:#1f70d1", "蓝色", "color"),
        ToolbarItem("color:#14a3c7", "青色", "color"),
        ToolbarItem("color:#28a745", "绿色", "color"),
        ToolbarItem("color:#f5d423", "黄色", "color"),
        ToolbarItem("color:#f5a623", "橙色", "color"),
        ToolbarItem("color:#ff66c4", "粉色", "color"),
        ToolbarItem("color:#7c3aed", "紫色", "color"),
        ToolbarItem("color:#8b5cf6", "浅紫色", "color"),
        ToolbarItem("color:#00c2ff", "亮蓝色", "color"),
        ToolbarItem("color:#00b050", "亮绿色", "color"),
        ToolbarItem("color:#f97316", "深橙色", "color"),
        ToolbarItem("color:#e11d48", "玫红色", "color"),
    ]

    def __init__(self) -> None:
        self.buttons: List[OverlayButton] = []
        self.icon_photos: List[ImageTk.PhotoImage] = []
        self.hover_command: Optional[str] = None

    def draw(
        self,
        canvas: tk.Canvas,
        box: Box,
        screen_size: Tuple[int, int],
        active_tool: Optional[str],
        active_color: str,
        active_width: int,
        can_undo: bool = False,
        can_redo: bool = False,
    ) -> None:
        canvas.delete("toolbar")
        self.buttons.clear()
        self.icon_photos.clear()

        toolbar_width = self._toolbar_width()
        toolbar_height = self.HEIGHT
        left, top, right, bottom = box
        screen_width, screen_height = screen_size

        x = right - toolbar_width
        max_x = screen_width - toolbar_width - 8
        x = 8 if max_x < 8 else max(8, min(x, max_x))
        y = bottom + 8
        if y + toolbar_height + 8 > screen_height:
            y = top - toolbar_height - 8
        y = max(8, min(y, max(8, screen_height - toolbar_height - 8)))

        accent = active_color
        canvas.create_rectangle(
            x,
            y,
            x + toolbar_width,
            y + toolbar_height,
            fill="#ffffff",
            outline=accent,
            width=1,
            tags=("toolbar",),
        )

        top_y = y + self.PADDING_Y
        bottom_y = top_y + self.BUTTON_SIZE + self.ROW_GAP
        self._draw_item_row(
            canvas,
            self.TOOL_ITEMS,
            x + self.PADDING_X,
            top_y,
            active_tool,
            active_color,
            active_width,
            can_undo,
            can_redo,
        )
        action_x = (
            x
            + self.PADDING_X
            + self._row_width(self.TOOL_ITEMS)
            + self.SECTION_GAP
        )
        self._draw_item_row(
            canvas,
            self.ACTION_ITEMS,
            action_x,
            top_y,
            active_tool,
            active_color,
            active_width,
            can_undo,
            can_redo,
        )

        canvas.create_line(
            x + self.PADDING_X,
            bottom_y - 2,
            x + toolbar_width - self.PADDING_X,
            bottom_y - 2,
            fill="#e5e7eb",
            tags=("toolbar",),
        )
        width_end = self._draw_item_row(
            canvas,
            self.WIDTH_ITEMS,
            x + self.PADDING_X,
            bottom_y,
            active_tool,
            active_color,
            active_width,
            can_undo,
            can_redo,
        )
        swatch_x = width_end + self.SECTION_GAP
        self._draw_active_swatch(canvas, swatch_x, bottom_y, active_color)
        self._draw_palette(
            canvas,
            swatch_x + self.BUTTON_SIZE + self.SECTION_GAP,
            bottom_y + 1,
            active_color,
        )

    def command_at(self, point: Point) -> Optional[str]:
        x, y = point
        for button in self.buttons:
            left, top, right, bottom = button.bounds
            if left <= x <= right and top <= y <= bottom and button.enabled:
                return button.item.command
        return None

    def tooltip_at(self, point: Point) -> Optional[str]:
        x, y = point
        for button in self.buttons:
            left, top, right, bottom = button.bounds
            if left <= x <= right and top <= y <= bottom:
                suffix = "（当前不可用）" if not button.enabled else ""
                return f"{button.item.tooltip}{suffix}"
        return None

    def _draw_item_row(
        self,
        canvas: tk.Canvas,
        items: List[ToolbarItem],
        x: int,
        y: int,
        active_tool: Optional[str],
        active_color: str,
        active_width: int,
        can_undo: bool,
        can_redo: bool,
    ) -> int:
        cursor_x = x
        for item in items:
            bounds = (
                cursor_x,
                y,
                cursor_x + self.BUTTON_SIZE,
                y + self.BUTTON_SIZE,
            )
            selected = self._is_selected(item, active_tool, active_color, active_width)
            enabled = self._is_enabled(item, can_undo, can_redo)
            self._draw_button(canvas, item, bounds, selected, enabled, active_color)
            self.buttons.append(OverlayButton(item, bounds, enabled))
            cursor_x += self.BUTTON_SIZE + self.GAP
        return cursor_x - self.GAP

    def _draw_active_swatch(
        self, canvas: tk.Canvas, x: int, y: int, active_color: str
    ) -> None:
        bounds = (x, y, x + self.BUTTON_SIZE, y + self.BUTTON_SIZE)
        canvas.create_rectangle(
            bounds,
            fill=active_color,
            outline="#111827",
            width=1,
            tags=("toolbar",),
        )
        if active_color.lower() == "#ffffff":
            canvas.create_line(x + 4, y + 4, x + self.BUTTON_SIZE - 4, y + 4, fill="#d1d5db", tags=("toolbar",))
            canvas.create_line(x + 4, y + self.BUTTON_SIZE - 4, x + self.BUTTON_SIZE - 4, y + self.BUTTON_SIZE - 4, fill="#d1d5db", tags=("toolbar",))

    def _draw_palette(
        self, canvas: tk.Canvas, x: int, y: int, active_color: str
    ) -> None:
        for index, item in enumerate(self.COLOR_ITEMS):
            color = item.command.split(":", 1)[1]
            row = index // self.PALETTE_COLUMNS
            col = index % self.PALETTE_COLUMNS
            cell_left = x + col * (self.COLOR_CELL + self.COLOR_GAP)
            cell_top = y + row * (self.COLOR_CELL + self.COLOR_GAP)
            bounds = (
                cell_left,
                cell_top,
                cell_left + self.COLOR_CELL,
                cell_top + self.COLOR_CELL,
            )
            selected = color.lower() == active_color.lower()
            canvas.create_rectangle(
                bounds,
                fill=color,
                outline="#111827" if color.lower() == "#ffffff" else color,
                width=1,
                tags=("toolbar",),
            )
            if selected:
                canvas.create_rectangle(
                    cell_left - 2,
                    cell_top - 2,
                    cell_left + self.COLOR_CELL + 2,
                    cell_top + self.COLOR_CELL + 2,
                    outline="#111827",
                    width=2,
                    tags=("toolbar",),
                )
            self.buttons.append(OverlayButton(item, bounds, True))

    def _toolbar_width(self) -> int:
        top_width = (
            self.PADDING_X * 2
            + self._row_width(self.TOOL_ITEMS)
            + self.SECTION_GAP
            + self._row_width(self.ACTION_ITEMS)
        )
        palette_width = (
            self.PALETTE_COLUMNS * self.COLOR_CELL
            + (self.PALETTE_COLUMNS - 1) * self.COLOR_GAP
        )
        bottom_width = (
            self.PADDING_X * 2
            + self._row_width(self.WIDTH_ITEMS)
            + self.SECTION_GAP
            + self.BUTTON_SIZE
            + self.SECTION_GAP
            + palette_width
        )
        return max(top_width, bottom_width)

    def _row_width(self, items: List[ToolbarItem]) -> int:
        if not items:
            return 0
        return len(items) * self.BUTTON_SIZE + (len(items) - 1) * self.GAP

    def _is_selected(
        self,
        item: ToolbarItem,
        active_tool: Optional[str],
        active_color: str,
        active_width: int,
    ) -> bool:
        if item.kind == "tool":
            return item.command == active_tool
        if item.kind == "color":
            return item.command == f"color:{active_color}"
        if item.kind == "width":
            return item.command == f"width:{active_width}"
        return False

    @staticmethod
    def _is_enabled(item: ToolbarItem, can_undo: bool, can_redo: bool) -> bool:
        if item.command in {"undo", "clear"}:
            return can_undo
        if item.command == "redo":
            return can_redo
        return True

    def _draw_button(
        self,
        canvas: tk.Canvas,
        item: ToolbarItem,
        bounds: Box,
        selected: bool,
        enabled: bool,
        active_color: str,
    ) -> None:
        left, top, right, bottom = bounds
        selected_fill = active_color
        if active_color.lower() == "#ffffff":
            selected_fill = "#f3f4f6"
        stroke = "#ffffff" if selected and active_color.lower() != "#ffffff" else "#111827"
        if not enabled:
            stroke = "#b8c0cc"

        if selected:
            canvas.create_rectangle(
                left,
                top,
                right,
                bottom,
                fill=selected_fill,
                outline=active_color,
                width=1,
                tags=("toolbar",),
            )
        elif item.command == self.hover_command and enabled:
            canvas.create_rectangle(
                left,
                top,
                right,
                bottom,
                fill="#edf3f7",
                outline="#c7d3dd",
                width=1,
                tags=("toolbar",),
            )

        icon_image = ToolbarIconFactory.make(
            item.command,
            self.BUTTON_SIZE,
            stroke,
            active_color,
            enabled,
        )
        photo = ImageTk.PhotoImage(icon_image)
        self.icon_photos.append(photo)
        canvas.create_image(
            (left + right) // 2,
            (top + bottom) // 2,
            image=photo,
            anchor=tk.CENTER,
            tags=("toolbar",),
        )


class InlineTextEditor:
    """截图区域内的透明文字编辑器，支持直接输入和拖动移动。"""

    MIN_WIDTH = 150
    MAX_WIDTH = 420
    MIN_HEIGHT = 34
    MAX_HEIGHT = 260
    MIN_FONT_SIZE = 10
    MAX_FONT_SIZE = 96
    HANDLE_HEIGHT = 0
    HANDLE_SIZE = 7
    PADDING_X = 4
    PADDING_Y = 3
    FONT = ("Microsoft YaHei UI", 15)

    def __init__(
        self,
        canvas: tk.Canvas,
        selection_box: Box,
        point: Point,
        color: str,
        width: int,
        text: str = "",
        size: Optional[Tuple[int, int]] = None,
        font_size: int = 22,
        on_commit: Optional[Callable[[], None]] = None,
        on_cancel: Optional[Callable[[], None]] = None,
    ) -> None:
        self.canvas = canvas
        self.selection_box = selection_box
        self.color = color
        self.width = width
        self.on_commit = on_commit
        self.on_cancel = on_cancel
        self.drag_start: Point = (0, 0)
        self.start_position: Point = (0, 0)
        self.text = text
        self.font_size = max(self.MIN_FONT_SIZE, min(self.MAX_FONT_SIZE, int(font_size)))
        self.cursor_index = len(text)
        self.item_ids: List[int] = []
        self.caret_id: Optional[int] = None
        self.resize_handle: Optional[str] = None
        self.resize_start_root: Point = (0, 0)
        self.resize_start_position: Point = (0, 0)
        self.resize_start_size: Tuple[int, int] = (0, 0)
        self.resize_start_font_size = self.font_size

        left, top, right, bottom = selection_box
        available_width = max(40, right - left)
        available_height = max(40, bottom - top)
        min_width = min(self.MIN_WIDTH, available_width)
        min_height = min(self.MIN_HEIGHT, available_height)
        preferred_width = size[0] if size else min(self.MAX_WIDTH, max(min_width, right - point[0] - 12))
        preferred_height = size[1] if size else self.MIN_HEIGHT
        self.size = (
            max(min_width, min(preferred_width, available_width)),
            max(min_height, min(preferred_height, available_height)),
        )
        self.position = self._clamp_position(point, self.size)

        self._bind_events()
        if size:
            self._render()
        else:
            self._autosize()
        self.focus()

    def _bind_events(self) -> None:
        self.canvas.bind("<KeyPress>", self._on_key)
        self.canvas.tag_bind("text_editor_drag", "<ButtonPress-1>", self._start_drag)
        self.canvas.tag_bind("text_editor_drag", "<B1-Motion>", self._drag)
        self.canvas.tag_bind("text_editor_text", "<ButtonPress-1>", self._start_drag)
        self.canvas.tag_bind("text_editor_text", "<B1-Motion>", self._drag)
        for handle in ("nw", "ne", "se", "sw"):
            tag = f"text_editor_resize_{handle}"
            self.canvas.tag_bind(
                tag,
                "<ButtonPress-1>",
                lambda event, name=handle: self._start_resize(event, name),
            )
            self.canvas.tag_bind(tag, "<B1-Motion>", self._resize)

    def focus(self) -> None:
        self.canvas.focus_set()

    def content(self) -> str:
        return self.text

    def box(self) -> Box:
        x, y = self.position
        return (x, y, x + self.size[0], y + self.size[1])

    def content_box(self) -> Box:
        left, top, right, bottom = self.box()
        return (
            left + self.PADDING_X,
            top + self.PADDING_Y,
            right - self.PADDING_X,
            bottom - self.PADDING_Y,
        )

    def contains(self, x: int, y: int) -> bool:
        left, top, right, bottom = self.box()
        return left <= x <= right and top <= y <= bottom

    def destroy(self) -> None:
        try:
            self.canvas.unbind("<KeyPress>")
            self.canvas.delete("text_editor")
        except tk.TclError:
            pass

    def lift(self) -> None:
        try:
            self.canvas.tag_raise("text_editor")
        except tk.TclError:
            pass

    def _on_key(self, event: tk.Event) -> str:
        ctrl_pressed = bool(event.state & 0x0004)
        keysym = event.keysym

        if keysym == "Escape":
            return self._cancel(event)
        if ctrl_pressed and keysym in {"Return", "KP_Enter"}:
            return self._commit(event)
        if keysym in {"Return", "KP_Enter"}:
            return self._insert_text("\n")
        if keysym == "BackSpace":
            if self.cursor_index > 0:
                self.text = (
                    self.text[: self.cursor_index - 1]
                    + self.text[self.cursor_index :]
                )
                self.cursor_index -= 1
                self._autosize()
            return "break"
        if keysym == "Delete":
            if self.cursor_index < len(self.text):
                self.text = (
                    self.text[: self.cursor_index]
                    + self.text[self.cursor_index + 1 :]
                )
                self._autosize()
            return "break"
        if keysym == "Left":
            self.cursor_index = max(0, self.cursor_index - 1)
            self._render()
            return "break"
        if keysym == "Right":
            self.cursor_index = min(len(self.text), self.cursor_index + 1)
            self._render()
            return "break"
        if keysym == "Home":
            self.cursor_index = 0
            self._render()
            return "break"
        if keysym == "End":
            self.cursor_index = len(self.text)
            self._render()
            return "break"
        if event.char and not ctrl_pressed:
            return self._insert_text(event.char)
        return "break"

    def _insert_text(self, value: str) -> str:
        self.text = self.text[: self.cursor_index] + value + self.text[self.cursor_index :]
        self.cursor_index += len(value)
        self._autosize()
        return "break"

    def _commit(self, event: Optional[tk.Event] = None) -> str:
        if self.on_commit:
            self.on_commit()
        return "break"

    def _cancel(self, event: Optional[tk.Event] = None) -> str:
        if self.on_cancel:
            self.on_cancel()
        return "break"

    def _focus_click(self, event: tk.Event) -> str:
        self.begin_drag(event.x_root, event.y_root)
        return "break"

    def begin_drag(self, root_x: int, root_y: int) -> None:
        self.focus()
        self.drag_start = (int(root_x), int(root_y))
        self.start_position = self.position

    def drag_to(self, root_x: int, root_y: int) -> None:
        dx = int(root_x) - self.drag_start[0]
        dy = int(root_y) - self.drag_start[1]
        self.position = self._clamp_position(
            (self.start_position[0] + dx, self.start_position[1] + dy),
            self.size,
        )
        self._render()

    def _start_drag(self, event: tk.Event) -> str:
        self.begin_drag(event.x_root, event.y_root)
        return "break"

    def _drag(self, event: tk.Event) -> str:
        self.drag_to(event.x_root, event.y_root)
        return "break"

    def _start_resize(self, event: tk.Event, handle: str) -> str:
        self.resize_handle = handle
        self.resize_start_root = (int(event.x_root), int(event.y_root))
        self.resize_start_position = self.position
        self.resize_start_size = self.size
        self.resize_start_font_size = self.font_size
        self.focus()
        return "break"

    def _resize(self, event: tk.Event) -> str:
        if not self.resize_handle:
            return "break"

        dx = int(event.x_root) - self.resize_start_root[0]
        dy = int(event.y_root) - self.resize_start_root[1]
        start_x, start_y = self.resize_start_position
        start_width, start_height = self.resize_start_size
        left, top, right, bottom = self.selection_box
        min_width = min(self.MIN_WIDTH, right - left)
        min_height = min(self.MIN_HEIGHT, bottom - top)

        next_left = start_x + (dx if "w" in self.resize_handle else 0)
        next_top = start_y + (dy if "n" in self.resize_handle else 0)
        next_right = start_x + start_width + (dx if "e" in self.resize_handle else 0)
        next_bottom = start_y + start_height + (dy if "s" in self.resize_handle else 0)

        next_left = max(left, min(next_left, next_right - min_width))
        next_top = max(top, min(next_top, next_bottom - min_height))
        next_right = min(right, max(next_right, next_left + min_width))
        next_bottom = min(bottom, max(next_bottom, next_top + min_height))
        next_width = max(min_width, next_right - next_left)
        next_height = max(min_height, next_bottom - next_top)

        width_ratio = next_width / max(1, start_width)
        height_ratio = next_height / max(1, start_height)
        scale = max(0.25, min(4.0, (width_ratio + height_ratio) / 2))
        self.font_size = max(
            self.MIN_FONT_SIZE,
            min(self.MAX_FONT_SIZE, int(round(self.resize_start_font_size * scale))),
        )
        self.position = (next_left, next_top)
        self.size = (next_width, next_height)
        self._render()
        return "break"

    def _autosize(self) -> None:
        text = self.content()
        lines = text.splitlines() or [""]
        longest = max(len(line) for line in lines)
        line_count = max(1, len(lines))
        left, top, right, bottom = self.selection_box
        available_width = max(40, right - left)
        available_height = max(40, bottom - top)
        max_width = min(self.MAX_WIDTH, available_width)
        max_height = min(self.MAX_HEIGHT, available_height)
        min_width = min(self.MIN_WIDTH, max_width)
        min_height = min(self.MIN_HEIGHT, max_height)
        char_width = max(7, int(round(self.font_size * 0.62)))
        line_height = max(18, int(round(self.font_size * 1.25)))
        width = max(min_width, min(max_width, longest * char_width + 42))
        height = max(
            min_height,
            min(max_height, line_count * line_height + self.PADDING_Y * 2 + 8),
        )
        self.size = (width, height)
        self.position = self._clamp_position(self.position, self.size)
        self._render()

    def _render(self) -> None:
        self.canvas.delete("text_editor")
        left, top, right, bottom = self.box()
        content_left, content_top, content_right, content_bottom = self.content_box()

        self.canvas.create_rectangle(
            left,
            top,
            right,
            bottom,
            outline=self.color,
            width=1,
            dash=(4, 2),
            tags=("text_editor", "text_editor_drag"),
        )
        for handle_name, handle_x, handle_y in (
            ("nw", left, top),
            ("ne", right, top),
            ("se", right, bottom),
            ("sw", left, bottom),
        ):
            half = self.HANDLE_SIZE // 2
            self.canvas.create_rectangle(
                handle_x - half,
                handle_y - half,
                handle_x + half,
                handle_y + half,
                fill=self.color,
                outline=self.color,
                tags=("text_editor", f"text_editor_resize_{handle_name}"),
            )
        display_text = self.text or "输入文字"
        text_fill = self.color if self.text else "#94a3b8"
        text_id = self.canvas.create_text(
            content_left,
            content_top,
            anchor=tk.NW,
            text=display_text,
            fill=text_fill,
            font=("Microsoft YaHei UI", max(8, int(round(self.font_size * 0.72)))),
            width=max(20, content_right - content_left),
            tags=("text_editor", "text_editor_text"),
        )
        self.item_ids = [text_id]
        self._draw_caret(text_id, content_left, content_top, content_bottom)
        self.canvas.tag_raise("text_editor")

    def _draw_caret(
        self, text_id: int, content_left: int, content_top: int, content_bottom: int
    ) -> None:
        bbox = self.canvas.bbox(text_id)
        if not bbox or not self.text:
            caret_x = content_left
            caret_top = content_top + 2
            caret_bottom = min(content_bottom - 2, content_top + 25)
        else:
            caret_x = min(self.content_box()[2] - 2, bbox[2] + 2)
            caret_top = max(content_top + 2, bbox[3] - 24)
            caret_bottom = min(content_bottom - 2, bbox[3] + 2)
        self.caret_id = self.canvas.create_line(
            caret_x,
            caret_top,
            caret_x,
            caret_bottom,
            fill=self.color,
            width=1,
            tags=("text_editor",),
        )

    def _clamp_position(self, point: Point, size: Tuple[int, int]) -> Point:
        left, top, right, bottom = self.selection_box
        width, height = size
        max_x = max(left, right - width)
        max_y = max(top, bottom - height)
        x = max(left, min(max_x, int(point[0])))
        y = max(top, min(max_y, int(point[1])))
        return x, y


class CaptureOverlay:
    """全屏覆盖层，负责框选、选区预览、简单标注和悬浮工具条。"""

    MIN_SIZE = 5
    DRAW_TOOLS = {"rect", "ellipse", "arrow", "line", "pen", "marker", "mosaic", "blur", "text"}

    def __init__(
        self,
        parent: tk.Tk,
        screen_image: Image.Image,
        on_capture: Callable[[Image.Image, str, Optional[Point]], None],
        on_cancel: Callable[[], None],
        smart_selection: bool = True,
        color_format: str = "hex",
        screen_origin: Point = (0, 0),
        accent_color: str = AppTheme.DEFAULT_ACCENT,
        mask_opacity: int = 55,
        border_width: int = 2,
        show_handles: bool = True,
        show_crosshair: bool = False,
        show_magnifier: bool = True,
        magnifier_zoom_index: int = CaptureMagnifier.DEFAULT_ZOOM_INDEX,
    ) -> None:
        self.parent = parent
        self.screen_image = screen_image
        self.screen_geometry = VirtualDesktopGeometry(
            int(screen_origin[0]),
            int(screen_origin[1]),
            screen_image.width,
            screen_image.height,
        )
        self.on_capture = on_capture
        self.on_cancel = on_cancel
        self.color_format = color_format if color_format in {"hex", "rgb"} else "hex"
        self.accent_color = AppTheme.normalize_accent(accent_color)
        self.mask_opacity = max(20, min(85, int(mask_opacity)))
        self.border_width = max(1, min(6, int(border_width)))
        self.show_handles = bool(show_handles)
        self.show_crosshair = bool(show_crosshair)
        self.show_magnifier = bool(show_magnifier)

        self.tracker = SelectionTracker(
            screen_image.width, screen_image.height, self.MIN_SIZE
        )
        self.candidate_detector = (
            WindowCandidateDetector(
                (screen_image.width, screen_image.height),
                self.screen_geometry.origin,
            )
            if smart_selection
            else None
        )
        self.toolbar = FloatingToolbar()
        self.magnifier = CaptureMagnifier()
        self.magnifier.zoom_index = max(
            0,
            min(len(self.magnifier.ZOOM_SAMPLES) - 1, int(magnifier_zoom_index)),
        )
        self.start_x = 0
        self.start_y = 0
        self.selection_box: Optional[Box] = None
        self.selection_photo: Optional[ImageTk.PhotoImage] = None
        self.annotations: List[Annotation] = []
        self.redo_stack: List[Annotation] = []

        self.active_tool: Optional[str] = None
        self.active_color = "#ff3b30"
        self.active_width = 4
        self.is_selecting = False
        self.is_drawing = False
        self.is_transforming = False
        self.transform_mode: Optional[str] = None
        self.drag_start: Point = (0, 0)
        self.transform_start_box: Optional[Box] = None
        self.transform_annotation_points: List[List[Point]] = []
        self.annotation_start: Optional[Point] = None
        self.current_points: List[Point] = []
        self.annotation_preview_photo: Optional[ImageTk.PhotoImage] = None
        self.mouse_point: Point = (0, 0)
        self.text_editor: Optional[InlineTextEditor] = None
        self.text_editor_restore: Optional[Tuple[Annotation, int]] = None
        self.is_moving_text_editor = False
        self.moving_text_annotation: Optional[Annotation] = None
        self.moving_text_start: Point = (0, 0)
        self.moving_text_points: List[Point] = []
        self.moving_text_bounds: Optional[Box] = None
        self.smart_candidates: List[Box] = []
        self.smart_candidate_index = 0
        self.hover_box: Optional[Box] = None
        self.hover_photo: Optional[ImageTk.PhotoImage] = None
        self.press_candidate: Optional[Box] = None
        self.selection_dragged = False
        self.force_magnifier = False
        self.shift_format_latched = False
        self.magnifier_notice = ""
        self.magnifier_notice_job: Optional[str] = None

        self.window = tk.Toplevel(parent)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.configure(cursor="crosshair")
        self.window.geometry(self.screen_geometry.window_geometry)
        self.color_copy_key_monitor = WindowsPhysicalKeyMonitor(
            self.window,
            ord("C"),
            self._copy_pointer_color,
        )

        dimmed = ImageEnhance.Brightness(screen_image).enhance(
            max(0.15, 1.0 - self.mask_opacity / 100.0)
        )
        self.background = ImageTk.PhotoImage(dimmed)

        self.canvas = tk.Canvas(
            self.window,
            width=screen_image.width,
            height=screen_image.height,
            highlightthickness=0,
            cursor="crosshair",
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.create_image(0, 0, image=self.background, anchor=tk.NW)

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self.canvas.bind("<Button-2>", self._on_middle_click)
        self.canvas.bind("<Button-3>", self._on_right_click)
        self.canvas.bind("<MouseWheel>", self._on_tool_width_wheel)
        self.window.bind("<Escape>", self._handle_escape)
        self.window.bind("<Return>", self._confirm)
        self.window.bind("<Control-c>", lambda event: self._finish("copy"))
        self.window.bind("<Control-s>", lambda event: self._finish("save"))
        self.window.bind("<Control-t>", lambda event: self._finish("pin"))
        self.window.bind("<Control-z>", lambda event: self._handle_undo_shortcut())
        self.window.bind("<Control-y>", lambda event: self._handle_redo_shortcut())
        self.window.bind("<Control-Shift-Z>", lambda event: self._handle_clear_shortcut())
        self.window.bind("<space>", lambda event: self._toggle_toolbar())
        self.window.bind("<Tab>", lambda event: self._handle_tab(1))
        self.window.bind("<Shift-Tab>", lambda event: self._handle_tab(-1))
        self.window.bind("1", lambda event: self._change_tool_width(-1))
        self.window.bind("2", lambda event: self._change_tool_width(1))
        self.window.bind("<KeyPress-Alt_L>", self._show_forced_magnifier)
        self.window.bind("<KeyPress-Alt_R>", self._show_forced_magnifier)
        self.window.bind("<KeyRelease-Alt_L>", self._hide_forced_magnifier)
        self.window.bind("<KeyRelease-Alt_R>", self._hide_forced_magnifier)
        self.window.bind("<KeyPress-Shift_L>", self._toggle_magnifier_format)
        self.window.bind("<KeyPress-Shift_R>", self._toggle_magnifier_format)
        self.window.bind("<KeyRelease-Shift_L>", self._release_format_toggle)
        self.window.bind("<KeyRelease-Shift_R>", self._release_format_toggle)
        if not self.color_copy_key_monitor.start():
            self.window.bind("<KeyPress-c>", self._copy_pointer_color)
            self.window.bind("<KeyPress-C>", self._copy_pointer_color)
        self.window.bind("<Up>", lambda event: self._handle_arrow_key(event, 0, -1))
        self.window.bind("<Down>", lambda event: self._handle_arrow_key(event, 0, 1))
        self.window.bind("<Left>", lambda event: self._handle_arrow_key(event, -1, 0))
        self.window.bind("<Right>", lambda event: self._handle_arrow_key(event, 1, 0))
        self.window.bind("<w>", lambda event: self._move_pointer(0, -1))
        self.window.bind("<s>", lambda event: self._move_pointer(0, 1))
        self.window.bind("<a>", lambda event: self._move_pointer(-1, 0))
        self.window.bind("<d>", lambda event: self._move_pointer(1, 0))
        self.window.grab_set()
        self.window.focus_force()

    def _on_press(self, event: tk.Event) -> None:
        x, y = self._event_point(event)
        self.mouse_point = (x, y)

        if self.text_editor and self.text_editor.contains(x, y):
            self.text_editor.begin_drag(event.x_root, event.y_root)
            self.is_moving_text_editor = True
            self._set_cursor("fleur")
            return

        if self.text_editor:
            self.is_moving_text_editor = False
            self._commit_text_editor()

        command = self._hit_toolbar(x, y)
        if command:
            self._run_toolbar_command(command)
            return

        text_hit = self._text_annotation_at((x, y)) if self.selection_box else None
        if text_hit and self.active_tool in {None, "text"}:
            self._begin_text_move(text_hit, x, y)
            return

        hit = self.tracker.hit_test((x, y))
        if self.selection_box and hit:
            if self.active_tool:
                self._start_annotation(x, y)
            else:
                self._begin_transform(hit, x, y)
            return

        self._begin_selection(x, y)

    def _on_drag(self, event: tk.Event) -> None:
        x, y = self._event_point(event)
        self.mouse_point = (x, y)

        if self.text_editor and self.is_moving_text_editor:
            self.text_editor.drag_to(event.x_root, event.y_root)
        elif self.is_selecting:
            if abs(x - self.start_x) > 3 or abs(y - self.start_y) > 3:
                self.selection_dragged = True
            if self.selection_dragged or self.press_candidate is None:
                self._draw_selection_preview((self.start_x, self.start_y, x, y))
        elif self.moving_text_annotation:
            self._update_text_move(x, y)
        elif self.is_drawing:
            self._update_annotation(x, y, shift_pressed=bool(event.state & 0x0001))
        elif self.is_transforming:
            self._update_transform(x, y)

    def _on_release(self, event: tk.Event) -> None:
        x, y = self._event_point(event)
        self.mouse_point = (x, y)

        if self.is_moving_text_editor:
            self.is_moving_text_editor = False
            if self.text_editor:
                self.text_editor.focus()
                self._set_cursor("xterm")
        elif self.is_selecting:
            self._finish_selection(x, y)
        elif self.moving_text_annotation:
            self._finish_text_move()
        elif self.is_drawing:
            self._finish_annotation(x, y, shift_pressed=bool(event.state & 0x0001))
        elif self.is_transforming:
            self._finish_transform()

    def _on_motion(self, event: tk.Event) -> None:
        if self.text_editor or self.is_selecting or self.is_drawing or self.is_transforming or self.moving_text_annotation:
            return

        x, y = self._event_point(event)
        self.mouse_point = (x, y)
        if not self.selection_box:
            self._update_smart_candidate((x, y))
            self._set_cursor(
                "none" if self._magnifier_is_visible() or self.show_crosshair else "crosshair"
            )
            self._draw_pointer_info()
            return

        toolbar_command = self.toolbar.command_at((x, y))
        if toolbar_command != self.toolbar.hover_command:
            self.toolbar.hover_command = toolbar_command
            self._draw_toolbar()

        if toolbar_command:
            self._set_cursor("hand2")
        elif self._text_annotation_at((x, y)) and self.active_tool in {None, "text"}:
            self._set_cursor("fleur")
        else:
            self._set_cursor(self.tracker.cursor_for_hit(self.tracker.hit_test((x, y)), self.active_tool))
        self._draw_pointer_info()

    def _on_double_click(self, event: tk.Event) -> None:
        x, y = self._event_point(event)
        text_hit = self._text_annotation_at((x, y)) if self.selection_box else None
        if text_hit:
            self._finish_text_move()
            self._begin_text_editor(x, y, text_hit)
            return
        if self.selection_box and self._point_in_box(x, y, self.selection_box):
            self._finish("copy")

    def _on_middle_click(self, event: tk.Event) -> str:
        if self.selection_box and not self.text_editor:
            self._finish("pin")
        return "break"

    def _on_right_click(self, event: tk.Event) -> None:
        if self.text_editor:
            self._commit_text_editor()
            return
        if self.selection_box and self.active_tool in {"rect", "ellipse", "mosaic", "blur"}:
            if self._mark_detected_element(self._event_point(event)):
                return
        if self.selection_box:
            self._clear_selection()
        else:
            self._cancel()

    def _begin_selection(self, x: int, y: int) -> None:
        self.press_candidate = self.hover_box
        self.selection_dragged = False
        self.start_x = x
        self.start_y = y
        self.selection_box = None
        self.selection_photo = None
        self.annotations.clear()
        self.redo_stack.clear()
        self.tracker.clear()
        self.active_tool = None
        self.is_selecting = True
        self.is_drawing = False
        self.is_transforming = False
        self._cancel_text_editor(restore=False)
        self._cancel_text_move()

        self.canvas.delete("selection_view")
        self.canvas.delete("annotation")
        self.canvas.delete("annotation_preview")
        self.canvas.delete("toolbar")
        self.canvas.delete("tooltip")
        self.canvas.delete("smart_candidate")
        if self.press_candidate:
            self._draw_selection_preview(self.press_candidate)
        else:
            self._draw_selection_preview((x, y, x, y))

    def _finish_selection(self, x: int, y: int) -> None:
        self.is_selecting = False
        if self.press_candidate and not self.selection_dragged:
            self.tracker.set_box(self.press_candidate)
        elif not self.tracker.set_from_points((self.start_x, self.start_y), (x, y)):
            self._clear_selection(draw_hint=False)
            return

        self.selection_box = self.tracker.box
        self.press_candidate = None
        self.hover_box = None
        self.smart_candidates = []
        self._redraw_selection()

    def _update_smart_candidate(self, point: Point) -> None:
        if self.candidate_detector is None:
            return
        candidates = self.candidate_detector.candidates_at(point)
        if candidates == self.smart_candidates:
            return
        self.smart_candidates = candidates
        self.smart_candidate_index = 0
        self.hover_box = candidates[0] if candidates else None
        self._draw_smart_candidate()

    def _cycle_smart_candidate(self, direction: int) -> str:
        if self.selection_box or not self.smart_candidates:
            return "break"
        self.smart_candidate_index = (
            self.smart_candidate_index + direction
        ) % len(self.smart_candidates)
        self.hover_box = self.smart_candidates[self.smart_candidate_index]
        self._draw_smart_candidate()
        self._draw_pointer_info()
        return "break"

    def _handle_tab(self, direction: int) -> str:
        if self.text_editor:
            return "break"
        if self.selection_box and self.active_tool in {"line", "arrow"}:
            self.active_tool = "arrow" if self.active_tool == "line" else "line"
            self._redraw_selection()
            return "break"
        return self._cycle_smart_candidate(direction)

    def _on_tool_width_wheel(self, event: tk.Event) -> str:
        if self.selection_box and self.active_tool and not self.text_editor:
            self._change_tool_width(1 if event.delta > 0 else -1)
        elif self._magnifier_is_visible():
            if self.magnifier.adjust_zoom(1 if event.delta > 0 else -1):
                if self.magnifier_notice_job:
                    try:
                        self.window.after_cancel(self.magnifier_notice_job)
                    except tk.TclError:
                        pass
                    self.magnifier_notice_job = None
                self.magnifier_notice = ""
                self._draw_pointer_info()
        return "break"

    def _change_tool_width(self, direction: int) -> str:
        if not self.selection_box or not self.active_tool or self.text_editor:
            return "break"
        self.active_width = self._clamp(self.active_width + direction, 1, 20)
        self._redraw_selection()
        return "break"

    def _show_forced_magnifier(self, event: Optional[tk.Event] = None) -> str:
        if not self.text_editor:
            self.force_magnifier = True
            self._set_cursor("none")
            self._draw_pointer_info()
        return "break"

    def _hide_forced_magnifier(self, event: Optional[tk.Event] = None) -> str:
        self.force_magnifier = False
        self._set_cursor(
            self.tracker.cursor_for_hit(
                self.tracker.hit_test(self.mouse_point),
                self.active_tool,
            )
        )
        self._draw_pointer_info()
        return "break"

    def _toggle_magnifier_format(self, event: Optional[tk.Event] = None) -> Optional[str]:
        if self.shift_format_latched:
            return "break"
        if (
            self.text_editor
            or self.is_selecting
            or self.is_drawing
            or self.is_transforming
            or self.moving_text_annotation
        ):
            return None
        if self.selection_box and not self.force_magnifier:
            return None
        self.shift_format_latched = True
        self.color_format = "rgb" if self.color_format == "hex" else "hex"
        self._set_magnifier_notice(f"当前格式 {self.color_format.upper()}")
        return "break"

    def _release_format_toggle(self, event: Optional[tk.Event] = None) -> None:
        self.shift_format_latched = False

    def _copy_pointer_color(self, event: Optional[tk.Event] = None) -> Optional[str]:
        if self.text_editor:
            return None
        x = self._clamp(self.mouse_point[0], 0, self.screen_image.width - 1)
        y = self._clamp(self.mouse_point[1], 0, self.screen_image.height - 1)
        red, green, blue = self.screen_image.getpixel((x, y))[:3]
        if self.color_format == "rgb":
            value = f"RGB({red}, {green}, {blue})"
        else:
            value = f"#{red:02X}{green:02X}{blue:02X}"
        self.window.clipboard_clear()
        self.window.clipboard_append(value)
        self.window.update_idletasks()
        self._set_magnifier_notice(f"已复制 {value}")
        if not self._magnifier_is_visible():
            self._draw_tooltip(x, y, self.magnifier_notice)
        return "break"

    def _set_magnifier_notice(self, message: str) -> None:
        if self.magnifier_notice_job:
            try:
                self.window.after_cancel(self.magnifier_notice_job)
            except tk.TclError:
                pass
        self.magnifier_notice = message
        self._draw_pointer_info()
        self.magnifier_notice_job = self.window.after(
            1200,
            self._clear_magnifier_notice,
        )

    def _clear_magnifier_notice(self) -> None:
        self.magnifier_notice_job = None
        self.magnifier_notice = ""
        try:
            self._draw_pointer_info()
        except tk.TclError:
            pass

    def _mark_detected_element(self, point: Point) -> bool:
        if not self.selection_box or not self.candidate_detector or not self.active_tool:
            return False
        candidates = self.candidate_detector.candidates_at(point)
        if not candidates:
            return False

        selection_left, selection_top, selection_right, selection_bottom = self.selection_box
        for candidate in candidates:
            left = max(selection_left, candidate[0])
            top = max(selection_top, candidate[1])
            right = min(selection_right, candidate[2])
            bottom = min(selection_bottom, candidate[3])
            if right - left < 4 or bottom - top < 4:
                continue
            self.annotations.append(
                Annotation(
                    self.active_tool,
                    [(left, top), (right, bottom)],
                    color=self.active_color,
                    width=self.active_width,
                )
            )
            self.redo_stack.clear()
            self._redraw_selection()
            return True
        return False

    def _draw_smart_candidate(self) -> None:
        self.canvas.delete("smart_candidate")
        if not self.hover_box:
            self.hover_photo = None
            return

        left, top, right, bottom = self.hover_box
        preview = self.screen_image.crop(self.hover_box)
        self.hover_photo = ImageTk.PhotoImage(preview)
        self.canvas.create_image(
            left,
            top,
            image=self.hover_photo,
            anchor=tk.NW,
            tags=("smart_candidate",),
        )
        self.canvas.create_rectangle(
            left,
            top,
            right,
            bottom,
            outline=self.accent_color,
            width=self.border_width,
            tags=("smart_candidate",),
        )
        label = f"{right - left} x {bottom - top}"
        text_id = self.canvas.create_text(
            left + 7,
            top + 7,
            anchor=tk.NW,
            text=label,
            fill="#ffffff",
            font=("Microsoft YaHei UI", 10, "bold"),
            tags=("smart_candidate",),
        )
        bounds = self.canvas.bbox(text_id)
        if bounds:
            background_id = self.canvas.create_rectangle(
                bounds[0] - 5,
                bounds[1] - 3,
                bounds[2] + 5,
                bounds[3] + 3,
                fill=self.accent_color,
                outline=self.accent_color,
                tags=("smart_candidate",),
            )
            self.canvas.tag_lower(background_id, text_id)

    def _draw_selection_preview(self, box: Box) -> None:
        box = self._clip_box(ScreenshotManager._normalize_box(box))
        left, top, right, bottom = box
        width = right - left
        height = bottom - top

        self.canvas.delete("selection_view")
        self.canvas.delete("toolbar")
        self.canvas.delete("smart_candidate")
        if width < 1 or height < 1:
            return

        preview = AnnotationRenderer.render(self.screen_image, box, self.annotations)
        self.selection_photo = ImageTk.PhotoImage(preview)
        self.canvas.create_image(
            left,
            top,
            image=self.selection_photo,
            anchor=tk.NW,
            tags=("selection_view",),
        )
        self.canvas.create_rectangle(
            left,
            top,
            right,
            bottom,
            outline=self.accent_color,
            width=self.border_width,
            tags=("selection_view",),
        )
        if self.show_handles:
            self._draw_handles(box)
        self._draw_size_label(box)
        if self.is_selecting or self.is_drawing or self.is_transforming:
            self._draw_pointer_info()

    def _redraw_selection(self) -> None:
        if not self.selection_box:
            return

        self._draw_selection_preview(self.selection_box)
        self._draw_toolbar()
        self._draw_pointer_info()
        if self.text_editor:
            self.text_editor.lift()

    def _draw_handles(self, box: Box) -> None:
        self.tracker.set_box(box)
        for x, y in self.tracker.handle_centers().values():
            self.canvas.create_rectangle(
                x - 4,
                y - 4,
                x + 4,
                y + 4,
                fill=self.accent_color,
                outline="#ffffff",
                tags=("selection_view",),
            )

    def _draw_size_label(self, box: Box) -> None:
        left, top, right, bottom = box
        label = f"{right - left} x {bottom - top}"
        x = left + 6
        y = top - 24 if top > 32 else top + 6
        text_id = self.canvas.create_text(
            x,
            y,
            anchor=tk.NW,
            text=label,
            fill="#ffffff",
            font=("Microsoft YaHei UI", 10, "bold"),
            tags=("selection_view",),
        )
        bbox = self.canvas.bbox(text_id)
        if bbox:
            bg_id = self.canvas.create_rectangle(
                bbox[0] - 5,
                bbox[1] - 3,
                bbox[2] + 5,
                bbox[3] + 3,
                fill=self.accent_color,
                outline=self.accent_color,
                tags=("selection_view",),
            )
            self.canvas.tag_lower(bg_id, text_id)

    def _draw_toolbar(self) -> None:
        if not self.selection_box:
            return

        self.toolbar.draw(
            self.canvas,
            self.selection_box,
            (self.screen_image.width, self.screen_image.height),
            self.active_tool,
            self.active_color,
            self.active_width,
            can_undo=bool(self.annotations),
            can_redo=bool(self.redo_stack),
        )

    def _run_toolbar_command(self, command: str) -> None:
        if self.text_editor:
            self._commit_text_editor()

        if command.startswith("color:"):
            self.active_color = command.split(":", 1)[1]
            self._redraw_selection()
            return

        if command.startswith("width:"):
            self.active_width = int(command.split(":", 1)[1])
            self._redraw_selection()
            return

        if command in self.DRAW_TOOLS:
            self.active_tool = None if self.active_tool == command else command
            self._redraw_selection()
            return

        if command == "undo":
            self._undo_annotation()
            return

        if command == "redo":
            self._redo_annotation()
            return

        if command == "clear":
            self._clear_annotations()
            return

        if command == "cancel":
            self._cancel()
            return

        if command in {"pin", "copy", "save", "finish"}:
            self._finish(command)

    def _begin_transform(self, mode: str, x: int, y: int) -> None:
        if not self.selection_box:
            return

        self.is_transforming = True
        self.transform_mode = mode
        self.drag_start = (x, y)
        self.transform_start_box = self.selection_box
        self.transform_annotation_points = [
            list(annotation.points) for annotation in self.annotations
        ]
        self.canvas.delete("toolbar")
        self.toolbar.buttons.clear()

    def _update_transform(self, x: int, y: int) -> None:
        if not self.transform_mode or not self.transform_start_box:
            return

        if self.transform_mode == "move":
            box, dx, dy = self.tracker.move_from_drag(
                self.transform_start_box, self.drag_start, (x, y)
            )
            self.selection_box = box
            self._restore_annotation_points(dx, dy)
        else:
            self.selection_box = self.tracker.resize_from_drag(
                self.transform_mode, self.transform_start_box, self.drag_start, (x, y)
            )

        self._redraw_selection()

    def _finish_transform(self) -> None:
        self.is_transforming = False
        self.transform_mode = None
        self.transform_start_box = None
        self.transform_annotation_points = []
        self._redraw_selection()

    def _restore_annotation_points(self, dx: int, dy: int) -> None:
        for annotation, original_points in zip(
            self.annotations, self.transform_annotation_points
        ):
            annotation.points = [(x + dx, y + dy) for x, y in original_points]

    def _undo_annotation(self) -> None:
        if self.text_editor:
            return
        if self.annotations:
            self.redo_stack.append(self.annotations.pop())
            self._redraw_selection()

    def _redo_annotation(self) -> None:
        if self.text_editor:
            return
        if self.redo_stack:
            self.annotations.append(self.redo_stack.pop())
            self._redraw_selection()

    def _clear_annotations(self) -> None:
        if self.text_editor:
            self._cancel_text_editor(restore=False)
        if self.annotations:
            self.annotations.clear()
            self.redo_stack.clear()
            self._redraw_selection()

    def _toggle_toolbar(self) -> None:
        if not self.selection_box:
            return
        if self.toolbar.buttons:
            self.canvas.delete("toolbar")
            self.toolbar.buttons.clear()
        else:
            self._draw_toolbar()

    def _clear_selection(self, draw_hint: bool = True) -> None:
        self.selection_box = None
        self.selection_photo = None
        self.annotation_preview_photo = None
        self.tracker.clear()
        self.annotations.clear()
        self.redo_stack.clear()
        self._cancel_text_editor(restore=False)
        self._cancel_text_move()
        self.active_tool = None
        self.press_candidate = None
        self.hover_box = None
        self.hover_photo = None
        self.smart_candidates = []
        self.smart_candidate_index = 0
        self.toolbar.hover_command = None
        self.is_selecting = False
        self.is_drawing = False
        self.is_transforming = False
        self.canvas.delete("selection_view")
        self.canvas.delete("annotation")
        self.canvas.delete("annotation_preview")
        self.canvas.delete("toolbar")
        self.canvas.delete("pointer_info")
        self.canvas.delete("tooltip")
        self.canvas.delete("smart_candidate")
        if draw_hint:
            self._set_cursor("none")
            self._draw_pointer_info()

    def _handle_escape(self, event: Optional[tk.Event] = None) -> None:
        if self.text_editor:
            self._cancel_text_editor()
            return
        if self.moving_text_annotation:
            self._cancel_text_move(restore=True)
            return
        if self.selection_box or self.is_selecting or self.is_drawing:
            self._clear_selection()
        else:
            self._cancel()

    def _handle_arrow_key(self, event: tk.Event, dx: int, dy: int) -> str:
        if not self.selection_box:
            if not self.is_selecting:
                self._move_pointer(dx, dy)
            return "break"
        if self.active_tool:
            return "break"

        if event.state & 0x0004:
            self._expand_selection(dx, dy)
        elif event.state & 0x0001:
            self._shrink_selection(dx, dy)
        else:
            left, top, right, bottom = self.selection_box
            moved_box, move_dx, move_dy = self.tracker.move_from_drag(
                self.selection_box,
                (0, 0),
                (dx, dy),
            )
            self.selection_box = moved_box
            self.transform_annotation_points = [
                list(annotation.points) for annotation in self.annotations
            ]
            self._restore_annotation_points(move_dx, move_dy)
            self.transform_annotation_points = []
            self._redraw_selection()
        return "break"

    def _expand_selection(self, dx: int, dy: int) -> None:
        if not self.selection_box:
            return
        left, top, right, bottom = self.selection_box
        if dx < 0:
            left -= 1
        elif dx > 0:
            right += 1
        if dy < 0:
            top -= 1
        elif dy > 0:
            bottom += 1
        self.selection_box = self._clip_box((left, top, right, bottom))
        self.tracker.set_box(self.selection_box)
        self._redraw_selection()

    def _shrink_selection(self, dx: int, dy: int) -> None:
        if not self.selection_box:
            return
        left, top, right, bottom = self.selection_box
        if dx < 0 and right - left > self.MIN_SIZE:
            right -= 1
        elif dx > 0 and right - left > self.MIN_SIZE:
            left += 1
        if dy < 0 and bottom - top > self.MIN_SIZE:
            bottom -= 1
        elif dy > 0 and bottom - top > self.MIN_SIZE:
            top += 1
        self.selection_box = self._clip_box((left, top, right, bottom))
        self.tracker.set_box(self.selection_box)
        self._redraw_selection()

    def _move_pointer(self, dx: int, dy: int) -> None:
        x = self._clamp(self.mouse_point[0] + dx, 0, self.screen_image.width - 1)
        y = self._clamp(self.mouse_point[1] + dy, 0, self.screen_image.height - 1)
        self.mouse_point = (x, y)
        if platform.system() == "Windows":
            absolute_x, absolute_y = self.screen_geometry.to_absolute_point((x, y))
            ctypes.windll.user32.SetCursorPos(absolute_x, absolute_y)
        self._draw_pointer_info()

    def _begin_text_editor(
        self, x: int, y: int, annotation: Optional[Annotation] = None
    ) -> None:
        if not self.selection_box:
            return

        self._cancel_text_move()
        self._cancel_text_editor()
        text = ""
        size: Optional[Tuple[int, int]] = None
        point = self._clamp_to_selection(x, y)
        color = self.active_color
        width = self.active_width
        font_size = 22

        if annotation and annotation in self.annotations:
            index = self.annotations.index(annotation)
            self.text_editor_restore = (annotation, index)
            self.annotations.pop(index)
            text = annotation.text
            color = annotation.color
            width = annotation.width
            font_size = annotation.font_size
            bounds = self._text_annotation_bounds(annotation)
            point = (
                bounds[0] - InlineTextEditor.PADDING_X,
                bounds[1] - InlineTextEditor.HANDLE_HEIGHT - InlineTextEditor.PADDING_Y,
            )
            size = (
                bounds[2] - bounds[0] + InlineTextEditor.PADDING_X * 2,
                bounds[3] - bounds[1] + InlineTextEditor.HANDLE_HEIGHT + InlineTextEditor.PADDING_Y * 2,
            )
            self._redraw_selection()
        else:
            self.text_editor_restore = None
            point = (
                point[0] - InlineTextEditor.PADDING_X,
                point[1] - InlineTextEditor.HANDLE_HEIGHT - InlineTextEditor.PADDING_Y,
            )

        self.text_editor = InlineTextEditor(
            self.canvas,
            self.selection_box,
            point,
            color,
            width,
            text=text,
            size=size,
            font_size=font_size,
            on_commit=self._commit_text_editor,
            on_cancel=self._cancel_text_editor,
        )
        self.canvas.delete("pointer_info")
        self.canvas.delete("tooltip")
        self._set_cursor("xterm")

    def _commit_text_editor(self) -> bool:
        if not self.text_editor:
            return False

        editor = self.text_editor
        text = editor.content().strip()
        restore = self.text_editor_restore
        box = editor.content_box()
        editor.destroy()
        self.text_editor = None
        self.text_editor_restore = None
        self.is_moving_text_editor = False

        if not text:
            if restore:
                annotation, index = restore
                self.annotations.insert(min(index, len(self.annotations)), annotation)
            self._redraw_selection()
            return False

        annotation = Annotation(
            "text",
            [(box[0], box[1]), (box[2], box[3])],
            text=text,
            color=editor.color,
            width=editor.width,
            font_size=editor.font_size,
        )

        if restore:
            _, index = restore
            self.annotations.insert(min(index, len(self.annotations)), annotation)
        else:
            self.annotations.append(annotation)
        self.redo_stack.clear()
        self._redraw_selection()
        return True

    def _cancel_text_editor(self, restore: bool = True) -> None:
        if not self.text_editor:
            self.text_editor_restore = None
            self.is_moving_text_editor = False
            return

        self.text_editor.destroy()
        self.text_editor = None
        self.is_moving_text_editor = False
        original = self.text_editor_restore
        self.text_editor_restore = None
        if restore and original:
            annotation, index = original
            self.annotations.insert(min(index, len(self.annotations)), annotation)
        if self.selection_box:
            self._redraw_selection()

    def _begin_text_move(self, annotation: Annotation, x: int, y: int) -> None:
        if not self.selection_box:
            return

        bounds = self._text_annotation_bounds(annotation)
        if len(annotation.points) < 2:
            annotation.points = [(bounds[0], bounds[1]), (bounds[2], bounds[3])]

        self.moving_text_annotation = annotation
        self.moving_text_start = (x, y)
        self.moving_text_points = list(annotation.points)
        self.moving_text_bounds = bounds
        self.canvas.delete("toolbar")
        self.toolbar.buttons.clear()
        self._set_cursor("fleur")

    def _update_text_move(self, x: int, y: int) -> None:
        if not self.moving_text_annotation or not self.moving_text_bounds or not self.selection_box:
            return

        requested_dx = x - self.moving_text_start[0]
        requested_dy = y - self.moving_text_start[1]
        left, top, right, bottom = self.moving_text_bounds
        box_left, box_top, box_right, box_bottom = self.selection_box
        width = right - left
        height = bottom - top
        new_left = self._clamp(left + requested_dx, box_left, max(box_left, box_right - width))
        new_top = self._clamp(top + requested_dy, box_top, max(box_top, box_bottom - height))
        dx = new_left - left
        dy = new_top - top
        self.moving_text_annotation.points = [
            (px + dx, py + dy) for px, py in self.moving_text_points
        ]
        self._draw_selection_preview(self.selection_box)
        self._draw_pointer_info()

    def _finish_text_move(self) -> None:
        if not self.moving_text_annotation:
            return
        self.moving_text_annotation = None
        self.moving_text_points = []
        self.moving_text_bounds = None
        self._redraw_selection()

    def _cancel_text_move(self, restore: bool = False) -> None:
        if restore and self.moving_text_annotation and self.moving_text_points:
            self.moving_text_annotation.points = list(self.moving_text_points)
        self.moving_text_annotation = None
        self.moving_text_points = []
        self.moving_text_bounds = None
        if restore and self.selection_box:
            self._redraw_selection()

    def _text_annotation_at(self, point: Point) -> Optional[Annotation]:
        x, y = point
        for annotation in reversed(self.annotations):
            if annotation.tool != "text" or not annotation.text:
                continue
            left, top, right, bottom = self._text_annotation_bounds(annotation)
            if left - 4 <= x <= right + 4 and top - 4 <= y <= bottom + 4:
                return annotation
        return None

    def _text_annotation_bounds(self, annotation: Annotation) -> Box:
        if annotation.tool == "text" and len(annotation.points) >= 2:
            return ScreenshotManager._normalize_box(
                (
                    annotation.points[0][0],
                    annotation.points[0][1],
                    annotation.points[1][0],
                    annotation.points[1][1],
                )
            )

        x, y = annotation.points[0] if annotation.points else (0, 0)
        font = AnnotationRenderer._load_font(annotation.font_size)
        probe = Image.new("RGB", (1, 1), "white")
        draw = ImageDraw.Draw(probe)
        bbox = draw.multiline_textbbox((x, y), annotation.text or "", font=font, spacing=4)
        return bbox[0] - 4, bbox[1] - 4, bbox[2] + 8, bbox[3] + 8

    def _handle_undo_shortcut(self) -> None:
        if not self.text_editor:
            self._undo_annotation()

    def _handle_redo_shortcut(self) -> None:
        if not self.text_editor:
            self._redo_annotation()

    def _handle_clear_shortcut(self) -> None:
        if not self.text_editor:
            self._clear_annotations()

    def _start_annotation(self, x: int, y: int) -> None:
        if not self.selection_box or not self.active_tool:
            return

        x, y = self._clamp_to_selection(x, y)
        if self.active_tool == "text":
            self._begin_text_editor(x, y)
            return

        self.is_drawing = True
        self.annotation_start = (x, y)
        self.current_points = [(x, y)]

    def _update_annotation(
        self,
        x: int,
        y: int,
        shift_pressed: bool = False,
    ) -> None:
        if not self.annotation_start or not self.active_tool:
            return

        x, y = self._clamp_to_selection(x, y)
        x, y = self._constrain_annotation_point((x, y), shift_pressed)
        self.canvas.delete("annotation_preview")

        if self.active_tool == "rect":
            start_x, start_y = self.annotation_start
            self.canvas.create_rectangle(
                start_x,
                start_y,
                x,
                y,
                outline=self.active_color,
                width=self.active_width,
                tags=("annotation_preview",),
            )
        elif self.active_tool == "ellipse":
            start_x, start_y = self.annotation_start
            self.canvas.create_oval(
                start_x,
                start_y,
                x,
                y,
                outline=self.active_color,
                width=self.active_width,
                tags=("annotation_preview",),
            )
        elif self.active_tool == "arrow":
            start_x, start_y = self.annotation_start
            self.canvas.create_line(
                start_x,
                start_y,
                x,
                y,
                fill=self.active_color,
                width=self.active_width,
                arrow=tk.LAST,
                tags=("annotation_preview",),
            )
        elif self.active_tool == "line":
            start_x, start_y = self.annotation_start
            self.canvas.create_line(
                start_x,
                start_y,
                x,
                y,
                fill=self.active_color,
                width=self.active_width,
                tags=("annotation_preview",),
            )
        elif self.active_tool == "marker":
            if shift_pressed:
                self.current_points = [self.annotation_start, (x, y)]
            else:
                self.current_points.append((x, y))
            self._draw_pen(
                self.current_points,
                ("annotation_preview",),
                self.active_color,
                max(8, self.active_width * 4),
            )
        elif self.active_tool == "mosaic":
            start_x, start_y = self.annotation_start
            left, top, right, bottom = ScreenshotManager._normalize_box(
                (start_x, start_y, x, y)
            )
            self._draw_mosaic_preview((left, top, right, bottom))
            self.canvas.create_rectangle(
                left,
                top,
                right,
                bottom,
                outline="#111827",
                width=1,
                dash=(4, 3),
                tags=("annotation_preview",),
            )
        elif self.active_tool == "blur":
            start_x, start_y = self.annotation_start
            left, top, right, bottom = ScreenshotManager._normalize_box(
                (start_x, start_y, x, y)
            )
            self._draw_blur_preview((left, top, right, bottom))
            self.canvas.create_rectangle(
                left,
                top,
                right,
                bottom,
                outline="#111827",
                width=1,
                dash=(4, 3),
                tags=("annotation_preview",),
            )
        elif self.active_tool == "pen":
            if shift_pressed:
                self.current_points = [self.annotation_start, (x, y)]
            else:
                self.current_points.append((x, y))
            self._draw_pen(self.current_points, ("annotation_preview",), self.active_color)

    def _finish_annotation(
        self,
        x: int,
        y: int,
        shift_pressed: bool = False,
    ) -> None:
        if not self.annotation_start or not self.active_tool:
            return

        x, y = self._clamp_to_selection(x, y)
        x, y = self._constrain_annotation_point((x, y), shift_pressed)
        self.canvas.delete("annotation_preview")

        if self.active_tool == "rect":
            start_x, start_y = self.annotation_start
            if abs(x - start_x) >= 4 and abs(y - start_y) >= 4:
                self.annotations.append(
                    Annotation(
                        "rect",
                        [(start_x, start_y), (x, y)],
                        color=self.active_color,
                        width=self.active_width,
                    )
                )
                self.redo_stack.clear()
        elif self.active_tool == "ellipse":
            start_x, start_y = self.annotation_start
            if abs(x - start_x) >= 4 and abs(y - start_y) >= 4:
                self.annotations.append(
                    Annotation(
                        "ellipse",
                        [(start_x, start_y), (x, y)],
                        color=self.active_color,
                        width=self.active_width,
                    )
                )
                self.redo_stack.clear()
        elif self.active_tool == "arrow":
            start_x, start_y = self.annotation_start
            if abs(x - start_x) >= 4 or abs(y - start_y) >= 4:
                self.annotations.append(
                    Annotation(
                        "arrow",
                        [(start_x, start_y), (x, y)],
                        color=self.active_color,
                        width=self.active_width,
                    )
                )
                self.redo_stack.clear()
        elif self.active_tool == "line":
            start_x, start_y = self.annotation_start
            if abs(x - start_x) >= 4 or abs(y - start_y) >= 4:
                self.annotations.append(
                    Annotation(
                        "line",
                        [(start_x, start_y), (x, y)],
                        color=self.active_color,
                        width=self.active_width,
                    )
                )
                self.redo_stack.clear()
        elif self.active_tool == "marker":
            self.current_points.append((x, y))
            if len(self.current_points) >= 2:
                self.annotations.append(
                    Annotation(
                        "marker",
                        list(self.current_points),
                        color=self.active_color,
                        width=self.active_width,
                    )
                )
                self.redo_stack.clear()
        elif self.active_tool == "mosaic":
            start_x, start_y = self.annotation_start
            if abs(x - start_x) >= 8 and abs(y - start_y) >= 8:
                self.annotations.append(
                    Annotation(
                        "mosaic",
                        [(start_x, start_y), (x, y)],
                        color=self.active_color,
                        width=self.active_width,
                    )
                )
                self.redo_stack.clear()
        elif self.active_tool == "blur":
            start_x, start_y = self.annotation_start
            if abs(x - start_x) >= 8 and abs(y - start_y) >= 8:
                self.annotations.append(
                    Annotation(
                        "blur",
                        [(start_x, start_y), (x, y)],
                        color=self.active_color,
                        width=self.active_width,
                    )
                )
                self.redo_stack.clear()
        elif self.active_tool == "pen":
            self.current_points.append((x, y))
            if len(self.current_points) >= 2:
                self.annotations.append(
                    Annotation(
                        "pen",
                        list(self.current_points),
                        color=self.active_color,
                        width=self.active_width,
                    )
                )
                self.redo_stack.clear()

        self.is_drawing = False
        self.annotation_start = None
        self.current_points = []
        self._redraw_selection()

    def _constrain_annotation_point(
        self,
        point: Point,
        shift_pressed: bool,
    ) -> Point:
        if not shift_pressed or not self.annotation_start or not self.active_tool:
            return point

        start_x, start_y = self.annotation_start
        dx = point[0] - start_x
        dy = point[1] - start_y
        if self.active_tool in {"rect", "ellipse", "mosaic", "blur"}:
            side = max(abs(dx), abs(dy))
            constrained = (
                start_x + (-side if dx < 0 else side),
                start_y + (-side if dy < 0 else side),
            )
            return self._clamp_to_selection(*constrained)

        if self.active_tool in {"line", "arrow", "marker", "pen"}:
            distance = math.hypot(dx, dy)
            if distance < 1:
                return point
            angle = math.atan2(dy, dx)
            snapped = round(angle / (math.pi / 4)) * (math.pi / 4)
            constrained = (
                int(round(start_x + math.cos(snapped) * distance)),
                int(round(start_y + math.sin(snapped) * distance)),
            )
            return self._clamp_to_selection(*constrained)
        return point

    def _draw_mosaic_preview(self, box: Box) -> None:
        left, top, right, bottom = self._clip_box(box)
        if right - left < 2 or bottom - top < 2:
            return

        preview = self.screen_image.crop((left, top, right, bottom)).convert("RGB")
        AnnotationRenderer._apply_mosaic(
            preview,
            [(0, 0), (preview.width, preview.height)],
            block_size=max(6, self.active_width * 3),
        )
        self.annotation_preview_photo = ImageTk.PhotoImage(preview)
        self.canvas.create_image(
            left,
            top,
            image=self.annotation_preview_photo,
            anchor=tk.NW,
            tags=("annotation_preview",),
        )

    def _draw_blur_preview(self, box: Box) -> None:
        left, top, right, bottom = self._clip_box(box)
        if right - left < 2 or bottom - top < 2:
            return

        preview = self.screen_image.crop((left, top, right, bottom)).convert("RGB")
        AnnotationRenderer._apply_blur(
            preview,
            [(0, 0), (preview.width, preview.height)],
            radius=max(4, self.active_width * 2),
        )
        self.annotation_preview_photo = ImageTk.PhotoImage(preview)
        self.canvas.create_image(
            left,
            top,
            image=self.annotation_preview_photo,
            anchor=tk.NW,
            tags=("annotation_preview",),
        )

    def _draw_pointer_info(self) -> None:
        self.canvas.delete("pointer_info")
        self.canvas.delete("tooltip")

        x, y = self.mouse_point
        safe_x = self._clamp(x, 0, self.screen_image.width - 1)
        safe_y = self._clamp(y, 0, self.screen_image.height - 1)
        pixel = self.screen_image.getpixel((safe_x, safe_y))
        red, green, blue = pixel[:3]
        tooltip = self.toolbar.tooltip_at((x, y))

        if self.show_crosshair:
            self._draw_screen_crosshair(safe_x, safe_y)

        if tooltip:
            self._draw_tooltip(x, y, tooltip)
            return

        if self._magnifier_is_visible():
            self.magnifier.draw(
                self.canvas,
                self.screen_image,
                (safe_x, safe_y),
                self.color_format,
                self.screen_geometry.origin,
                notice=self.magnifier_notice,
            )
            return

        if self.selection_box:
            left, top, right, bottom = self.selection_box
            absolute_left, absolute_top = self.screen_geometry.to_absolute_point(
                (left, top)
            )
            absolute_x, absolute_y = self.screen_geometry.to_absolute_point((x, y))
            lines = [
                f"顶点 ({absolute_left}, {absolute_top})    大小 {right - left} x {bottom - top}",
                f"光标 ({absolute_x}, {absolute_y})    RGB ({red}, {green}, {blue})",
            ]
        else:
            absolute_x, absolute_y = self.screen_geometry.to_absolute_point((x, y))
            lines = [
                f"光标 ({absolute_x}, {absolute_y})    RGB ({red}, {green}, {blue})",
            ]

        self._draw_tooltip(x, y, "\n".join(lines), tag="pointer_info")

    def _draw_screen_crosshair(self, x: int, y: int) -> None:
        self.canvas.create_line(
            0,
            y,
            self.screen_image.width,
            y,
            fill=self.accent_color,
            width=1,
            dash=(3, 3),
            tags=("pointer_info",),
        )
        self.canvas.create_line(
            x,
            0,
            x,
            self.screen_image.height,
            fill=self.accent_color,
            width=1,
            dash=(3, 3),
            tags=("pointer_info",),
        )

    def _magnifier_is_visible(self) -> bool:
        if self.force_magnifier:
            return True
        return self.show_magnifier and bool(
            not self.selection_box
            or self.is_selecting
            or self.is_drawing
            or self.is_transforming
        )

    def _draw_tooltip(
        self, x: int, y: int, text: str, tag: str = "tooltip"
    ) -> None:
        text_id = self.canvas.create_text(
            0,
            0,
            anchor=tk.NW,
            text=text,
            fill="#ffffff",
            font=("Microsoft YaHei UI", 9),
            tags=(tag,),
        )
        bbox = self.canvas.bbox(text_id)
        if not bbox:
            return

        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        panel_x = x + 14
        panel_y = y + 18
        if panel_x + width + 16 > self.screen_image.width:
            panel_x = x - width - 20
        if panel_y + height + 12 > self.screen_image.height:
            panel_y = y - height - 16
        panel_x = self._clamp(panel_x, 8, self.screen_image.width - width - 16)
        panel_y = self._clamp(panel_y, 8, self.screen_image.height - height - 12)

        self.canvas.coords(text_id, panel_x + 8, panel_y + 6)
        bg_id = self.canvas.create_rectangle(
            panel_x,
            panel_y,
            panel_x + width + 16,
            panel_y + height + 12,
            fill="#111827",
            outline=self.accent_color,
            tags=(tag,),
        )
        self.canvas.tag_lower(bg_id, text_id)

    def _draw_annotations(self) -> None:
        self.canvas.delete("annotation")
        for annotation in self.annotations:
            if annotation.tool == "rect" and len(annotation.points) >= 2:
                item_id = self.canvas.create_rectangle(
                    annotation.points[0][0],
                    annotation.points[0][1],
                    annotation.points[1][0],
                    annotation.points[1][1],
                    outline=annotation.color,
                    width=annotation.width,
                    tags=("annotation",),
                )
                annotation.canvas_ids = [item_id]
            elif annotation.tool == "ellipse" and len(annotation.points) >= 2:
                item_id = self.canvas.create_oval(
                    annotation.points[0][0],
                    annotation.points[0][1],
                    annotation.points[1][0],
                    annotation.points[1][1],
                    outline=annotation.color,
                    width=annotation.width,
                    tags=("annotation",),
                )
                annotation.canvas_ids = [item_id]
            elif annotation.tool == "arrow" and len(annotation.points) >= 2:
                item_id = self.canvas.create_line(
                    annotation.points[0][0],
                    annotation.points[0][1],
                    annotation.points[-1][0],
                    annotation.points[-1][1],
                    fill=annotation.color,
                    width=annotation.width,
                    arrow=tk.LAST,
                    tags=("annotation",),
                )
                annotation.canvas_ids = [item_id]
            elif annotation.tool == "line" and len(annotation.points) >= 2:
                item_id = self.canvas.create_line(
                    annotation.points[0][0],
                    annotation.points[0][1],
                    annotation.points[-1][0],
                    annotation.points[-1][1],
                    fill=annotation.color,
                    width=annotation.width,
                    tags=("annotation",),
                )
                annotation.canvas_ids = [item_id]
            elif annotation.tool == "marker" and len(annotation.points) >= 2:
                annotation.canvas_ids = self._draw_pen(
                    annotation.points,
                    ("annotation",),
                    annotation.color,
                    max(8, annotation.width * 4),
                )
            elif annotation.tool == "mosaic" and len(annotation.points) >= 2:
                left, top, right, bottom = ScreenshotManager._normalize_box(
                    (
                        annotation.points[0][0],
                        annotation.points[0][1],
                        annotation.points[1][0],
                        annotation.points[1][1],
                    )
                )
                item_id = self.canvas.create_rectangle(
                    left,
                    top,
                    right,
                    bottom,
                    outline="#111827",
                    width=1,
                    dash=(4, 3),
                    tags=("annotation",),
                )
                annotation.canvas_ids = [item_id]
            elif annotation.tool == "blur" and len(annotation.points) >= 2:
                left, top, right, bottom = ScreenshotManager._normalize_box(
                    (
                        annotation.points[0][0],
                        annotation.points[0][1],
                        annotation.points[1][0],
                        annotation.points[1][1],
                    )
                )
                item_id = self.canvas.create_rectangle(
                    left,
                    top,
                    right,
                    bottom,
                    outline="#111827",
                    width=1,
                    dash=(4, 3),
                    tags=("annotation",),
                )
                annotation.canvas_ids = [item_id]
            elif annotation.tool == "pen":
                annotation.canvas_ids = self._draw_pen(
                    annotation.points, ("annotation",), annotation.color, annotation.width
                )
            elif annotation.tool == "text" and annotation.points and annotation.text:
                item_id = self.canvas.create_text(
                    annotation.points[0][0],
                    annotation.points[0][1],
                    anchor=tk.NW,
                    text=annotation.text,
                    fill=annotation.color,
                    font=(
                        "Microsoft YaHei UI",
                        max(8, int(round(annotation.font_size * 0.72))),
                    ),
                    width=max(
                        20,
                        annotation.points[1][0] - annotation.points[0][0],
                    ) if len(annotation.points) >= 2 else 0,
                    tags=("annotation",),
                )
                annotation.canvas_ids = [item_id]

    def _draw_pen(
        self,
        points: List[Point],
        tags: Tuple[str, ...],
        color: str,
        width: Optional[int] = None,
    ) -> List[int]:
        if len(points) < 2:
            return []

        flattened = [coordinate for point in points for coordinate in point]
        item_id = self.canvas.create_line(
            *flattened,
            fill=color,
            width=width or self.active_width,
            smooth=True,
            tags=tags,
        )
        return [item_id]

    def _confirm(self, event: Optional[tk.Event] = None) -> None:
        self._finish("finish")

    def _finish(self, action: str) -> None:
        if not self.selection_box:
            return

        if self.text_editor:
            self._commit_text_editor()

        left, top, _, _ = ScreenshotManager._normalize_box(self.selection_box)
        pin_position = self.screen_geometry.to_absolute_point((left, top))
        image = AnnotationRenderer.render(
            self.screen_image, self.selection_box, self.annotations
        )
        self._close()
        self.on_capture(image, action, pin_position)

    def _cancel(self, event: Optional[tk.Event] = None) -> None:
        self._close()
        self.on_cancel()

    def _close(self) -> None:
        self.color_copy_key_monitor.stop()
        if self.text_editor:
            self.text_editor.destroy()
            self.text_editor = None
            self.text_editor_restore = None
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        self.window.destroy()

    def _set_cursor(self, cursor: str) -> None:
        try:
            self.window.configure(cursor=cursor)
            self.canvas.configure(cursor=cursor)
        except tk.TclError:
            self.window.configure(cursor="crosshair")
            self.canvas.configure(cursor="crosshair")

    def _hit_toolbar(self, x: int, y: int) -> Optional[str]:
        return self.toolbar.command_at((x, y))

    def _event_point(self, event: tk.Event) -> Point:
        return (
            self._clamp(event.x, 0, self.screen_image.width),
            self._clamp(event.y, 0, self.screen_image.height),
        )

    def _clip_box(self, box: Box) -> Box:
        left, top, right, bottom = box
        return (
            self._clamp(left, 0, self.screen_image.width),
            self._clamp(top, 0, self.screen_image.height),
            self._clamp(right, 0, self.screen_image.width),
            self._clamp(bottom, 0, self.screen_image.height),
        )

    def _clamp_to_selection(self, x: int, y: int) -> Point:
        if not self.selection_box:
            return x, y

        left, top, right, bottom = self.selection_box
        return self._clamp(x, left, right), self._clamp(y, top, bottom)

    @staticmethod
    def _point_in_box(x: int, y: int, box: Box) -> bool:
        left, top, right, bottom = box
        return left <= x <= right and top <= y <= bottom

    @staticmethod
    def _clamp(value: int, minimum: int, maximum: int) -> int:
        return max(minimum, min(maximum, int(value)))


@dataclass
class LongScreenshotSettings:
    max_frames: int = 30
    wheel_notches: int = 4
    settle_ms: int = 350
    min_overlap: int = 60
    stop_when_still: bool = True
    capture_mode: str = "auto"


@dataclass(frozen=True)
class LongStitchMatch:
    overlap: int
    score: float
    quality: str
    static_top: int = 0
    static_bottom: int = 0


@dataclass(frozen=True)
class LongAppendResult:
    state: str
    image: Image.Image
    captured_frames: int
    accepted_frames: int
    added_height: int = 0
    match: Optional[LongStitchMatch] = None


@dataclass(frozen=True)
class LongCaptureOutput:
    image: Image.Image
    source_box: Box
    captured_frames: int
    accepted_frames: int
    quality: str
    partial: bool
    reason: str


class RegionSelectorOverlay:
    """长截图专用区域选择层，只返回屏幕坐标，不进入标注流程。"""

    MIN_SIZE = 40

    def __init__(
        self,
        parent: tk.Tk,
        screen_image: Image.Image,
        on_select: Callable[[Box], None],
        on_cancel: Callable[[], None],
        screen_origin: Point = (0, 0),
    ) -> None:
        self.parent = parent
        self.screen_image = screen_image
        self.screen_geometry = VirtualDesktopGeometry(
            int(screen_origin[0]),
            int(screen_origin[1]),
            screen_image.width,
            screen_image.height,
        )
        self.on_select = on_select
        self.on_cancel = on_cancel
        self.start_x = 0
        self.start_y = 0
        self.selection_box: Optional[Box] = None
        self.selection_photo: Optional[ImageTk.PhotoImage] = None

        self.window = tk.Toplevel(parent)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.geometry(self.screen_geometry.window_geometry)
        self.window.configure(cursor="crosshair")

        dimmed = ImageEnhance.Brightness(screen_image).enhance(0.42)
        self.background = ImageTk.PhotoImage(dimmed)

        self.canvas = tk.Canvas(
            self.window,
            width=screen_image.width,
            height=screen_image.height,
            highlightthickness=0,
            cursor="crosshair",
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.create_image(0, 0, image=self.background, anchor=tk.NW)
        self.canvas.create_text(
            20,
            20,
            anchor=tk.NW,
            text="长截图：选择可滚动内容区域，松开后自动滚动拼接，Esc 取消",
            fill="#ffffff",
            font=("Microsoft YaHei UI", 13, "bold"),
        )

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.window.bind("<Escape>", self._cancel)
        self.window.grab_set()
        self.window.focus_force()

    def _on_press(self, event: tk.Event) -> None:
        self.start_x, self.start_y = self._event_point(event)
        self._draw_selection((self.start_x, self.start_y, self.start_x, self.start_y))

    def _on_drag(self, event: tk.Event) -> None:
        x, y = self._event_point(event)
        self._draw_selection((self.start_x, self.start_y, x, y))

    def _on_release(self, event: tk.Event) -> None:
        x, y = self._event_point(event)
        box = self._clip_box(
            ScreenshotManager._normalize_box((self.start_x, self.start_y, x, y))
        )
        left, top, right, bottom = box
        if right - left < self.MIN_SIZE or bottom - top < self.MIN_SIZE:
            self.canvas.delete("selection")
            self.selection_box = None
            return

        self.selection_box = box
        self._close()
        self.on_select(self.screen_geometry.to_absolute_box(box))

    def _draw_selection(self, box: Box) -> None:
        box = self._clip_box(ScreenshotManager._normalize_box(box))
        left, top, right, bottom = box
        width = right - left
        height = bottom - top

        self.canvas.delete("selection")
        if width < 1 or height < 1:
            return

        preview = self.screen_image.crop((left, top, right, bottom))
        self.selection_photo = ImageTk.PhotoImage(preview)
        self.canvas.create_image(
            left,
            top,
            image=self.selection_photo,
            anchor=tk.NW,
            tags=("selection",),
        )
        self.canvas.create_rectangle(
            left,
            top,
            right,
            bottom,
            outline="#14a3c7",
            width=2,
            tags=("selection",),
        )
        text_id = self.canvas.create_text(
            left + 8,
            top + 8,
            anchor=tk.NW,
            text=f"{width} x {height}",
            fill="#ffffff",
            font=("Microsoft YaHei UI", 10, "bold"),
            tags=("selection",),
        )
        bbox = self.canvas.bbox(text_id)
        if bbox:
            bg_id = self.canvas.create_rectangle(
                bbox[0] - 6,
                bbox[1] - 4,
                bbox[2] + 6,
                bbox[3] + 4,
                fill="#14a3c7",
                outline="#14a3c7",
                tags=("selection",),
            )
            self.canvas.tag_lower(bg_id, text_id)

    def _cancel(self, event: Optional[tk.Event] = None) -> None:
        self._close()
        self.on_cancel()

    def _close(self) -> None:
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        self.window.destroy()

    def _event_point(self, event: tk.Event) -> Point:
        return (
            self._clamp(event.x, 0, self.screen_image.width),
            self._clamp(event.y, 0, self.screen_image.height),
        )

    def _clip_box(self, box: Box) -> Box:
        left, top, right, bottom = box
        return (
            self._clamp(left, 0, self.screen_image.width),
            self._clamp(top, 0, self.screen_image.height),
            self._clamp(right, 0, self.screen_image.width),
            self._clamp(bottom, 0, self.screen_image.height),
        )

    @staticmethod
    def _clamp(value: int, minimum: int, maximum: int) -> int:
        return max(minimum, min(maximum, int(value)))


class MouseWheelScroller:
    """向选区中心发送滚轮事件，用于驱动当前鼠标下的滚动窗口。"""

    MOUSEEVENTF_WHEEL = 0x0800
    WHEEL_DELTA = 120

    @classmethod
    def scroll_down(cls, box: Box, notches: int) -> None:
        if platform.system() != "Windows":
            raise RuntimeError("自动滚动长截图目前仅支持 Windows。")

        left, top, right, bottom = box
        center_x = right - min(12, max(2, (right - left) // 10))
        center_y = top + (bottom - top) // 2
        user32 = ctypes.windll.user32
        user32.SetCursorPos(int(center_x), int(center_y))
        user32.mouse_event(
            cls.MOUSEEVENTF_WHEEL,
            0,
            0,
            -abs(int(notches)) * cls.WHEEL_DELTA,
            0,
        )


class LongScreenshotStitcher:
    """匹配相邻帧的重叠区域，并评估拼接质量。"""

    COMPARE_WIDTH = 320
    COMPARE_HEIGHT = 520
    OVERLAP_SCORE_THRESHOLD = 20.0
    STILL_SCORE_THRESHOLD = 1.2
    STATIC_ROW_THRESHOLD = 2.2

    def stitch(
        self, frames: List[Image.Image], settings: LongScreenshotSettings
    ) -> Image.Image:
        assembler = LongScreenshotAssembler(self)
        for frame in frames:
            result = assembler.append(frame, settings)
            if result.state == "unmatched":
                break
        return assembler.image()

    def is_same_frame(self, first: Image.Image, second: Image.Image) -> bool:
        if first.size != second.size:
            return False
        return self._difference_score(first, second) <= self.STILL_SCORE_THRESHOLD

    def find_overlap(
        self,
        previous: Image.Image,
        current: Image.Image,
        min_overlap: int,
    ) -> Optional[LongStitchMatch]:
        if previous.size != current.size or previous.width < 8 or previous.height < 20:
            return None

        small_previous, scale = self._prepare(previous)
        small_current, _ = self._prepare(current)
        width, height = small_previous.size
        static_top, static_bottom = self._find_static_bands(
            small_previous,
            small_current,
        )
        margin = max(2, int(width * 0.06))
        ignore_top = max(static_top, max(1, int(round(4 * scale))))
        trim_bottom = max(1, int(round(2 * scale)))
        min_overlap_scaled = max(8, int(round(min_overlap * scale)))
        max_overlap = min(
            height - static_bottom - 1,
            int(height * 0.97),
        )
        if max_overlap <= min_overlap_scaled:
            return None

        best_overlap = 0
        best_score = float("inf")
        for overlap in range(max_overlap, min_overlap_scaled - 1, -1):
            score = self._overlap_score(
                small_previous,
                small_current,
                overlap,
                margin,
                ignore_top,
                static_bottom,
                trim_bottom,
            )
            if score is not None and score < best_score:
                best_score = score
                best_overlap = overlap

        if not best_overlap:
            return None

        for overlap in range(
            max(min_overlap_scaled, best_overlap - 3),
            min(max_overlap, best_overlap + 3) + 1,
        ):
            score = self._overlap_score(
                small_previous,
                small_current,
                overlap,
                margin,
                ignore_top,
                static_bottom,
                trim_bottom,
            )
            if score is not None and score < best_score:
                best_score = score
                best_overlap = overlap

        if best_score > self.OVERLAP_SCORE_THRESHOLD:
            return None

        overlap = max(1, min(current.height - 1, int(round(best_overlap / scale))))
        quality = "good"
        if best_score <= 5.0:
            quality = "excellent"
        elif best_score > 12.0:
            quality = "fair"
        return LongStitchMatch(
            overlap=overlap,
            score=best_score,
            quality=quality,
            static_top=max(0, int(round(static_top / scale))),
            static_bottom=max(0, int(round(static_bottom / scale))),
        )

    def _find_overlap(
        self, previous: Image.Image, current: Image.Image, min_overlap: int
    ) -> Optional[int]:
        match = self.find_overlap(previous, current, min_overlap)
        return match.overlap if match else None

    @staticmethod
    def _overlap_score(
        previous: Image.Image,
        current: Image.Image,
        overlap: int,
        margin: int,
        ignore_top: int,
        static_bottom: int,
        trim_bottom: int,
    ) -> Optional[float]:
        usable_height = overlap - ignore_top - trim_bottom
        if usable_height < 8 or previous.width - margin * 2 < 8:
            return None
        previous_start = (
            previous.height
            - static_bottom
            - overlap
            + ignore_top
        )
        previous_end = previous.height - static_bottom - trim_bottom
        if previous_start < 0 or previous_end <= previous_start:
            return None
        previous_part = previous.crop(
            (
                margin,
                previous_start,
                previous.width - margin,
                previous_end,
            )
        )
        current_part = current.crop(
            (
                margin,
                ignore_top,
                current.width - margin,
                overlap - trim_bottom,
            )
        )
        difference = ImageChops.difference(previous_part, current_part)
        return float(ImageStat.Stat(difference).mean[0])

    def _find_static_bands(
        self,
        previous: Image.Image,
        current: Image.Image,
    ) -> Tuple[int, int]:
        difference = ImageChops.difference(previous, current)
        width, height = difference.size
        margin = max(2, int(width * 0.08))
        max_band = max(0, min(48, height // 7))

        def row_score(y: int) -> float:
            row = difference.crop((margin, y, width - margin, y + 1))
            return float(ImageStat.Stat(row).mean[0])

        top = 0
        while top < max_band and row_score(top) <= self.STATIC_ROW_THRESHOLD:
            top += 1
        bottom = 0
        while bottom < max_band and row_score(height - bottom - 1) <= self.STATIC_ROW_THRESHOLD:
            bottom += 1
        return top, bottom

    def _difference_score(self, first: Image.Image, second: Image.Image) -> float:
        small_first, _ = self._prepare(first)
        small_second, _ = self._prepare(second)
        diff = ImageChops.difference(small_first, small_second)
        return ImageStat.Stat(diff).mean[0]

    def _prepare(self, image: Image.Image) -> Tuple[Image.Image, float]:
        scale = min(
            1.0,
            self.COMPARE_WIDTH / max(1, image.width),
            self.COMPARE_HEIGHT / max(1, image.height),
        )
        width = max(1, int(round(image.width * scale)))
        height = max(1, int(image.height * scale))
        resampling = getattr(getattr(Image, "Resampling", Image), "BILINEAR")
        return image.convert("L").resize((width, height), resampling), scale


class LongScreenshotAssembler:
    """边采集边拼接，避免结束时一次性占用大量内存。"""

    MAX_OUTPUT_PIXELS = 100_000_000

    def __init__(self, stitcher: Optional[LongScreenshotStitcher] = None) -> None:
        self.stitcher = stitcher or LongScreenshotStitcher()
        self.previous: Optional[Image.Image] = None
        self.body: Optional[Image.Image] = None
        self.footer: Optional[Image.Image] = None
        self.footer_height = 0
        self.captured_frames = 0
        self.accepted_frames = 0

    def append(
        self,
        frame: Image.Image,
        settings: LongScreenshotSettings,
    ) -> LongAppendResult:
        current = frame.convert("RGB")
        self.captured_frames += 1
        if self.previous is None:
            self.previous = current
            self.body = current.copy()
            self.accepted_frames = 1
            return self._result("first", added_height=current.height)

        if self.stitcher.is_same_frame(self.previous, current):
            return self._result("duplicate")

        match = self.stitcher.find_overlap(
            self.previous,
            current,
            settings.min_overlap,
        )
        if match is None:
            return self._result("unmatched")

        footer_height = match.static_bottom if match.static_bottom >= 3 else 0
        if footer_height:
            footer_height = min(footer_height, current.height - 1)
            if self.footer_height == 0 and self.body is not None:
                self.body = self.body.crop(
                    (0, 0, self.body.width, max(1, self.body.height - footer_height))
                )
            self.footer_height = footer_height
            self.footer = current.crop(
                (0, current.height - footer_height, current.width, current.height)
            )

        content_bottom = current.height - self.footer_height
        if match.overlap >= content_bottom - 1:
            self.previous = current
            return self._result("duplicate", match=match)

        tail = current.crop((0, match.overlap, current.width, content_bottom))
        self.body = self._append_vertical(self.body, tail)
        self.previous = current
        self.accepted_frames += 1
        return self._result(
            "added",
            added_height=tail.height,
            match=match,
        )

    def image(self) -> Image.Image:
        if self.body is None:
            raise ValueError("没有可用的长截图内容。")
        if self.footer is None:
            return self.body
        return self._append_vertical(self.body, self.footer)

    def _result(
        self,
        state: str,
        added_height: int = 0,
        match: Optional[LongStitchMatch] = None,
    ) -> LongAppendResult:
        return LongAppendResult(
            state=state,
            image=self.image(),
            captured_frames=self.captured_frames,
            accepted_frames=self.accepted_frames,
            added_height=added_height,
            match=match,
        )

    @staticmethod
    def _append_vertical(
        first: Optional[Image.Image],
        second: Image.Image,
    ) -> Image.Image:
        if first is None:
            return second.copy()
        if first.width != second.width:
            raise ValueError("长截图帧宽度发生变化，无法继续拼接。")
        output_height = first.height + second.height
        if first.width * output_height > LongScreenshotAssembler.MAX_OUTPUT_PIXELS:
            raise ValueError("长截图尺寸超过安全上限，请缩小选区宽度或提前完成。")
        result = Image.new("RGB", (first.width, output_height))
        result.paste(first, (0, 0))
        result.paste(second, (0, first.height))
        return result


class LongScreenshotCapturePanel:
    """长截图采集浮窗：展示实时结果，并提供暂停、完成和取消。"""

    WIDTH = 300
    HEIGHT = 500
    PREVIEW_WIDTH = 266
    PREVIEW_HEIGHT = 300

    def __init__(
        self,
        root: tk.Tk,
        source_box: Box,
        settings: LongScreenshotSettings,
        on_pause: Callable[[], None],
        on_finish: Callable[[], None],
        on_cancel: Callable[[], None],
    ) -> None:
        self.root = root
        self.source_box = source_box
        self.settings = settings
        self.on_pause = on_pause
        self.on_finish = on_finish
        self.on_cancel = on_cancel
        self.preview_photo: Optional[ImageTk.PhotoImage] = None
        self.drag_start: Point = (0, 0)
        self.window_start: Point = (0, 0)
        self.hidden_for_capture = False

        self.window = tk.Toplevel(root)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.configure(bg="#0f718f")

        frame = tk.Frame(
            self.window,
            bg="#ffffff",
            highlightthickness=1,
            highlightbackground="#0f718f",
        )
        frame.pack(fill=tk.BOTH, expand=True)

        header = tk.Frame(frame, bg="#17212b", height=42)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(
            header,
            text="长截图采集中",
            bg="#17212b",
            fg="#ffffff",
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(side=tk.LEFT, padx=(12, 6))
        mode_text = "自动滚动" if settings.capture_mode == "auto" else "手动滚动"
        tk.Label(
            header,
            text=mode_text,
            bg="#17212b",
            fg="#9dd9e8",
            font=("Microsoft YaHei UI", 9),
        ).pack(side=tk.RIGHT, padx=12)
        for widget in (header, *header.winfo_children()):
            widget.bind("<ButtonPress-1>", self._begin_move)
            widget.bind("<B1-Motion>", self._move)

        body = tk.Frame(frame, bg="#ffffff")
        body.pack(fill=tk.BOTH, expand=True, padx=12, pady=(10, 12))
        self.preview = tk.Canvas(
            body,
            width=self.PREVIEW_WIDTH,
            height=self.PREVIEW_HEIGHT,
            bg="#eef2f5",
            highlightthickness=1,
            highlightbackground="#cbd5df",
        )
        self.preview.pack(fill=tk.X)

        self.size_var = tk.StringVar(value="等待第一帧")
        self.status_var = tk.StringVar(value="准备采集")
        tk.Label(
            body,
            textvariable=self.size_var,
            bg="#ffffff",
            fg="#243442",
            anchor=tk.W,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(fill=tk.X, pady=(9, 1))
        self.status_label = tk.Label(
            body,
            textvariable=self.status_var,
            bg="#ffffff",
            fg="#4b6475",
            anchor=tk.W,
            font=("Microsoft YaHei UI", 9),
        )
        self.status_label.pack(fill=tk.X)
        self.progress = ttk.Progressbar(
            body,
            mode="determinate",
            maximum=max(1, settings.max_frames),
            value=0,
        )
        self.progress.pack(fill=tk.X, pady=(8, 10))

        actions = tk.Frame(body, bg="#ffffff")
        actions.pack(fill=tk.X)
        self.pause_button = ttk.Button(actions, text="暂停", command=self.on_pause)
        self.pause_button.pack(side=tk.LEFT)
        ttk.Button(actions, text="取消", command=self.on_cancel).pack(
            side=tk.RIGHT,
            padx=(6, 0),
        )
        ttk.Button(actions, text="完成", command=self.on_finish).pack(side=tk.RIGHT)

        self.window.bind("<Return>", lambda event: self.on_finish())
        self.window.bind("<Escape>", lambda event: self.on_cancel())
        self._place_outside_source()

    def update_result(
        self,
        result: LongAppendResult,
        message: str,
        quality: str = "good",
    ) -> None:
        image = result.image
        self.size_var.set(
            f"{image.width} × {image.height}    已拼接 {result.accepted_frames} 段"
        )
        self.status_var.set(message)
        self.status_label.configure(
            fg={
                "excellent": "#16794b",
                "good": "#16794b",
                "fair": "#a16207",
                "error": "#c53030",
            }.get(
                quality,
                "#4b6475",
            )
        )
        self.progress.configure(value=min(result.captured_frames, self.settings.max_frames))
        self._render_preview(image)

    def set_paused(self, paused: bool) -> None:
        self.pause_button.configure(text="继续" if paused else "暂停")
        if paused:
            self.status_var.set("已暂停")
            self.status_label.configure(fg="#a16207")

    def capture_without_panel(self, callback: Callable[[], None]) -> None:
        self._update_overlap_state()
        if self.overlaps_source:
            self.hidden_for_capture = True
            self.window.withdraw()
            self.root.after(70, callback)
        else:
            callback()

    def show_after_capture(self) -> None:
        if self.hidden_for_capture:
            self.hidden_for_capture = False
            self.window.deiconify()
            self.window.lift()

    def close(self) -> None:
        try:
            self.window.destroy()
        except tk.TclError:
            pass

    def _render_preview(self, image: Image.Image) -> None:
        self.preview.delete("all")
        scale = min(1.0, self.PREVIEW_WIDTH / max(1, image.width))
        width = max(1, int(round(image.width * scale)))
        height = max(1, int(round(image.height * scale)))
        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        rendered = image.resize((width, height), resampling)
        if rendered.height > self.PREVIEW_HEIGHT:
            rendered = rendered.crop(
                (0, rendered.height - self.PREVIEW_HEIGHT, rendered.width, rendered.height)
            )
        self.preview_photo = ImageTk.PhotoImage(rendered)
        self.preview.create_image(
            self.PREVIEW_WIDTH // 2,
            self.PREVIEW_HEIGHT,
            image=self.preview_photo,
            anchor=tk.S,
        )

    def _place_outside_source(self) -> None:
        desktop = VirtualDesktopGeometry.detect()
        left, top, right, bottom = self.source_box
        y = max(desktop.top + 12, min(top, desktop.top + desktop.height - self.HEIGHT - 12))
        candidates = (
            right + 12,
            left - self.WIDTH - 12,
            desktop.left + desktop.width - self.WIDTH - 12,
            desktop.left + 12,
        )
        x = desktop.left + 12
        for candidate in candidates:
            if desktop.left + 8 <= candidate <= desktop.left + desktop.width - self.WIDTH - 8:
                panel_box = (candidate, y, candidate + self.WIDTH, y + self.HEIGHT)
                if not self._intersects(panel_box, self.source_box):
                    x = candidate
                    break
        self.window.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")
        self.window.update_idletasks()
        self._update_overlap_state()

    def _begin_move(self, event: tk.Event) -> None:
        self.drag_start = (event.x_root, event.y_root)
        self.window_start = (self.window.winfo_x(), self.window.winfo_y())

    def _move(self, event: tk.Event) -> None:
        x = self.window_start[0] + event.x_root - self.drag_start[0]
        y = self.window_start[1] + event.y_root - self.drag_start[1]
        self.window.geometry(f"+{x}+{y}")
        self._update_overlap_state()

    def _update_overlap_state(self) -> None:
        panel_box = (
            self.window.winfo_x(),
            self.window.winfo_y(),
            self.window.winfo_x() + max(1, self.window.winfo_width()),
            self.window.winfo_y() + max(1, self.window.winfo_height()),
        )
        self.overlaps_source = self._intersects(panel_box, self.source_box)

    @staticmethod
    def _intersects(first: Box, second: Box) -> bool:
        return not (
            first[2] <= second[0]
            or first[0] >= second[2]
            or first[3] <= second[1]
            or first[1] >= second[3]
        )


class LongScreenshotController:
    """控制自动/手动滚动、稳定检测、增量拼接和随时停止。"""

    def __init__(
        self,
        root: tk.Tk,
        box: Box,
        settings: LongScreenshotSettings,
        on_progress: Callable[[str], None],
        on_complete: Callable[[LongCaptureOutput], None],
        on_error: Callable[[Exception], None],
        on_cancel: Optional[Callable[[], None]] = None,
        capture_func: Optional[Callable[[], Image.Image]] = None,
        scroll_func: Optional[Callable[[Box, int], None]] = None,
        panel_factory: Optional[Callable[..., LongScreenshotCapturePanel]] = None,
    ) -> None:
        self.root = root
        self.box = ScreenshotManager._normalize_box(box)
        self.settings = settings
        self.on_progress = on_progress
        self.on_complete = on_complete
        self.on_error = on_error
        self.on_cancel = on_cancel
        self.capture_func = capture_func or self._capture_region
        self.scroll_func = scroll_func or MouseWheelScroller.scroll_down
        self.panel_factory = panel_factory or LongScreenshotCapturePanel
        self.stitcher = LongScreenshotStitcher()
        self.assembler = LongScreenshotAssembler(self.stitcher)
        self.panel: Optional[LongScreenshotCapturePanel] = None
        self.running = False
        self.paused = False
        self.partial = False
        self.quality_warnings = 0
        self.manual_probe: Optional[Image.Image] = None
        self.scheduled_job: Optional[str] = None
        self.pending_step: Optional[Callable[[], None]] = None

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.panel = self.panel_factory(
            self.root,
            self.box,
            self.settings,
            self.toggle_pause,
            self.stop,
            self.cancel,
        )
        mode_text = "自动滚动" if self.settings.capture_mode == "auto" else "手动滚动"
        self.on_progress(f"长截图已启动：{mode_text}")
        self._schedule_step(self._capture_next, 280)

    def stop(self) -> None:
        if not self.running:
            return
        if self.assembler.accepted_frames:
            self._finish("user")
        else:
            self.cancel()

    def cancel(self) -> None:
        if not self.running:
            return
        self._stop_running()
        if self.on_cancel:
            self.on_cancel()

    def toggle_pause(self) -> None:
        if not self.running:
            return
        self.paused = not self.paused
        if self.panel:
            self.panel.set_paused(self.paused)
        self.on_progress("长截图已暂停" if self.paused else "长截图继续采集")
        if not self.paused and self.pending_step and self.scheduled_job is None:
            self._schedule_step(self.pending_step, 80)

    def _capture_next(self) -> None:
        if not self.running:
            return
        if self.panel:
            self.panel.capture_without_panel(self._capture_and_process)
        else:
            self._capture_and_process()

    def _capture_and_process(self) -> None:
        if not self.running:
            return
        try:
            frame = self.capture_func()
            if self.panel:
                self.panel.show_after_capture()

            if self.settings.capture_mode == "manual" and self.assembler.previous is not None:
                if self.stitcher.is_same_frame(self.assembler.previous, frame):
                    self._update_waiting("等待手动滚动")
                    self._schedule_manual_poll()
                    return
                if self.manual_probe is None or not self.stitcher.is_same_frame(
                    self.manual_probe,
                    frame,
                ):
                    self.manual_probe = frame
                    self._update_waiting("等待画面稳定")
                    self._schedule_manual_poll()
                    return
                self.manual_probe = None

            result = self.assembler.append(frame, self.settings)
            self._handle_append_result(result)
        except Exception as exc:
            self._fail(exc)

    def _handle_append_result(self, result: LongAppendResult) -> None:
        if result.state in {"first", "added"}:
            quality = result.match.quality if result.match else "good"
            if quality == "fair":
                self.quality_warnings += 1
            message = "首屏已捕获" if result.state == "first" else f"新增 {result.added_height} 像素"
            if self.panel:
                self.panel.update_result(result, message, quality)
            self.on_progress(
                f"长截图采集中：{result.accepted_frames} 段，{result.image.height} 像素高"
            )
            if result.captured_frames >= self.settings.max_frames:
                self._finish("limit")
            elif self.settings.capture_mode == "auto":
                self._schedule_step(self._scroll_once, 120)
            else:
                self._schedule_manual_poll()
            return

        if result.state == "duplicate":
            if self.settings.capture_mode == "auto" and self.settings.stop_when_still:
                self._finish("end")
            else:
                self._update_waiting("画面未变化")
                self._schedule_manual_poll()
            return

        self.partial = self.assembler.accepted_frames > 1
        if self.panel:
            self.panel.update_result(result, "当前帧无法可靠匹配", "error")
        if self.partial:
            self._finish("unmatched")
        else:
            self._fail(RuntimeError("前两帧无法匹配，请减小滚动距离或重新选择区域。"))

    def _scroll_once(self) -> None:
        if not self.running:
            return
        try:
            self.scroll_func(self.box, self.settings.wheel_notches)
            self._schedule_step(self._capture_next, self.settings.settle_ms)
        except Exception as exc:
            self._fail(exc)

    def _schedule_manual_poll(self) -> None:
        self._schedule_step(
            self._capture_next,
            max(180, self.settings.settle_ms // 2),
        )

    def _update_waiting(self, message: str) -> None:
        if self.panel and self.assembler.accepted_frames:
            result = LongAppendResult(
                state="duplicate",
                image=self.assembler.image(),
                captured_frames=self.assembler.captured_frames,
                accepted_frames=self.assembler.accepted_frames,
            )
            self.panel.update_result(result, message, "fair")
        self.on_progress(message)

    def _schedule_step(self, step: Callable[[], None], delay_ms: int) -> None:
        self.pending_step = step
        if self.scheduled_job:
            try:
                self.root.after_cancel(self.scheduled_job)
            except tk.TclError:
                pass
        self.scheduled_job = self.root.after(max(0, int(delay_ms)), self._run_step)

    def _run_step(self) -> None:
        self.scheduled_job = None
        if not self.running or self.paused:
            return
        step = self.pending_step
        self.pending_step = None
        if step:
            step()

    def _capture_region(self) -> Image.Image:
        return ScreenshotManager.grab_box(self.box)

    def _finish(self, reason: str) -> None:
        if not self.running:
            return
        image = self.assembler.image()
        output = LongCaptureOutput(
            image=image,
            source_box=self.box,
            captured_frames=self.assembler.captured_frames,
            accepted_frames=self.assembler.accepted_frames,
            quality="fair" if self.quality_warnings or self.partial else "good",
            partial=self.partial,
            reason=reason,
        )
        self._stop_running()
        self.on_complete(output)

    def _fail(self, exc: Exception) -> None:
        if self.running and self.assembler.accepted_frames > 1:
            self.partial = True
            self.on_progress(f"长截图提前停止：{exc}")
            self._finish("error")
            return
        self._stop_running()
        self.on_error(exc)

    def _stop_running(self) -> None:
        self.running = False
        self.paused = False
        if self.scheduled_job:
            try:
                self.root.after_cancel(self.scheduled_job)
            except tk.TclError:
                pass
        self.scheduled_job = None
        self.pending_step = None
        if self.panel:
            self.panel.close()
            self.panel = None


class LongScreenshotOptionsDialog(tk.Toplevel):
    """长截图参数窗口，避免把滚动次数等设置写死。"""

    def __init__(self, parent: tk.Tk) -> None:
        super().__init__(parent)
        self.title("长截图设置")
        self.resizable(False, False)
        self.transient(parent)
        self.result: Optional[LongScreenshotSettings] = None

        self.capture_mode_var = tk.StringVar(value="auto")
        self.max_frames_var = tk.IntVar(value=30)
        self.wheel_notches_var = tk.IntVar(value=4)
        self.settle_ms_var = tk.IntVar(value=350)
        self.stop_when_still_var = tk.BooleanVar(value=True)

        body = ttk.Frame(self, padding=16)
        body.pack(fill=tk.BOTH, expand=True)

        ttk.Label(body, text="采集方式").grid(row=0, column=0, sticky=tk.W, pady=6)
        mode_frame = ttk.Frame(body)
        mode_frame.grid(row=0, column=1, sticky=tk.W, pady=6)
        ttk.Radiobutton(
            mode_frame,
            text="自动滚动",
            value="auto",
            variable=self.capture_mode_var,
            command=self._sync_mode_controls,
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            mode_frame,
            text="手动滚动",
            value="manual",
            variable=self.capture_mode_var,
            command=self._sync_mode_controls,
        ).pack(side=tk.LEFT, padx=(10, 0))

        ttk.Label(body, text="最大截图张数").grid(row=1, column=0, sticky=tk.W, pady=6)
        ttk.Spinbox(
            body,
            from_=2,
            to=100,
            textvariable=self.max_frames_var,
            width=8,
            justify=tk.CENTER,
        ).grid(row=1, column=1, sticky=tk.W, pady=6)

        ttk.Label(body, text="每次滚轮格数").grid(row=2, column=0, sticky=tk.W, pady=6)
        self.wheel_spinbox = ttk.Spinbox(
            body,
            from_=1,
            to=10,
            textvariable=self.wheel_notches_var,
            width=8,
            justify=tk.CENTER,
        )
        self.wheel_spinbox.grid(row=2, column=1, sticky=tk.W, pady=6)

        ttk.Label(body, text="画面稳定等待毫秒").grid(row=3, column=0, sticky=tk.W, pady=6)
        ttk.Spinbox(
            body,
            from_=150,
            to=2000,
            increment=50,
            textvariable=self.settle_ms_var,
            width=8,
            justify=tk.CENTER,
        ).grid(row=3, column=1, sticky=tk.W, pady=6)

        self.stop_checkbutton = ttk.Checkbutton(
            body,
            text="检测到底部自动停止",
            variable=self.stop_when_still_var,
        )
        self.stop_checkbutton.grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=(8, 4))

        ttk.Label(
            body,
            text="选择网页、文档或聊天列表的可滚动内容区域。",
            style="Muted.TLabel",
        ).grid(row=5, column=0, columnspan=2, sticky=tk.W, pady=(2, 12))

        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, columnspan=2, sticky=tk.E)
        ttk.Button(buttons, text="取消", command=self._cancel).pack(
            side=tk.RIGHT, padx=(8, 0)
        )
        ttk.Button(buttons, text="开始选择", command=self._accept).pack(side=tk.RIGHT)

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda event: self._cancel())
        self.bind("<Return>", lambda event: self._accept())
        self.grab_set()
        self._sync_mode_controls()
        self._center(parent)
        self.focus_force()

    @classmethod
    def ask(cls, parent: tk.Tk) -> Optional[LongScreenshotSettings]:
        dialog = cls(parent)
        parent.wait_window(dialog)
        return dialog.result

    def _accept(self) -> None:
        try:
            max_frames = max(2, min(100, int(self.max_frames_var.get())))
            wheel_notches = max(1, min(10, int(self.wheel_notches_var.get())))
            settle_ms = max(150, min(2000, int(self.settle_ms_var.get())))
        except (tk.TclError, ValueError):
            messagebox.showwarning("提示", "长截图参数必须是数字。", parent=self)
            return

        self.result = LongScreenshotSettings(
            max_frames=max_frames,
            wheel_notches=wheel_notches,
            settle_ms=settle_ms,
            min_overlap=60,
            stop_when_still=bool(self.stop_when_still_var.get()),
            capture_mode=(
                self.capture_mode_var.get()
                if self.capture_mode_var.get() in {"auto", "manual"}
                else "auto"
            ),
        )
        self.destroy()

    def _sync_mode_controls(self) -> None:
        state = "normal" if self.capture_mode_var.get() == "auto" else "disabled"
        self.wheel_spinbox.configure(state=state)
        self.stop_checkbutton.configure(state=state)

    def _cancel(self) -> None:
        self.result = None
        self.destroy()

    def _center(self, parent: tk.Tk) -> None:
        self.update_idletasks()
        parent_x = parent.winfo_rootx()
        parent_y = parent.winfo_rooty()
        parent_width = max(1, parent.winfo_width())
        parent_height = max(1, parent.winfo_height())
        width = self.winfo_width()
        height = self.winfo_height()
        x = parent_x + (parent_width - width) // 2
        y = parent_y + (parent_height - height) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")


class LongScreenshotResultDialog(tk.Toplevel):
    """手机式长截图结果页，支持滚动预览和上下范围裁剪。"""

    OVERVIEW_WIDTH = 118
    OVERVIEW_HEIGHT = 540
    MIN_CROP_HEIGHT = 24

    def __init__(
        self,
        parent: tk.Tk,
        output: LongCaptureOutput,
        on_action: Callable[[Image.Image, str, int], None],
        on_cancel: Callable[[], None],
    ) -> None:
        super().__init__(parent)
        self.title("长截图结果")
        self.output = output
        self.image = output.image.convert("RGB")
        self.on_action = on_action
        self.on_cancel = on_cancel
        self.crop_top = 0
        self.crop_bottom = self.image.height
        self.drag_handle: Optional[str] = None
        self.preview_photo: Optional[ImageTk.PhotoImage] = None
        self.overview_photo: Optional[ImageTk.PhotoImage] = None
        self.preview_scale = 1.0
        self.preview_x = 12
        self.overview_scale = 1.0
        self.overview_x = 0
        self.overview_y = 0
        self.render_job: Optional[str] = None

        desktop = VirtualDesktopGeometry.detect()
        width = min(900, max(700, desktop.width - 80))
        height = min(740, max(560, desktop.height - 80))
        self.geometry(f"{width}x{height}")
        self.minsize(680, 540)
        self.attributes("-topmost", True)

        header = ttk.Frame(self, padding=(14, 11))
        header.pack(fill=tk.X)
        if output.partial:
            quality_text = "部分完成"
        elif output.quality == "fair":
            quality_text = "拼接完成，请检查接缝"
        else:
            quality_text = "拼接完成"
        self.summary_var = tk.StringVar()
        ttk.Label(
            header,
            text=quality_text,
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(side=tk.LEFT)
        ttk.Label(header, textvariable=self.summary_var).pack(side=tk.RIGHT)

        separator = ttk.Separator(self, orient=tk.HORIZONTAL)
        separator.pack(fill=tk.X)

        content = ttk.Frame(self, padding=12)
        content.pack(fill=tk.BOTH, expand=True)
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)

        preview_frame = ttk.Frame(content)
        preview_frame.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 12))
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        self.preview_canvas = tk.Canvas(
            preview_frame,
            bg="#dfe5ea",
            highlightthickness=0,
        )
        self.preview_canvas.grid(row=0, column=0, sticky=tk.NSEW)
        scrollbar = ttk.Scrollbar(
            preview_frame,
            orient=tk.VERTICAL,
            command=self.preview_canvas.yview,
        )
        scrollbar.grid(row=0, column=1, sticky=tk.NS)
        self.preview_canvas.configure(yscrollcommand=scrollbar.set)
        self.preview_canvas.bind("<Configure>", self._schedule_render)

        overview_frame = ttk.Frame(content)
        overview_frame.grid(row=0, column=1, sticky=tk.NS)
        ttk.Label(
            overview_frame,
            text="裁剪范围",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(anchor=tk.W, pady=(0, 7))
        self.overview_canvas = tk.Canvas(
            overview_frame,
            width=self.OVERVIEW_WIDTH,
            height=self.OVERVIEW_HEIGHT,
            bg="#eef2f5",
            highlightthickness=1,
            highlightbackground="#c7d1da",
            cursor="sb_v_double_arrow",
        )
        self.overview_canvas.pack()
        self.overview_canvas.bind("<ButtonPress-1>", self._begin_crop_drag)
        self.overview_canvas.bind("<B1-Motion>", self._drag_crop)
        self.overview_canvas.bind("<ButtonRelease-1>", self._end_crop_drag)

        footer = ttk.Frame(self, padding=(12, 4, 12, 12))
        footer.pack(fill=tk.X)
        ttk.Button(footer, text="取消", command=self._cancel).pack(side=tk.RIGHT)
        ttk.Button(footer, text="完成", command=lambda: self._accept("finish")).pack(
            side=tk.RIGHT,
            padx=(0, 8),
        )
        ttk.Button(footer, text="复制", command=lambda: self._accept("copy")).pack(
            side=tk.RIGHT,
            padx=(0, 8),
        )
        ttk.Button(footer, text="保存", command=lambda: self._accept("save")).pack(
            side=tk.RIGHT,
            padx=(0, 8),
        )
        ttk.Button(footer, text="贴图", command=lambda: self._accept("pin")).pack(
            side=tk.RIGHT,
            padx=(0, 8),
        )

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", self._cancel)
        self.bind("<Return>", lambda event: self._accept("finish"))
        self.preview_canvas.bind("<MouseWheel>", self._preview_wheel)
        self.update_idletasks()
        self._center_at_pointer(desktop)
        self._render_images()
        self.grab_set()
        self.focus_force()

    def _render_images(self) -> None:
        self.render_job = None
        y_position = self.preview_canvas.yview()[0] if self.preview_canvas.find_all() else 0.0
        available_width = max(260, self.preview_canvas.winfo_width() - 24)
        self.preview_scale = min(
            1.0,
            available_width / max(1, self.image.width),
            30000 / max(1, self.image.height),
        )
        preview_size = (
            max(1, int(round(self.image.width * self.preview_scale))),
            max(1, int(round(self.image.height * self.preview_scale))),
        )
        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        preview = self.image.resize(preview_size, resampling)
        self.preview_photo = ImageTk.PhotoImage(preview)
        self.preview_canvas.delete("all")
        self.preview_x = max(
            12,
            (max(1, self.preview_canvas.winfo_width()) - preview.width) // 2,
        )
        self.preview_canvas.create_image(
            self.preview_x,
            12,
            image=self.preview_photo,
            anchor=tk.NW,
            tags=("long_image",),
        )
        self.preview_canvas.configure(
            scrollregion=(
                0,
                0,
                max(self.preview_canvas.winfo_width(), self.preview_x + preview.width + 12),
                preview.height + 24,
            )
        )
        self.preview_canvas.yview_moveto(y_position)

        self.overview_scale = min(
            (self.OVERVIEW_WIDTH - 16) / max(1, self.image.width),
            (self.OVERVIEW_HEIGHT - 16) / max(1, self.image.height),
        )
        overview_size = (
            max(1, int(round(self.image.width * self.overview_scale))),
            max(1, int(round(self.image.height * self.overview_scale))),
        )
        overview = self.image.resize(overview_size, resampling)
        self.overview_photo = ImageTk.PhotoImage(overview)
        self.overview_x = (self.OVERVIEW_WIDTH - overview.width) // 2
        self.overview_y = (self.OVERVIEW_HEIGHT - overview.height) // 2
        self.overview_canvas.delete("all")
        self.overview_canvas.create_image(
            self.overview_x,
            self.overview_y,
            image=self.overview_photo,
            anchor=tk.NW,
            tags=("overview_image",),
        )
        self._draw_crop_guides()

    def _schedule_render(self, event: Optional[tk.Event] = None) -> None:
        if self.render_job:
            try:
                self.after_cancel(self.render_job)
            except tk.TclError:
                pass
        self.render_job = self.after(80, self._render_images)

    def _draw_crop_guides(self) -> None:
        self.preview_canvas.delete("crop_guide")
        self.overview_canvas.delete("crop_guide")
        preview_left = self.preview_x
        preview_right = preview_left + int(round(self.image.width * self.preview_scale))
        preview_top = 12 + int(round(self.crop_top * self.preview_scale))
        preview_bottom = 12 + int(round(self.crop_bottom * self.preview_scale))
        full_bottom = 12 + int(round(self.image.height * self.preview_scale))
        if preview_top > 12:
            self.preview_canvas.create_rectangle(
                preview_left,
                12,
                preview_right,
                preview_top,
                fill="#4b5563",
                stipple="gray50",
                outline="",
                tags=("crop_guide",),
            )
        if preview_bottom < full_bottom:
            self.preview_canvas.create_rectangle(
                preview_left,
                preview_bottom,
                preview_right,
                full_bottom,
                fill="#4b5563",
                stipple="gray50",
                outline="",
                tags=("crop_guide",),
            )
        for y in (preview_top, preview_bottom):
            self.preview_canvas.create_line(
                preview_left,
                y,
                preview_right,
                y,
                fill="#0b93bd",
                width=2,
                tags=("crop_guide",),
            )

        left = self.overview_x
        right = left + int(round(self.image.width * self.overview_scale))
        top = self.overview_y + int(round(self.crop_top * self.overview_scale))
        bottom = self.overview_y + int(round(self.crop_bottom * self.overview_scale))
        image_bottom = self.overview_y + int(round(self.image.height * self.overview_scale))
        if top > self.overview_y:
            self.overview_canvas.create_rectangle(
                left,
                self.overview_y,
                right,
                top,
                fill="#1f2933",
                stipple="gray50",
                outline="",
                tags=("crop_guide",),
            )
        if bottom < image_bottom:
            self.overview_canvas.create_rectangle(
                left,
                bottom,
                right,
                image_bottom,
                fill="#1f2933",
                stipple="gray50",
                outline="",
                tags=("crop_guide",),
            )
        self.overview_canvas.create_rectangle(
            left - 2,
            top,
            right + 2,
            bottom,
            outline="#0b93bd",
            width=2,
            tags=("crop_guide",),
        )
        for y in (top, bottom):
            self.overview_canvas.create_rectangle(
                left - 5,
                y - 4,
                right + 5,
                y + 4,
                fill="#0b93bd",
                outline="#ffffff",
                tags=("crop_guide",),
            )
        self.summary_var.set(
            f"{self.image.width} × {self.crop_bottom - self.crop_top}  ·  {self.output.accepted_frames} 段"
        )

    def _begin_crop_drag(self, event: tk.Event) -> str:
        top_y = self.overview_y + self.crop_top * self.overview_scale
        bottom_y = self.overview_y + self.crop_bottom * self.overview_scale
        self.drag_handle = "top" if abs(event.y - top_y) <= abs(event.y - bottom_y) else "bottom"
        self._set_crop_from_overview(event.y)
        return "break"

    def _drag_crop(self, event: tk.Event) -> str:
        if self.drag_handle:
            self._set_crop_from_overview(event.y)
        return "break"

    def _end_crop_drag(self, event: Optional[tk.Event] = None) -> str:
        self.drag_handle = None
        return "break"

    def _set_crop_from_overview(self, y: int) -> None:
        value = int(round((y - self.overview_y) / max(self.overview_scale, 0.0001)))
        value = max(0, min(self.image.height, value))
        if self.drag_handle == "top":
            self.crop_top = min(value, self.crop_bottom - self.MIN_CROP_HEIGHT)
        elif self.drag_handle == "bottom":
            self.crop_bottom = max(value, self.crop_top + self.MIN_CROP_HEIGHT)
        self._draw_crop_guides()
        target_y = (
            self.crop_top if self.drag_handle == "top" else self.crop_bottom
        ) * self.preview_scale
        scroll_height = max(1, int(self.image.height * self.preview_scale))
        self.preview_canvas.yview_moveto(max(0.0, min(1.0, target_y / scroll_height)))

    def _preview_wheel(self, event: tk.Event) -> str:
        self.preview_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def _accept(self, action: str) -> None:
        cropped = self.image.crop(
            (0, self.crop_top, self.image.width, self.crop_bottom)
        )
        crop_top = self.crop_top
        self._close()
        self.on_action(cropped, action, crop_top)

    def _cancel(self, event: Optional[tk.Event] = None) -> None:
        self._close()
        self.on_cancel()

    def _close(self) -> None:
        if self.render_job:
            try:
                self.after_cancel(self.render_job)
            except tk.TclError:
                pass
            self.render_job = None
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()

    def _center_at_pointer(self, desktop: VirtualDesktopGeometry) -> None:
        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        pointer_x = self.winfo_pointerx()
        pointer_y = self.winfo_pointery()
        x = max(
            desktop.left,
            min(pointer_x - width // 2, desktop.left + desktop.width - width),
        )
        y = max(
            desktop.top,
            min(pointer_y - height // 2, desktop.top + desktop.height - height),
        )
        self.geometry(f"+{x}+{y}")


@dataclass(frozen=True)
class HotkeyActionDefinition:
    identifier: str
    label: str
    default: str
    global_hotkey: bool = True


HOTKEY_ACTIONS = (
    HotkeyActionDefinition("region", "框选截图", "Ctrl+Shift+A"),
    HotkeyActionDefinition("full", "全屏截图", "Ctrl+Shift+F"),
    HotkeyActionDefinition("active", "活动窗口", "Ctrl+Shift+W"),
    HotkeyActionDefinition("long", "长截图", "Ctrl+Shift+L"),
    HotkeyActionDefinition("color", "取色", "Ctrl+Shift+C"),
    HotkeyActionDefinition("whiteboard", "白板", "Ctrl+Shift+B"),
    HotkeyActionDefinition("paste", "剪贴板贴图", "F3"),
    HotkeyActionDefinition("toggle_pins", "显示/隐藏贴图", "Shift+F3"),
    HotkeyActionDefinition("pin_current", "贴出当前图片", "Ctrl+T", False),
    HotkeyActionDefinition("quick_save", "快捷保存", "Ctrl+Shift+S", False),
    HotkeyActionDefinition("preferences", "打开首选项", "Ctrl+Shift+P", False),
    HotkeyActionDefinition("save_as", "当前图片另存为", "Ctrl+S", False),
    HotkeyActionDefinition("copy_current", "复制当前图片", "Ctrl+C", False),
    HotkeyActionDefinition("ocr_current", "提取当前图片文字", "Ctrl+Shift+O", False),
)


def default_hotkey_map() -> dict[str, str]:
    return {definition.identifier: definition.default for definition in HOTKEY_ACTIONS}


class HotkeyCodec:
    """规范化快捷键文本，并转换为 Win32/Tk 可使用的键值。"""

    MODIFIER_ORDER = ("Ctrl", "Alt", "Shift", "Win")
    MODIFIER_ALIASES = {
        "ctrl": "Ctrl",
        "control": "Ctrl",
        "alt": "Alt",
        "shift": "Shift",
        "win": "Win",
        "windows": "Win",
        "meta": "Win",
    }
    KEY_ALIASES = {
        "return": "Enter",
        "enter": "Enter",
        "escape": "Esc",
        "esc": "Esc",
        "space": "Space",
        "tab": "Tab",
        "backspace": "Backspace",
        "delete": "Delete",
        "insert": "Insert",
        "home": "Home",
        "end": "End",
        "prior": "PageUp",
        "pageup": "PageUp",
        "next": "PageDown",
        "pagedown": "PageDown",
        "up": "Up",
        "down": "Down",
        "left": "Left",
        "right": "Right",
        "print": "PrintScreen",
        "snapshot": "PrintScreen",
        "printscreen": "PrintScreen",
    }
    VIRTUAL_KEYS = {
        "Enter": 0x0D,
        "Esc": 0x1B,
        "Space": 0x20,
        "Tab": 0x09,
        "Backspace": 0x08,
        "Delete": 0x2E,
        "Insert": 0x2D,
        "Home": 0x24,
        "End": 0x23,
        "PageUp": 0x21,
        "PageDown": 0x22,
        "Up": 0x26,
        "Down": 0x28,
        "Left": 0x25,
        "Right": 0x27,
        "PrintScreen": 0x2C,
    }
    TK_KEYS = {
        "Enter": "Return",
        "Esc": "Escape",
        "Space": "space",
        "Tab": "Tab",
        "Backspace": "BackSpace",
        "Delete": "Delete",
        "Insert": "Insert",
        "Home": "Home",
        "End": "End",
        "PageUp": "Prior",
        "PageDown": "Next",
        "Up": "Up",
        "Down": "Down",
        "Left": "Left",
        "Right": "Right",
        "PrintScreen": "Print",
    }

    @classmethod
    def normalize(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value:
            return ""
        tokens = [token.strip() for token in value.split("+") if token.strip()]
        modifiers: set[str] = set()
        key: Optional[str] = None
        for token in tokens:
            lowered = token.lower()
            modifier = cls.MODIFIER_ALIASES.get(lowered)
            if modifier:
                modifiers.add(modifier)
                continue
            if key is not None:
                raise ValueError("一个快捷键只能包含一个普通按键。")
            key = cls._normalize_key(token)
        if key is None:
            raise ValueError("快捷键缺少普通按键。")
        ordered = [name for name in cls.MODIFIER_ORDER if name in modifiers]
        return "+".join([*ordered, key])

    @classmethod
    def to_windows(cls, value: str) -> Tuple[int, int]:
        normalized = cls.normalize(value)
        if not normalized:
            raise ValueError("快捷键未设置。")
        tokens = normalized.split("+")
        key = tokens[-1]
        modifiers = 0
        for token in tokens[:-1]:
            modifiers |= {
                "Alt": 0x0001,
                "Ctrl": 0x0002,
                "Shift": 0x0004,
                "Win": 0x0008,
            }[token]
        return modifiers, cls._virtual_key(key)

    @classmethod
    def to_tk_sequence(cls, value: str) -> Optional[str]:
        normalized = cls.normalize(value)
        if not normalized:
            return None
        tokens = normalized.split("+")
        key = tokens[-1]
        modifiers = []
        for token in tokens[:-1]:
            modifiers.append(
                {
                    "Ctrl": "Control",
                    "Alt": "Alt",
                    "Shift": "Shift",
                    "Win": "Mod4",
                }[token]
            )
        tk_key = cls.TK_KEYS.get(key, key)
        if len(key) == 1 and key.isalpha() and "Shift" not in tokens[:-1]:
            tk_key = key.lower()
        sequence = "-".join([*modifiers, tk_key])
        return f"<{sequence}>"

    @classmethod
    def from_tk_event(cls, event: tk.Event) -> Optional[str]:
        if event.keysym in {
            "Control_L",
            "Control_R",
            "Shift_L",
            "Shift_R",
            "Alt_L",
            "Alt_R",
            "Meta_L",
            "Meta_R",
            "Super_L",
            "Super_R",
        }:
            return None
        modifiers = []
        if platform.system() == "Windows":
            modifiers.extend(cls._windows_modifiers())
        else:
            if event.state & 0x0004:
                modifiers.append("Ctrl")
            if event.state & 0x0008:
                modifiers.append("Alt")
            if event.state & 0x0001:
                modifiers.append("Shift")
        key = cls._normalize_key(str(event.keysym))
        return cls.normalize("+".join([*modifiers, key]))

    @staticmethod
    def _windows_modifiers() -> List[str]:
        user32 = ctypes.windll.user32
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetAsyncKeyState.restype = ctypes.c_short

        def is_down(*virtual_keys: int) -> bool:
            return any(user32.GetAsyncKeyState(key) & 0x8000 for key in virtual_keys)

        modifiers = []
        if is_down(0x11, 0xA2, 0xA3):
            modifiers.append("Ctrl")
        if is_down(0x12, 0xA4, 0xA5):
            modifiers.append("Alt")
        if is_down(0x10, 0xA0, 0xA1):
            modifiers.append("Shift")
        if is_down(0x5B, 0x5C):
            modifiers.append("Win")
        return modifiers

    @classmethod
    def validate_mapping(cls, values: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        used: dict[str, str] = {}
        labels = {definition.identifier: definition.label for definition in HOTKEY_ACTIONS}
        for definition in HOTKEY_ACTIONS:
            value = str(values.get(definition.identifier, "") or "").strip()
            if not value:
                normalized[definition.identifier] = ""
                continue
            chord = cls.normalize(value)
            cls._validate_safe_chord(chord, definition.global_hotkey)
            if chord in used:
                raise ValueError(
                    f"“{labels[definition.identifier]}”与“{labels[used[chord]]}”使用了相同快捷键 {chord}。"
                )
            used[chord] = definition.identifier
            normalized[definition.identifier] = chord
        return normalized

    @classmethod
    def merge_with_defaults(cls, values: object) -> dict[str, str]:
        merged = default_hotkey_map()
        if isinstance(values, dict):
            for definition in HOTKEY_ACTIONS:
                if definition.identifier in values:
                    try:
                        merged[definition.identifier] = cls.normalize(
                            str(values[definition.identifier] or "")
                        )
                    except ValueError:
                        merged[definition.identifier] = definition.default
        return merged

    @classmethod
    def _normalize_key(cls, value: str) -> str:
        stripped = value.strip()
        lowered = stripped.lower()
        if lowered in cls.KEY_ALIASES:
            return cls.KEY_ALIASES[lowered]
        if re.fullmatch(r"f(?:[1-9]|1\d|2[0-4])", lowered):
            return lowered.upper()
        if len(stripped) == 1 and stripped.isalnum():
            return stripped.upper()
        raise ValueError(f"不支持按键 {stripped or '(空)'}。")

    @classmethod
    def _virtual_key(cls, key: str) -> int:
        if len(key) == 1 and key.isalnum():
            return ord(key.upper())
        if re.fullmatch(r"F(?:[1-9]|1\d|2[0-4])", key):
            return 0x70 + int(key[1:]) - 1
        if key in cls.VIRTUAL_KEYS:
            return cls.VIRTUAL_KEYS[key]
        raise ValueError(f"无法注册按键 {key}。")

    @staticmethod
    def _validate_safe_chord(chord: str, global_hotkey: bool = True) -> None:
        tokens = chord.split("+")
        key = tokens[-1]
        modifiers = set(tokens[:-1])
        if (
            global_hotkey
            and not modifiers
            and not re.fullmatch(r"F(?:[1-9]|1\d|2[0-4])", key)
            and key != "PrintScreen"
        ):
            raise ValueError("全局单键仅支持 F1-F24 或 PrintScreen。")
        if len(key) == 1 and key.isalnum() and not modifiers.intersection(
            {"Ctrl", "Alt", "Win"}
        ):
            raise ValueError("字母或数字快捷键至少需要 Ctrl、Alt 或 Win 修饰键。")


@dataclass
class PersistedAppConfig:
    output_dir: str = "screenshots"
    quick_save_dir: str = "quick-save"
    history_dir: str = "history"
    auto_save: bool = True
    image_format: str = "PNG"
    image_quality: int = 92
    filename_pattern: str = OutputNamePolicy.DEFAULT_PATTERN
    quick_filename_pattern: str = OutputNamePolicy.DEFAULT_QUICK_PATTERN
    max_history: int = 50
    color_format: str = "hex"
    accent_color: str = AppTheme.DEFAULT_ACCENT
    smart_selection: bool = True
    capture_delay: int = 0
    capture_mask_opacity: int = 55
    capture_border_width: int = 2
    capture_show_handles: bool = True
    capture_show_crosshair: bool = False
    capture_show_magnifier: bool = True
    magnifier_zoom_index: int = CaptureMagnifier.DEFAULT_ZOOM_INDEX
    global_hotkeys: bool = True
    close_to_tray: bool = True
    start_in_tray: bool = True
    show_main_after_capture: bool = False
    launch_at_startup: bool = False
    pin_default_opacity: int = 100
    pin_always_on_top: bool = True
    pin_restore_limit: int = 3
    pin_max_size: int = 12000
    pin_thumbnail_width: int = 180
    pin_thumbnail_height: int = 120
    hotkeys: dict[str, str] = field(default_factory=default_hotkey_map)


class MainWindowVisibilityPolicy:
    """统一决定启动和截图结束后的主窗口状态。"""

    @staticmethod
    def startup_state(
        config: PersistedAppConfig,
        tray_available: bool,
    ) -> str:
        if config.start_in_tray and tray_available:
            return "withdrawn"
        return "normal"

    @staticmethod
    def after_capture_state(
        config: PersistedAppConfig,
        previous_state: Optional[str],
        tray_available: bool,
        force_show: bool = False,
    ) -> Optional[str]:
        if force_show:
            return "normal"
        if previous_state is None:
            return None
        if config.show_main_after_capture:
            return "normal"
        if tray_available:
            return "withdrawn"
        return previous_state


class ConfigStore:
    """保存用户习惯：目录、自动保存、历史数量等。"""

    CONFIG_FILE = Path("screenshot_settings.json")

    @classmethod
    def load(cls) -> PersistedAppConfig:
        if not cls.CONFIG_FILE.exists():
            return PersistedAppConfig()

        try:
            data = json.loads(cls.CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return PersistedAppConfig()

        config = PersistedAppConfig()
        for key in config.__dataclass_fields__:
            if key in data:
                if key == "hotkeys":
                    config.hotkeys = HotkeyCodec.merge_with_defaults(data[key])
                else:
                    setattr(config, key, data[key])
        config.hotkeys = HotkeyCodec.merge_with_defaults(config.hotkeys)
        try:
            config.hotkeys = HotkeyCodec.validate_mapping(config.hotkeys)
        except ValueError:
            config.hotkeys = default_hotkey_map()
        config.image_format = OutputNamePolicy.normalize_format(config.image_format)
        config.accent_color = AppTheme.normalize_accent(config.accent_color)
        config.color_format = config.color_format if config.color_format in {"hex", "rgb"} else "hex"
        config.image_quality = cls._clamp_int(config.image_quality, 1, 100, 92)
        config.max_history = cls._clamp_int(config.max_history, 1, 500, 50)
        config.capture_delay = cls._clamp_int(config.capture_delay, 0, 10, 0)
        config.capture_mask_opacity = cls._clamp_int(
            config.capture_mask_opacity, 20, 85, 55
        )
        config.capture_border_width = cls._clamp_int(
            config.capture_border_width, 1, 6, 2
        )
        config.magnifier_zoom_index = cls._clamp_int(
            config.magnifier_zoom_index,
            0,
            len(CaptureMagnifier.ZOOM_SAMPLES) - 1,
            CaptureMagnifier.DEFAULT_ZOOM_INDEX,
        )
        config.pin_default_opacity = cls._clamp_int(
            config.pin_default_opacity, 15, 100, 100
        )
        config.pin_restore_limit = cls._clamp_int(config.pin_restore_limit, 1, 20, 3)
        config.pin_max_size = cls._clamp_int(config.pin_max_size, 320, 20000, 12000)
        config.pin_thumbnail_width = cls._clamp_int(
            config.pin_thumbnail_width, 40, 600, 180
        )
        config.pin_thumbnail_height = cls._clamp_int(
            config.pin_thumbnail_height, 40, 600, 120
        )
        config.filename_pattern = str(config.filename_pattern or OutputNamePolicy.DEFAULT_PATTERN)
        config.quick_filename_pattern = str(
            config.quick_filename_pattern or OutputNamePolicy.DEFAULT_QUICK_PATTERN
        )
        return config

    @classmethod
    def save(cls, config: PersistedAppConfig) -> None:
        data = {
            key: getattr(config, key)
            for key in config.__dataclass_fields__
        }
        cls.CONFIG_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _clamp_int(value: object, minimum: int, maximum: int, fallback: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = fallback
        return max(minimum, min(maximum, number))


class WindowsStartupManager:
    """管理当前用户的 Windows 开机启动项。"""

    VALUE_NAME = "ScreenshotTool"
    REGISTRY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

    @classmethod
    def command(cls) -> str:
        if getattr(sys, "frozen", False):
            return f'"{Path(sys.executable).resolve()}"'
        return f'"{Path(sys.executable).resolve()}" "{Path(__file__).resolve()}"'

    @classmethod
    def set_enabled(cls, enabled: bool) -> None:
        if platform.system() != "Windows":
            if enabled:
                raise RuntimeError("开机启动设置目前仅支持 Windows。")
            return
        import winreg

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            cls.REGISTRY_PATH,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            if enabled:
                winreg.SetValueEx(
                    key,
                    cls.VALUE_NAME,
                    0,
                    winreg.REG_SZ,
                    cls.command(),
                )
                return
            try:
                winreg.DeleteValue(key, cls.VALUE_NAME)
            except FileNotFoundError:
                pass


@dataclass(frozen=True)
class GlobalHotkeyBinding:
    identifier: int
    modifiers: int
    virtual_key: int
    name: str
    callback: Callable[[], None]


class WindowsGlobalHotkeyManager:
    """在线程消息队列里监听 Win32 全局热键，并在 Tk 主线程执行回调。"""

    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_WIN = 0x0008
    MOD_NOREPEAT = 0x4000
    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012
    POLL_MS = 40

    def __init__(
        self,
        root: tk.Tk,
        bindings: List[GlobalHotkeyBinding],
        status_func: Callable[[str], None],
    ) -> None:
        self.root = root
        self.bindings = bindings
        self.status_func = status_func
        self._events: queue.SimpleQueue[int] = queue.SimpleQueue()
        self._ready = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._thread_id = 0
        self._poll_job: Optional[str] = None
        self._running = False
        self.active_ids: set[int] = set()
        self.failed_names: List[str] = []

    def start(self) -> None:
        if platform.system() != "Windows" or self._running:
            return
        self._running = True
        self._ready.clear()
        self.active_ids.clear()
        self.failed_names.clear()
        self._thread = threading.Thread(
            target=self._message_loop,
            name="screenshot-global-hotkeys",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=1.5)
        self._schedule_poll()

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._poll_job:
            try:
                self.root.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None
        if self._thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(
                    self._thread_id,
                    self.WM_QUIT,
                    0,
                    0,
                )
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.8)
        self._thread = None
        self._thread_id = 0

    def summary(self) -> str:
        if platform.system() != "Windows":
            return "全局热键仅在 Windows 生效"
        if not self.active_ids:
            return "全局热键未注册，可能已被其他截图软件占用"
        if self.failed_names:
            return f"全局热键已启用，{len(self.failed_names)} 个组合键被占用"
        return f"全局热键已启用（{len(self.active_ids)} 个）"

    def _message_loop(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.RegisterHotKey.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.c_int,
            ctypes.wintypes.UINT,
            ctypes.wintypes.UINT,
        ]
        user32.RegisterHotKey.restype = ctypes.wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = ctypes.wintypes.BOOL
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(ctypes.wintypes.MSG),
            ctypes.wintypes.HWND,
            ctypes.wintypes.UINT,
            ctypes.wintypes.UINT,
        ]
        user32.GetMessageW.restype = ctypes.wintypes.BOOL
        self._thread_id = int(kernel32.GetCurrentThreadId())
        try:
            for binding in self.bindings:
                registered = user32.RegisterHotKey(
                    None,
                    binding.identifier,
                    binding.modifiers | self.MOD_NOREPEAT,
                    binding.virtual_key,
                )
                if registered:
                    self.active_ids.add(binding.identifier)
                else:
                    self.failed_names.append(binding.name)
            self._ready.set()

            message = ctypes.wintypes.MSG()
            while self._running:
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    break
                if message.message == self.WM_HOTKEY:
                    self._events.put(int(message.wParam))
        finally:
            for identifier in tuple(self.active_ids):
                user32.UnregisterHotKey(None, identifier)
            self.active_ids.clear()
            self._ready.set()

    def _schedule_poll(self) -> None:
        try:
            self._poll_job = self.root.after(self.POLL_MS, self._poll)
        except tk.TclError:
            self._poll_job = None

    def _poll(self) -> None:
        callbacks = {binding.identifier: binding.callback for binding in self.bindings}
        while True:
            try:
                identifier = self._events.get_nowait()
            except queue.Empty:
                break
            callback = callbacks.get(identifier)
            if callback:
                try:
                    callback()
                except Exception as exc:
                    self.status_func(f"全局热键执行失败：{exc}")

        if self._running:
            self._schedule_poll()


class TrayIconFactory:
    """生成项目自己的托盘图标，不依赖外部品牌素材。"""

    @staticmethod
    def make(size: int = 64, accent_color: str = AppTheme.DEFAULT_ACCENT) -> Image.Image:
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        scale = size / 64
        accent = AppTheme.normalize_accent(accent_color)

        def box(values: Tuple[int, int, int, int]) -> Tuple[int, int, int, int]:
            return tuple(int(round(value * scale)) for value in values)  # type: ignore[return-value]

        draw.rounded_rectangle(
            box((5, 5, 59, 59)),
            radius=max(4, int(round(11 * scale))),
            fill=accent,
            outline=AppTheme.blend(accent, "#000000", 0.25),
            width=max(1, int(round(2 * scale))),
        )
        stroke = max(2, int(round(5 * scale)))
        white = "#ffffff"
        draw.line(
            [
                (int(18 * scale), int(29 * scale)),
                (int(18 * scale), int(18 * scale)),
                (int(29 * scale), int(18 * scale)),
            ],
            fill=white,
            width=stroke,
            joint="curve",
        )
        draw.line(
            [
                (int(35 * scale), int(18 * scale)),
                (int(46 * scale), int(18 * scale)),
                (int(46 * scale), int(29 * scale)),
            ],
            fill=white,
            width=stroke,
            joint="curve",
        )
        draw.line(
            [
                (int(18 * scale), int(35 * scale)),
                (int(18 * scale), int(46 * scale)),
                (int(29 * scale), int(46 * scale)),
            ],
            fill=white,
            width=stroke,
            joint="curve",
        )
        draw.line(
            [
                (int(35 * scale), int(46 * scale)),
                (int(46 * scale), int(46 * scale)),
                (int(46 * scale), int(35 * scale)),
            ],
            fill=white,
            width=stroke,
            joint="curve",
        )
        return image


class SystemTrayController:
    """使用 Windows 原生 API 创建托盘图标，并转发菜单动作到 Tk 主线程。"""

    POLL_MS = 50
    WM_TRAYICON = 0x8001
    WM_COMMAND = 0x0111
    WM_CLOSE = 0x0010
    WM_DESTROY = 0x0002
    WM_LBUTTONUP = 0x0202
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    WM_CONTEXTMENU = 0x007B

    MENU_ITEMS = (
        (1001, "capture", "框选截图"),
        (1002, "paste", "从剪贴板贴图"),
        (1003, "toggle_pins", "显示/隐藏全部贴图"),
        None,
        (1004, "history", "截图历史"),
        (1005, "preferences", "首选项"),
        (1006, "show", "显示主窗口"),
        None,
        (1007, "quit", "退出"),
    )

    def __init__(
        self,
        root: tk.Tk,
        actions: dict[str, Callable[[], None]],
        status_func: Callable[[str], None],
        accent_color: str = AppTheme.DEFAULT_ACCENT,
    ) -> None:
        self.root = root
        self.actions = actions
        self.status_func = status_func
        self.accent_color = AppTheme.normalize_accent(accent_color)
        self._events: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._ready = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._poll_job: Optional[str] = None
        self._hwnd = 0
        self._wndproc = None
        self._error: Optional[str] = None
        self.running = False

    def start(self) -> bool:
        if platform.system() != "Windows" or self.running:
            return False
        self._ready.clear()
        self._error = None
        self.running = True
        self._thread = threading.Thread(
            target=self._message_loop,
            name="screenshot-system-tray",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=2.0)
        if not self._hwnd:
            self.running = False
            return False
        self._schedule_poll()
        return True

    def stop(self) -> None:
        was_running = self.running
        self.running = False
        if self._poll_job:
            try:
                self.root.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None
        if self._hwnd:
            try:
                ctypes.windll.user32.PostMessageW(
                    ctypes.wintypes.HWND(self._hwnd),
                    self.WM_CLOSE,
                    0,
                    0,
                )
            except Exception:
                pass
        if was_running and self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None
        self._hwnd = 0

    def _message_loop(self) -> None:
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32
        wndproc_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t,
            ctypes.wintypes.HWND,
            ctypes.wintypes.UINT,
            ctypes.wintypes.WPARAM,
            ctypes.wintypes.LPARAM,
        )

        class Guid(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.wintypes.DWORD),
                ("Data2", ctypes.wintypes.WORD),
                ("Data3", ctypes.wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        class NotifyIconData(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.wintypes.DWORD),
                ("hWnd", ctypes.wintypes.HWND),
                ("uID", ctypes.wintypes.UINT),
                ("uFlags", ctypes.wintypes.UINT),
                ("uCallbackMessage", ctypes.wintypes.UINT),
                ("hIcon", ctypes.wintypes.HICON),
                ("szTip", ctypes.c_wchar * 128),
                ("dwState", ctypes.wintypes.DWORD),
                ("dwStateMask", ctypes.wintypes.DWORD),
                ("szInfo", ctypes.c_wchar * 256),
                ("uTimeoutOrVersion", ctypes.wintypes.UINT),
                ("szInfoTitle", ctypes.c_wchar * 64),
                ("dwInfoFlags", ctypes.wintypes.DWORD),
                ("guidItem", Guid),
                ("hBalloonIcon", ctypes.wintypes.HICON),
            ]

        class WindowClass(ctypes.Structure):
            _fields_ = [
                ("style", ctypes.wintypes.UINT),
                ("lpfnWndProc", wndproc_type),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", ctypes.wintypes.HINSTANCE),
                ("hIcon", ctypes.wintypes.HICON),
                ("hCursor", ctypes.wintypes.HANDLE),
                ("hbrBackground", ctypes.wintypes.HBRUSH),
                ("lpszMenuName", ctypes.wintypes.LPCWSTR),
                ("lpszClassName", ctypes.wintypes.LPCWSTR),
            ]

        user32.RegisterClassW.argtypes = [ctypes.POINTER(WindowClass)]
        user32.RegisterClassW.restype = ctypes.wintypes.ATOM
        user32.UnregisterClassW.argtypes = [
            ctypes.wintypes.LPCWSTR,
            ctypes.wintypes.HINSTANCE,
        ]
        user32.UnregisterClassW.restype = ctypes.wintypes.BOOL
        user32.CreateWindowExW.argtypes = [
            ctypes.wintypes.DWORD,
            ctypes.wintypes.LPCWSTR,
            ctypes.wintypes.LPCWSTR,
            ctypes.wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.wintypes.HWND,
            ctypes.wintypes.HMENU,
            ctypes.wintypes.HINSTANCE,
            ctypes.c_void_p,
        ]
        user32.CreateWindowExW.restype = ctypes.wintypes.HWND
        user32.DefWindowProcW.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.wintypes.UINT,
            ctypes.wintypes.WPARAM,
            ctypes.wintypes.LPARAM,
        ]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DestroyWindow.argtypes = [ctypes.wintypes.HWND]
        user32.DestroyWindow.restype = ctypes.wintypes.BOOL
        user32.DestroyIcon.argtypes = [ctypes.wintypes.HICON]
        user32.DestroyIcon.restype = ctypes.wintypes.BOOL
        kernel32.GetModuleHandleW.restype = ctypes.wintypes.HINSTANCE
        shell32.Shell_NotifyIconW.argtypes = [
            ctypes.wintypes.DWORD,
            ctypes.POINTER(NotifyIconData),
        ]
        shell32.Shell_NotifyIconW.restype = ctypes.wintypes.BOOL

        hinstance = kernel32.GetModuleHandleW(None)
        class_name = f"ScreenshotToolTray_{id(self):x}"

        @wndproc_type
        def window_proc(hwnd: int, message: int, wparam: int, lparam: int) -> int:
            if message == self.WM_TRAYICON:
                mouse_message = int(lparam) & 0xFFFF
                if mouse_message in {self.WM_LBUTTONUP, self.WM_LBUTTONDBLCLK}:
                    self._enqueue_action("capture")
                elif mouse_message in {self.WM_RBUTTONUP, self.WM_CONTEXTMENU}:
                    try:
                        self._show_context_menu(int(hwnd))
                    except Exception as exc:
                        self._enqueue_status(f"托盘菜单打开失败：{exc}")
                return 0
            if message == self.WM_COMMAND:
                self._enqueue_menu_id(int(wparam) & 0xFFFF)
                return 0
            if message == self.WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if message == self.WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return int(user32.DefWindowProcW(hwnd, message, wparam, lparam))

        self._wndproc = window_proc
        window_class = WindowClass()
        window_class.lpfnWndProc = window_proc
        window_class.hInstance = hinstance
        window_class.lpszClassName = class_name
        atom = user32.RegisterClassW(ctypes.byref(window_class))
        if not atom:
            self._error = "注册托盘窗口失败"
            self._ready.set()
            self.running = False
            return

        hwnd = user32.CreateWindowExW(
            0,
            class_name,
            "ScreenshotToolTray",
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            hinstance,
            None,
        )
        if not hwnd:
            self._error = "创建托盘窗口失败"
            user32.UnregisterClassW(class_name, hinstance)
            self._ready.set()
            self.running = False
            return

        self._hwnd = int(hwnd)
        hicon = self._create_hicon(TrayIconFactory.make(32, self.accent_color))
        notify = NotifyIconData()
        notify.cbSize = ctypes.sizeof(NotifyIconData)
        notify.hWnd = hwnd
        notify.uID = 1
        notify.uFlags = 0x0001 | 0x0002 | 0x0004
        notify.uCallbackMessage = self.WM_TRAYICON
        notify.hIcon = hicon
        notify.szTip = "轻量截图工具"
        added = bool(shell32.Shell_NotifyIconW(0x00000000, ctypes.byref(notify)))
        if not added:
            self._error = "添加系统托盘图标失败"
            user32.DestroyWindow(hwnd)
            self._hwnd = 0
            self._ready.set()
            self.running = False
            if hicon:
                user32.DestroyIcon(hicon)
            user32.UnregisterClassW(class_name, hinstance)
            return

        self._ready.set()
        message = ctypes.wintypes.MSG()
        try:
            while self.running and user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        finally:
            shell32.Shell_NotifyIconW(0x00000002, ctypes.byref(notify))
            if hicon:
                user32.DestroyIcon(hicon)
            user32.UnregisterClassW(class_name, hinstance)
            self._hwnd = 0
            self.running = False

    @staticmethod
    def _configure_menu_api(user32: object) -> None:
        user32.CreatePopupMenu.argtypes = []
        user32.CreatePopupMenu.restype = ctypes.wintypes.HMENU
        user32.AppendMenuW.argtypes = [
            ctypes.wintypes.HMENU,
            ctypes.wintypes.UINT,
            ctypes.c_size_t,
            ctypes.wintypes.LPCWSTR,
        ]
        user32.AppendMenuW.restype = ctypes.wintypes.BOOL
        user32.SetMenuDefaultItem.argtypes = [
            ctypes.wintypes.HMENU,
            ctypes.wintypes.UINT,
            ctypes.wintypes.UINT,
        ]
        user32.SetMenuDefaultItem.restype = ctypes.wintypes.BOOL
        user32.GetCursorPos.argtypes = [ctypes.POINTER(ctypes.wintypes.POINT)]
        user32.GetCursorPos.restype = ctypes.wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [ctypes.wintypes.HWND]
        user32.SetForegroundWindow.restype = ctypes.wintypes.BOOL
        user32.TrackPopupMenu.argtypes = [
            ctypes.wintypes.HMENU,
            ctypes.wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.wintypes.HWND,
            ctypes.c_void_p,
        ]
        user32.TrackPopupMenu.restype = ctypes.wintypes.UINT
        user32.DestroyMenu.argtypes = [ctypes.wintypes.HMENU]
        user32.DestroyMenu.restype = ctypes.wintypes.BOOL

    def _show_context_menu(self, hwnd: int) -> None:
        user32 = ctypes.windll.user32
        self._configure_menu_api(user32)
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        try:
            for entry in self.MENU_ITEMS:
                if entry is None:
                    user32.AppendMenuW(menu, 0x0800, 0, None)
                else:
                    identifier, _, label = entry
                    user32.AppendMenuW(menu, 0x0000, identifier, label)

            user32.SetMenuDefaultItem(menu, 1001, False)
            cursor = ctypes.wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(cursor))
            window = ctypes.wintypes.HWND(hwnd)
            user32.SetForegroundWindow(window)
            command = user32.TrackPopupMenu(
                menu,
                0x0100 | 0x0002,
                cursor.x,
                cursor.y,
                0,
                window,
                None,
            )
            if command:
                self._enqueue_menu_id(int(command))
        finally:
            user32.DestroyMenu(menu)

    def _enqueue_menu_id(self, identifier: int) -> None:
        for entry in self.MENU_ITEMS:
            if entry and entry[0] == identifier:
                self._enqueue_action(entry[1])
                return

    def _enqueue_action(self, name: str) -> None:
        callback = self.actions.get(name)
        if callback:
            self._events.put(callback)

    def _enqueue_status(self, message: str) -> None:
        self._events.put(lambda value=message: self.status_func(value))

    @staticmethod
    def _create_hicon(image: Image.Image) -> int:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        class IconInfo(ctypes.Structure):
            _fields_ = [
                ("fIcon", ctypes.wintypes.BOOL),
                ("xHotspot", ctypes.wintypes.DWORD),
                ("yHotspot", ctypes.wintypes.DWORD),
                ("hbmMask", ctypes.wintypes.HBITMAP),
                ("hbmColor", ctypes.wintypes.HBITMAP),
            ]

        gdi32.CreateBitmap.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.wintypes.UINT,
            ctypes.wintypes.UINT,
            ctypes.c_void_p,
        ]
        gdi32.CreateBitmap.restype = ctypes.wintypes.HBITMAP
        gdi32.DeleteObject.argtypes = [ctypes.wintypes.HANDLE]
        gdi32.DeleteObject.restype = ctypes.wintypes.BOOL
        user32.CreateIconIndirect.argtypes = [ctypes.POINTER(IconInfo)]
        user32.CreateIconIndirect.restype = ctypes.wintypes.HICON

        transpose = getattr(getattr(Image, "Transpose", Image), "FLIP_TOP_BOTTOM")
        rgba = image.convert("RGBA").transpose(transpose)
        width, height = rgba.size
        color_data = ctypes.create_string_buffer(rgba.tobytes("raw", "BGRA"))
        mask_stride = ((width + 31) // 32) * 4
        mask_data = ctypes.create_string_buffer(bytes(mask_stride * height))
        color_bitmap = gdi32.CreateBitmap(width, height, 1, 32, color_data)
        mask_bitmap = gdi32.CreateBitmap(width, height, 1, 1, mask_data)
        if not color_bitmap or not mask_bitmap:
            if color_bitmap:
                gdi32.DeleteObject(color_bitmap)
            if mask_bitmap:
                gdi32.DeleteObject(mask_bitmap)
            return 0

        info = IconInfo(True, 0, 0, mask_bitmap, color_bitmap)
        hicon = user32.CreateIconIndirect(ctypes.byref(info))
        gdi32.DeleteObject(color_bitmap)
        gdi32.DeleteObject(mask_bitmap)
        return int(hicon or 0)

    def _schedule_poll(self) -> None:
        try:
            self._poll_job = self.root.after(self.POLL_MS, self._poll)
        except tk.TclError:
            self._poll_job = None

    def _poll(self) -> None:
        while True:
            try:
                callback = self._events.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception as exc:
                self.status_func(f"托盘操作失败：{exc}")
        if self.running:
            self._schedule_poll()


class PinContentFactory:
    """把剪贴板文本、颜色值等内容转成可贴到屏幕上的图片。"""

    COLOR_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")
    RGB_RE = re.compile(
        r"^(?:rgb\()?\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*\)?$",
        re.IGNORECASE,
    )

    @classmethod
    def from_text(cls, text: str) -> Image.Image:
        color = cls.parse_color(text)
        if color:
            return cls.color_card(color)
        return cls.text_card(text)

    @classmethod
    def parse_color(cls, text: str) -> Optional[Tuple[int, int, int]]:
        value = text.strip()
        if cls.COLOR_RE.match(value):
            value = value.lstrip("#")
            return (
                int(value[0:2], 16),
                int(value[2:4], 16),
                int(value[4:6], 16),
            )

        rgb_match = cls.RGB_RE.match(value)
        if rgb_match:
            red, green, blue = (int(part) for part in rgb_match.groups())
            if all(0 <= channel <= 255 for channel in (red, green, blue)):
                return red, green, blue

        return None

    @classmethod
    def color_card(cls, color: Tuple[int, int, int]) -> Image.Image:
        red, green, blue = color
        hex_value = f"#{red:02X}{green:02X}{blue:02X}"
        image = Image.new("RGB", (320, 180), "#f8fafc")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((18, 18, 152, 162), radius=10, fill=color)
        draw.rectangle((176, 36, 300, 37), fill="#d8dee8")

        font_title = AnnotationRenderer._load_font(26)
        font_body = AnnotationRenderer._load_font(17)
        draw.text((176, 52), hex_value, fill="#111827", font=font_title)
        draw.text((176, 94), f"RGB {red}, {green}, {blue}", fill="#374151", font=font_body)
        draw.text((176, 124), "颜色贴图", fill="#64748b", font=font_body)
        return image

    @classmethod
    def text_card(cls, text: str) -> Image.Image:
        value = text.strip() or "(空文本)"
        wrapped = "\n".join(textwrap.wrap(value, width=28))[:1800]
        font = AnnotationRenderer._load_font(18)
        title_font = AnnotationRenderer._load_font(14)

        probe = Image.new("RGB", (10, 10), "white")
        draw = ImageDraw.Draw(probe)
        bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=6)
        width = max(280, min(700, bbox[2] - bbox[0] + 48))
        height = max(160, min(900, bbox[3] - bbox[1] + 78))

        image = Image.new("RGB", (width, height), "#fffef8")
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, width, height), outline="#d5d1bf", width=2)
        draw.text((24, 16), "文本贴图", fill="#64748b", font=title_font)
        draw.multiline_text((24, 46), wrapped, fill="#111827", font=font, spacing=6)
        return image


class ScreenshotHistoryManager:
    """保存成功截图，支持回放和历史窗口。"""

    META_FILE = "metadata.json"

    def __init__(self, history_dir: Path, max_items: int) -> None:
        self.history_dir = history_dir
        self.max_items = max_items
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.records = self._load_records()
        self.cursor = len(self.records)

    def add(self, image: Image.Image, kind: str = "capture", region: Optional[Box] = None) -> Path:
        self.history_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"{timestamp}.png"
        path = self.history_dir / filename
        image.convert("RGB").save(path, "PNG")

        self.records.append(
            {
                "time": timestamp,
                "file": filename,
                "kind": kind,
                "width": image.width,
                "height": image.height,
                "region": list(region) if region else None,
            }
        )
        self._trim()
        self._save_records()
        self.cursor = len(self.records)
        return path

    def all_records(self) -> List[dict]:
        return list(self.records)

    def latest_image(self) -> Optional[Image.Image]:
        if not self.records:
            return None
        return self.load_image(self.records[-1])

    def previous_image(self) -> Optional[Image.Image]:
        if not self.records:
            return None
        self.cursor = max(0, min(self.cursor - 1, len(self.records) - 1))
        return self.load_image(self.records[self.cursor])

    def next_image(self) -> Optional[Image.Image]:
        if not self.records:
            return None
        self.cursor = min(len(self.records) - 1, self.cursor + 1)
        return self.load_image(self.records[self.cursor])

    def load_image(self, record: dict) -> Optional[Image.Image]:
        path = self.history_dir / str(record.get("file", ""))
        if not path.exists():
            return None
        return Image.open(path).convert("RGB")

    def clear(self) -> None:
        for record in self.records:
            path = self.history_dir / str(record.get("file", ""))
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
        self.records = []
        self.cursor = 0
        self._save_records()

    def _load_records(self) -> List[dict]:
        meta_path = self.history_dir / self.META_FILE
        if not meta_path.exists():
            return []
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [record for record in data if isinstance(record, dict)]

    def _save_records(self) -> None:
        meta_path = self.history_dir / self.META_FILE
        meta_path.write_text(
            json.dumps(self.records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _trim(self) -> None:
        while len(self.records) > self.max_items:
            record = self.records.pop(0)
            path = self.history_dir / str(record.get("file", ""))
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass


class ActiveWindowCapture:
    """Windows 活动窗口截图。"""

    @classmethod
    def capture(cls) -> Image.Image:
        image, _ = cls.capture_with_position()
        return image

    @staticmethod
    def capture_with_position() -> Tuple[Image.Image, Point]:
        if platform.system() != "Windows":
            raise RuntimeError("活动窗口截图目前仅支持 Windows。")

        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
        user32.GetWindowRect.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.POINTER(ctypes.wintypes.RECT),
        ]
        user32.GetWindowRect.restype = ctypes.wintypes.BOOL
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            raise RuntimeError("无法获取活动窗口。")

        rect = ctypes.wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise RuntimeError("无法读取活动窗口位置。")

        box = (rect.left, rect.top, rect.right, rect.bottom)
        left, top, right, bottom = ScreenshotManager._normalize_box(box)
        if right - left < 2 or bottom - top < 2:
            raise RuntimeError("活动窗口尺寸无效。")
        return ScreenshotManager.grab_box((left, top, right, bottom)), (left, top)


class WindowSnapper:
    """计算贴图移动时的边缘吸附位置。"""

    @staticmethod
    def snap(
        position: Point,
        size: Tuple[int, int],
        targets: List[Box],
        tolerance: int = 10,
    ) -> Point:
        x, y = position
        width, height = size
        best_dx: Optional[int] = None
        best_dy: Optional[int] = None

        for left, top, right, bottom in targets:
            for delta in (left - x, right - x, left - (x + width), right - (x + width)):
                if abs(delta) <= tolerance and (best_dx is None or abs(delta) < abs(best_dx)):
                    best_dx = delta
            for delta in (top - y, bottom - y, top - (y + height), bottom - (y + height)):
                if abs(delta) <= tolerance and (best_dy is None or abs(delta) < abs(best_dy)):
                    best_dy = delta

        return x + (best_dx or 0), y + (best_dy or 0)


class PinnedImageWindow:
    """置顶贴图窗口，支持移动、缩放、透明度、旋转、镜像、灰度、反色。"""

    def __init__(
        self,
        root: tk.Tk,
        image: Image.Image,
        on_close: Callable[["PinnedImageWindow", bool], None],
        copy_func: Callable[[Image.Image], None],
        save_func: Callable[[Image.Image], Path],
        ocr_func: Optional[Callable[[Image.Image], None]] = None,
        title: str = "贴图",
        position: Optional[Point] = None,
        snap_targets_func: Optional[Callable[["PinnedImageWindow"], List[Box]]] = None,
        initial_opacity: float = 1.0,
        initial_topmost: bool = True,
        max_window_size: int = 12000,
        thumbnail_size: Tuple[int, int] = (180, 120),
    ) -> None:
        self.root = root
        self.original_image = image.convert("RGBA")
        self.image = self.original_image.copy()
        self.on_close = on_close
        self.copy_func = copy_func
        self.save_func = save_func
        self.ocr_func = ocr_func
        self.title = title
        self.position = position
        self.snap_targets_func = snap_targets_func
        self.max_window_size = max(320, min(20000, int(max_window_size)))
        self.thumbnail_size = (
            max(40, min(600, int(thumbnail_size[0]))),
            max(40, min(600, int(thumbnail_size[1]))),
        )
        self.default_scale = min(
            1.0,
            self.max_window_size
            / max(1, self.image.width, self.image.height),
        )
        self.scale = self.default_scale
        self.default_opacity = max(0.15, min(1.0, float(initial_opacity)))
        self.opacity = self.default_opacity
        self.is_topmost = bool(initial_topmost)
        self.grayscale = False
        self.inverted = False
        self.photo: Optional[ImageTk.PhotoImage] = None
        self.drag_start = (0, 0)
        self.window_start = (0, 0)
        self.resize_edges = ""
        self.resize_start_scale = self.default_scale
        self.resize_start_size: Tuple[int, int] = (1, 1)
        self.thumbnail_mode = False
        self.pre_thumbnail_scale = self.default_scale
        self.close_pending = False
        self.closed = False

        self.window = tk.Toplevel(root)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", self.is_topmost)
        self.window.attributes("-alpha", self.opacity)
        self.window.configure(bg="#111827")
        self.color_copy_key_monitor = WindowsPhysicalKeyMonitor(
            self.window,
            ord("C"),
            self._copy_pointer_color,
        )

        self.label = tk.Label(
            self.window,
            bd=0,
            relief=tk.FLAT,
            highlightthickness=0,
            bg="#111827",
            takefocus=True,
        )
        self.label.pack(fill=tk.BOTH, expand=True)

        self.menu = tk.Menu(self.window, tearoff=False)
        self.menu.add_command(label="复制图像", command=self.copy)
        self.menu.add_command(label="保存图像", command=self.save)
        if self.ocr_func:
            self.menu.add_command(label="提取文字", command=self.extract_text)
        self.menu.add_separator()
        self.menu.add_command(label="顺时针旋转 90 度", command=lambda: self.rotate(-90))
        self.menu.add_command(label="逆时针旋转 90 度", command=lambda: self.rotate(90))
        self.menu.add_command(label="水平翻转", command=self.flip_horizontal)
        self.menu.add_command(label="垂直翻转", command=self.flip_vertical)
        self.menu.add_command(label="灰度切换", command=self.toggle_grayscale)
        self.menu.add_command(label="反色切换", command=self.toggle_invert)
        self.menu.add_separator()
        self.topmost_menu_var = tk.BooleanVar(
            master=self.window,
            value=self.is_topmost,
        )
        self.menu.add_checkbutton(
            label="保持置顶",
            variable=self.topmost_menu_var,
            command=self._apply_topmost_menu_state,
        )
        self.menu.add_command(label="重置大小/透明度", command=self.reset_view)
        self.menu.add_command(label="缩略图模式", command=self.toggle_thumbnail)
        self.menu.add_separator()
        self.menu.add_command(
            label="关闭，可恢复",
            command=lambda: self._close_from_menu(destroy=False),
        )
        self.menu.add_command(
            label="销毁",
            command=lambda: self._close_from_menu(destroy=True),
        )

        self._bind_events()
        self._render()
        self._place_window()

    def _bind_events(self) -> None:
        for widget in (self.window, self.label):
            widget.bind("<ButtonPress-1>", self._start_move)
            widget.bind("<B1-Motion>", self._move)
            widget.bind("<ButtonRelease-1>", self._stop_pointer_action)
            widget.bind("<Motion>", self._update_resize_cursor)
            widget.bind("<Double-Button-1>", lambda event: self.close(destroy=False))
            widget.bind("<Shift-Double-Button-1>", lambda event: self.toggle_thumbnail())
            widget.bind("<Button-2>", lambda event: self.reset_view())
            widget.bind("<Button-3>", self._show_menu)
            widget.bind("<MouseWheel>", self._on_wheel)
            widget.bind("<Escape>", lambda event: self.close(destroy=False))
            widget.bind("<Shift-Escape>", lambda event: self.close(destroy=True))
            widget.bind("<Control-w>", lambda event: self.close(destroy=False))
            widget.bind("<Control-c>", lambda event: self.copy())
            widget.bind("<Control-s>", lambda event: self.save())
            widget.bind("1", lambda event: self.rotate(-90))
            widget.bind("2", lambda event: self.rotate(90))
            widget.bind("3", lambda event: self.flip_horizontal())
            widget.bind("4", lambda event: self.flip_vertical())
            widget.bind("5", lambda event: self.toggle_grayscale())
            widget.bind("6", lambda event: self.toggle_invert())
            widget.bind("+", lambda event: self.zoom(1.1, self._window_center()))
            widget.bind("-", lambda event: self.zoom(0.9, self._window_center()))
            widget.bind("<Control-plus>", lambda event: self.adjust_opacity(0.05))
            widget.bind("<Control-equal>", lambda event: self.adjust_opacity(0.05))
            widget.bind("<Control-minus>", lambda event: self.adjust_opacity(-0.05))
        if not self.color_copy_key_monitor.start():
            for widget in (self.window, self.label):
                widget.bind("<KeyPress-c>", self._copy_pointer_color)
                widget.bind("<KeyPress-C>", self._copy_pointer_color)

    def _render(self) -> None:
        self.scale = self._clamp_scale(self.scale)
        image = self.image.copy()
        if self.grayscale:
            image = ImageOps.grayscale(image).convert("RGBA")
        if self.inverted:
            alpha = image.getchannel("A")
            rgb = ImageOps.invert(image.convert("RGB")).convert("RGBA")
            rgb.putalpha(alpha)
            image = rgb

        width = max(1, int(image.width * self.scale))
        height = max(1, int(image.height * self.scale))
        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        image = image.resize((width, height), resampling)
        self.photo = ImageTk.PhotoImage(image)
        self.label.configure(image=self.photo)
        self.window.geometry(f"{width}x{height}")

    def _place_window(self) -> None:
        if self.position is not None:
            x, y = self.position
        else:
            x = self.root.winfo_pointerx() + 24
            y = self.root.winfo_pointery() + 24
        self.window.geometry(self._position_geometry(x, y))
        self.window.focus_force()
        self.label.focus_set()

    def _start_move(self, event: tk.Event) -> None:
        self.drag_start = (event.x_root, event.y_root)
        self.window_start = (self.window.winfo_x(), self.window.winfo_y())
        self.resize_edges = self._edge_at(event.x, event.y)
        self.resize_start_scale = self.scale
        self.resize_start_size = (
            max(1, self.window.winfo_width()),
            max(1, self.window.winfo_height()),
        )
        self.window.focus_force()

    def _move(self, event: tk.Event) -> None:
        dx = event.x_root - self.drag_start[0]
        dy = event.y_root - self.drag_start[1]
        if self.resize_edges:
            self._resize_from_drag(dx, dy)
            return

        position = (self.window_start[0] + dx, self.window_start[1] + dy)
        if event.state & 0x0001 and self.snap_targets_func:
            position = WindowSnapper.snap(
                position,
                (self.window.winfo_width(), self.window.winfo_height()),
                self.snap_targets_func(self),
            )
        self.window.geometry(
            self._position_geometry(*position)
        )

    def _stop_pointer_action(self, event: Optional[tk.Event] = None) -> None:
        self.resize_edges = ""

    def _resize_from_drag(self, dx: int, dy: int) -> None:
        start_width, start_height = self.resize_start_size
        factors: List[float] = []
        if "e" in self.resize_edges:
            factors.append((start_width + dx) / start_width)
        elif "w" in self.resize_edges:
            factors.append((start_width - dx) / start_width)
        if "s" in self.resize_edges:
            factors.append((start_height + dy) / start_height)
        elif "n" in self.resize_edges:
            factors.append((start_height - dy) / start_height)
        if not factors:
            return

        factor = max(factors, key=lambda value: abs(value - 1.0))
        self.scale = self._clamp_scale(self.resize_start_scale * factor)
        self.thumbnail_mode = False
        self._render()
        new_width = max(1, int(self.image.width * self.scale))
        new_height = max(1, int(self.image.height * self.scale))
        x = self.window_start[0]
        y = self.window_start[1]
        if "w" in self.resize_edges:
            x += start_width - new_width
        if "n" in self.resize_edges:
            y += start_height - new_height
        self.window.geometry(self._position_geometry(x, y))

    def _update_resize_cursor(self, event: tk.Event) -> None:
        edges = self._edge_at(event.x, event.y)
        cursor = {
            "n": "sb_v_double_arrow",
            "s": "sb_v_double_arrow",
            "e": "sb_h_double_arrow",
            "w": "sb_h_double_arrow",
            "ne": "sizing",
            "nw": "sizing",
            "se": "sizing",
            "sw": "sizing",
        }.get(edges, "fleur")
        self.label.configure(cursor=cursor)

    def _edge_at(self, x: int, y: int) -> str:
        margin = 5
        width = max(1, self.window.winfo_width())
        height = max(1, self.window.winfo_height())
        horizontal = "w" if x <= margin else "e" if x >= width - margin else ""
        vertical = "n" if y <= margin else "s" if y >= height - margin else ""
        return f"{vertical}{horizontal}"

    def _show_menu(self, event: tk.Event) -> None:
        self.window.focus_force()
        self.topmost_menu_var.set(self.is_topmost)
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                self.menu.grab_release()
            except tk.TclError:
                pass

    def _on_wheel(self, event: tk.Event) -> None:
        if event.state & 0x0004:
            self.adjust_opacity(0.05 if event.delta > 0 else -0.05)
        else:
            self.zoom(
                1.08 if event.delta > 0 else 0.92,
                (event.x_root, event.y_root),
            )

    def zoom(self, factor: float, anchor: Optional[Point] = None) -> None:
        old_width = max(1, int(self.image.width * self.scale))
        old_height = max(1, int(self.image.height * self.scale))
        old_x = self.window.winfo_x()
        old_y = self.window.winfo_y()
        anchor = anchor or self._window_center()
        relative_x = (anchor[0] - old_x) / old_width
        relative_y = (anchor[1] - old_y) / old_height

        next_scale = self._clamp_scale(self.scale * factor)
        if math.isclose(next_scale, self.scale, rel_tol=0.0, abs_tol=0.0001):
            return
        self.scale = next_scale
        self.thumbnail_mode = False
        self._render()
        new_width = max(1, int(self.image.width * self.scale))
        new_height = max(1, int(self.image.height * self.scale))
        next_x = int(round(anchor[0] - relative_x * new_width))
        next_y = int(round(anchor[1] - relative_y * new_height))
        self.window.geometry(self._position_geometry(next_x, next_y))

    def rotate(self, angle: int) -> None:
        self.image = self.image.rotate(angle, expand=True)
        self._render()

    def flip_horizontal(self) -> None:
        self.image = ImageOps.mirror(self.image)
        self._render()

    def flip_vertical(self) -> None:
        self.image = ImageOps.flip(self.image)
        self._render()

    def toggle_grayscale(self) -> None:
        self.grayscale = not self.grayscale
        self._render()

    def toggle_invert(self) -> None:
        self.inverted = not self.inverted
        self._render()

    def toggle_topmost(self) -> None:
        self._set_topmost(not self.is_topmost)

    def _apply_topmost_menu_state(self) -> None:
        self._set_topmost(bool(self.topmost_menu_var.get()))

    def _set_topmost(self, enabled: bool) -> None:
        previous = self.is_topmost
        try:
            self.window.attributes("-topmost", bool(enabled))
        except tk.TclError as exc:
            self.is_topmost = previous
            self.topmost_menu_var.set(previous)
            messagebox.showerror("贴图置顶失败", str(exc), parent=self.window)
            return
        self.is_topmost = bool(enabled)
        self.topmost_menu_var.set(self.is_topmost)

    def reset_view(self) -> None:
        self.scale = self.default_scale
        self.opacity = self.default_opacity
        self.thumbnail_mode = False
        self.window.attributes("-alpha", self.opacity)
        self._render()

    def adjust_opacity(self, delta: float) -> None:
        self.opacity = max(0.15, min(1.0, self.opacity + delta))
        self.window.attributes("-alpha", self.opacity)

    def toggle_thumbnail(self) -> None:
        center = self._window_center()
        if self.thumbnail_mode:
            self.scale = self.pre_thumbnail_scale
            self.thumbnail_mode = False
        else:
            self.pre_thumbnail_scale = self.scale
            self.scale = self._clamp_scale(
                min(
                    1.0,
                    self.thumbnail_size[0] / max(1, self.image.width),
                    self.thumbnail_size[1] / max(1, self.image.height),
                )
            )
            self.thumbnail_mode = True
        self._render()
        self.window.update_idletasks()
        self.window.geometry(
            self._position_geometry(
                center[0] - self.window.winfo_width() // 2,
                center[1] - self.window.winfo_height() // 2,
            )
        )

    def _copy_pointer_color(self, event: Optional[tk.Event] = None) -> str:
        image = self.current_image()
        local_x = self.window.winfo_pointerx() - self.window.winfo_x()
        local_y = self.window.winfo_pointery() - self.window.winfo_y()
        x = max(0, min(image.width - 1, int(local_x / max(self.scale, 0.001))))
        y = max(0, min(image.height - 1, int(local_y / max(self.scale, 0.001))))
        red, green, blue = image.getpixel((x, y))[:3]
        value = f"#{red:02X}{green:02X}{blue:02X}"
        self.window.clipboard_clear()
        self.window.clipboard_append(value)
        self.window.update_idletasks()
        return "break"

    def _window_center(self) -> Point:
        return (
            self.window.winfo_x() + max(1, self.window.winfo_width()) // 2,
            self.window.winfo_y() + max(1, self.window.winfo_height()) // 2,
        )

    def _clamp_scale(self, value: float) -> float:
        maximum = min(
            8.0,
            self.max_window_size / max(1, self.image.width, self.image.height),
        )
        minimum = min(0.12, maximum)
        return max(minimum, min(maximum, float(value)))

    @staticmethod
    def _position_geometry(x: int, y: int) -> str:
        return f"+{int(x)}+{int(y)}"

    def copy(self) -> None:
        self.copy_func(self.current_image())

    def save(self) -> None:
        self.save_func(self.current_image())

    def extract_text(self) -> None:
        if self.ocr_func:
            image = self.current_image()
            self._dismiss_context_menu()
            self.root.after_idle(lambda: self.ocr_func(image))

    def current_image(self) -> Image.Image:
        image = self.image.copy()
        if self.grayscale:
            image = ImageOps.grayscale(image).convert("RGBA")
        if self.inverted:
            alpha = image.getchannel("A")
            rgb = ImageOps.invert(image.convert("RGB")).convert("RGBA")
            rgb.putalpha(alpha)
            image = rgb
        return image.convert("RGB")

    def show(self) -> None:
        self.window.deiconify()
        self.window.lift()

    def hide(self) -> None:
        self.window.withdraw()

    def close(self, destroy: bool) -> None:
        if self.closed:
            return
        self.closed = True
        self.close_pending = False
        self._dismiss_context_menu()
        self.color_copy_key_monitor.stop()
        self._hide_window_now()
        try:
            self.window.destroy()
        except tk.TclError:
            pass
        self.on_close(self, destroy)

    def _close_from_menu(self, destroy: bool) -> None:
        if self.closed or self.close_pending:
            return
        self.close_pending = True
        self._dismiss_context_menu()
        self._hide_window_now()
        try:
            self.root.after_idle(lambda: self.close(destroy))
        except tk.TclError:
            self.close(destroy)

    def _dismiss_context_menu(self) -> None:
        if platform.system() == "Windows":
            try:
                user32 = ctypes.windll.user32
                user32.EndMenu.argtypes = []
                user32.EndMenu.restype = ctypes.wintypes.BOOL
                user32.EndMenu()
            except (AttributeError, OSError, ctypes.ArgumentError):
                pass
        try:
            self.menu.unpost()
            self.menu.grab_release()
        except tk.TclError:
            pass

    def _hide_window_now(self) -> None:
        top_level = None
        if platform.system() == "Windows":
            try:
                user32 = ctypes.windll.user32
                user32.GetAncestor.argtypes = [
                    ctypes.wintypes.HWND,
                    ctypes.wintypes.UINT,
                ]
                user32.GetAncestor.restype = ctypes.wintypes.HWND
                user32.ShowWindow.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
                user32.ShowWindow.restype = ctypes.wintypes.BOOL
                child = ctypes.wintypes.HWND(self.window.winfo_id())
                top_level = user32.GetAncestor(child, 2) or child
            except (AttributeError, OSError, ctypes.ArgumentError, tk.TclError):
                top_level = None
        try:
            self.window.withdraw()
        except tk.TclError:
            return
        if not top_level:
            return
        try:
            user32.ShowWindow(top_level, 0)
            ctypes.windll.dwmapi.DwmFlush()
        except (AttributeError, OSError, ctypes.ArgumentError):
            pass


class PinManager:
    """管理所有贴图窗口。"""

    def __init__(
        self,
        root: tk.Tk,
        copy_func: Callable[[Image.Image], None],
        save_func: Callable[[Image.Image], Path],
        status_func: Callable[[str], None],
        default_opacity: float = 1.0,
        always_on_top: bool = True,
        restore_limit: int = 3,
        max_window_size: int = 12000,
        thumbnail_size: Tuple[int, int] = (180, 120),
        ocr_func: Optional[Callable[[Image.Image], None]] = None,
    ) -> None:
        self.root = root
        self.copy_func = copy_func
        self.save_func = save_func
        self.status_func = status_func
        self.default_opacity = max(0.15, min(1.0, float(default_opacity)))
        self.always_on_top = bool(always_on_top)
        self.restore_limit = max(1, min(20, int(restore_limit)))
        self.max_window_size = max(320, min(20000, int(max_window_size)))
        self.thumbnail_size = (
            max(40, min(600, int(thumbnail_size[0]))),
            max(40, min(600, int(thumbnail_size[1]))),
        )
        self.ocr_func = ocr_func
        self.windows: List[PinnedImageWindow] = []
        self.closed_images: List[Image.Image] = []
        self.hidden = False

    def pin_image(
        self,
        image: Image.Image,
        title: str = "贴图",
        position: Optional[Point] = None,
    ) -> PinnedImageWindow:
        window = PinnedImageWindow(
            self.root,
            image,
            on_close=self._on_close,
            copy_func=self.copy_func,
            save_func=self.save_func,
            ocr_func=self.ocr_func,
            title=title,
            position=position,
            snap_targets_func=self._snap_targets,
            initial_opacity=self.default_opacity,
            initial_topmost=self.always_on_top,
            max_window_size=self.max_window_size,
            thumbnail_size=self.thumbnail_size,
        )
        self.windows.append(window)
        self.hidden = False
        self.status_func("已贴到屏幕")
        return window

    def configure_defaults(
        self,
        default_opacity: float,
        always_on_top: bool,
        restore_limit: int,
        max_window_size: Optional[int] = None,
        thumbnail_size: Optional[Tuple[int, int]] = None,
    ) -> None:
        self.default_opacity = max(0.15, min(1.0, float(default_opacity)))
        self.always_on_top = bool(always_on_top)
        self.restore_limit = max(1, min(20, int(restore_limit)))
        if max_window_size is not None:
            self.max_window_size = max(320, min(20000, int(max_window_size)))
        if thumbnail_size is not None:
            self.thumbnail_size = (
                max(40, min(600, int(thumbnail_size[0]))),
                max(40, min(600, int(thumbnail_size[1]))),
            )
        self.closed_images = self.closed_images[-self.restore_limit :]

    def _snap_targets(self, exclude: PinnedImageWindow) -> List[Box]:
        targets: List[Box] = [
            VirtualDesktopGeometry.detect(
                (self.root.winfo_screenwidth(), self.root.winfo_screenheight())
            ).bounds
        ]
        for window in self.windows:
            if window is exclude:
                continue
            try:
                left = window.window.winfo_x()
                top = window.window.winfo_y()
                targets.append(
                    (
                        left,
                        top,
                        left + window.window.winfo_width(),
                        top + window.window.winfo_height(),
                    )
                )
            except tk.TclError:
                continue
        return targets

    def paste_from_clipboard(self) -> None:
        image = self._image_from_clipboard()
        if image is None:
            text = self._text_from_clipboard()
            if text is None:
                self.status_func("剪贴板没有可贴出的图片、文字、颜色或图片文件。")
                return
            image = PinContentFactory.from_text(text)
        self.pin_image(image, "剪贴板贴图")

    def toggle_all(self) -> None:
        if not self.windows and self.closed_images:
            self.restore_last_closed()
            return

        self.hidden = not self.hidden
        for window in list(self.windows):
            if self.hidden:
                window.hide()
            else:
                window.show()
        self.status_func("已隐藏全部贴图" if self.hidden else "已显示全部贴图")

    def restore_last_closed(self) -> None:
        if not self.closed_images:
            self.status_func("没有可恢复的贴图")
            return
        image = self.closed_images.pop()
        self.pin_image(image, "恢复贴图")

    def destroy_all(self) -> None:
        for window in list(self.windows):
            try:
                window.close(destroy=True)
            except tk.TclError:
                pass
        self.windows.clear()
        self.closed_images.clear()
        self.status_func("已销毁全部贴图")

    def _on_close(self, window: PinnedImageWindow, destroy: bool) -> None:
        if window in self.windows:
            self.windows.remove(window)
        if not destroy:
            self.closed_images.append(window.current_image())
            self.closed_images = self.closed_images[-self.restore_limit :]
        self.status_func("贴图已销毁" if destroy else "贴图已关闭，可再次恢复")

    def _image_from_clipboard(self) -> Optional[Image.Image]:
        try:
            data = ImageGrab.grabclipboard()
        except Exception:
            return None

        if isinstance(data, Image.Image):
            return data.convert("RGB")

        if isinstance(data, list):
            for value in data:
                path = Path(value)
                if path.is_file():
                    try:
                        return Image.open(path).convert("RGB")
                    except Exception:
                        continue
        return None

    def _text_from_clipboard(self) -> Optional[str]:
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            return None
        return text if text.strip() else None


class ColorPickerOverlay:
    """全屏取色器，带放大镜和 RGB/HEX 复制。"""

    def __init__(
        self,
        parent: tk.Tk,
        screen_image: Image.Image,
        on_pick: Callable[[Tuple[int, int, int], str], None],
        on_cancel: Callable[[], None],
        color_format: str = "hex",
        screen_origin: Point = (0, 0),
        magnifier_zoom_index: int = CaptureMagnifier.DEFAULT_ZOOM_INDEX,
    ) -> None:
        self.parent = parent
        self.screen_image = screen_image.convert("RGB")
        self.screen_geometry = VirtualDesktopGeometry(
            int(screen_origin[0]),
            int(screen_origin[1]),
            screen_image.width,
            screen_image.height,
        )
        self.on_pick = on_pick
        self.on_cancel = on_cancel
        self.color_format = color_format
        self.mouse_point: Point = (0, 0)
        self.background = ImageTk.PhotoImage(self.screen_image)
        self.magnifier = CaptureMagnifier()
        self.magnifier.zoom_index = max(
            0,
            min(len(self.magnifier.ZOOM_SAMPLES) - 1, int(magnifier_zoom_index)),
        )
        self.shift_format_latched = False

        self.window = tk.Toplevel(parent)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.geometry(self.screen_geometry.window_geometry)
        self.window.configure(cursor="none")
        self.color_copy_key_monitor = WindowsPhysicalKeyMonitor(
            self.window,
            ord("C"),
            self._pick,
        )

        self.canvas = tk.Canvas(
            self.window,
            width=screen_image.width,
            height=screen_image.height,
            highlightthickness=0,
            cursor="none",
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.create_image(0, 0, image=self.background, anchor=tk.NW)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Button-1>", self._pick)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        if not self.color_copy_key_monitor.start():
            self.window.bind("<c>", self._pick)
            self.window.bind("<C>", self._pick)
        self.window.bind("<Shift_L>", self._toggle_format)
        self.window.bind("<Shift_R>", self._toggle_format)
        self.window.bind("<KeyRelease-Shift_L>", self._release_format_toggle)
        self.window.bind("<KeyRelease-Shift_R>", self._release_format_toggle)
        self.window.bind("<Escape>", self._cancel)
        self.window.grab_set()
        self.window.focus_force()

    def _on_motion(self, event: tk.Event) -> None:
        self.mouse_point = (
            self._clamp(event.x, 0, self.screen_image.width - 1),
            self._clamp(event.y, 0, self.screen_image.height - 1),
        )
        self._draw()

    def _draw(self) -> None:
        self.canvas.delete("picker")
        self.magnifier.draw(
            self.canvas,
            self.screen_image,
            self.mouse_point,
            self.color_format,
            self.screen_geometry.origin,
            tag="picker",
        )

    def _on_wheel(self, event: tk.Event) -> str:
        if self.magnifier.adjust_zoom(1 if event.delta > 0 else -1):
            self._draw()
        return "break"

    def _pick(self, event: Optional[tk.Event] = None) -> None:
        color = self.screen_image.getpixel(self.mouse_point)
        value = self._format_color(color)
        self._close()
        self.on_pick(color, value)

    def _toggle_format(self, event: Optional[tk.Event] = None) -> None:
        if self.shift_format_latched:
            return
        self.shift_format_latched = True
        self.color_format = "rgb" if self.color_format == "hex" else "hex"
        self._draw()

    def _release_format_toggle(self, event: Optional[tk.Event] = None) -> None:
        self.shift_format_latched = False

    def _cancel(self, event: Optional[tk.Event] = None) -> None:
        self._close()
        self.on_cancel()

    def _format_color(self, color: Tuple[int, int, int]) -> str:
        red, green, blue = color
        if self.color_format == "rgb":
            return f"RGB({red}, {green}, {blue})"
        return f"#{red:02X}{green:02X}{blue:02X}"

    def _close(self) -> None:
        self.color_copy_key_monitor.stop()
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        self.window.destroy()

    @staticmethod
    def _clamp(value: int, minimum: int, maximum: int) -> int:
        return max(minimum, min(maximum, int(value)))


class OcrResultDialog(tk.Toplevel):
    """展示原图与 OCR 坐标，并支持从图片或文本区复制。"""

    WIDTH = 1100
    HEIGHT = 700
    MIN_WIDTH = 820
    MIN_HEIGHT = 540
    CANVAS_MARGIN = 16
    ACCENT = "#087EA4"
    ACCENT_SOFT = "#D7EEF5"

    def __init__(
        self,
        parent: tk.Tk,
        image: Image.Image,
        backend: Optional[WindowsOcrBackend] = None,
        title: str = "图片文字",
        on_close: Optional[Callable[[], None]] = None,
        status_func: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(parent)
        self.parent = parent
        self.image = image.convert("RGB")
        self.backend = backend or WindowsOcrBackend()
        self.on_close_callback = on_close
        self.status_func = status_func
        self.document: Optional[OcrDocument] = None
        self.selection_model: Optional[OcrSelectionModel] = None
        self.selected_words: set[Tuple[int, int]] = set()
        self.hovered_line: Optional[int] = None
        self.drag_start_image: Optional[Point] = None
        self.drag_box_image: Optional[Box] = None
        self.fit_mode = True
        self.render_scale = 1.0
        self.image_origin: Point = (self.CANVAS_MARGIN, self.CANVAS_MARGIN)
        self.photo: Optional[ImageTk.PhotoImage] = None
        self.render_job: Optional[str] = None
        self.poll_job: Optional[str] = None
        self.result_queue: queue.SimpleQueue[Tuple[int, str, object]] = queue.SimpleQueue()
        self.recognition_generation = 0
        self.closed = False
        self.line_tags: List[str] = []

        self.title(f"提取文字 - {title}")
        self.configure(bg="#F3F5F7")
        self.minsize(self.MIN_WIDTH, self.MIN_HEIGHT)
        self.status_var = tk.StringVar(value="正在识别...")
        self.zoom_var = tk.StringVar(value="100%")
        self._build_ui()
        self._center_at_pointer()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda event: self.close())
        self.after(60, self._start_recognition)
        self.after(80, self._render_image)
        self.after(120, self._set_initial_sash)
        self.lift()
        self.focus_force()

    def _build_ui(self) -> None:
        shell = ttk.Frame(self, padding=(14, 12))
        shell.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(shell)
        header.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(
            header,
            text="图片文字",
            font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(side=tk.LEFT)
        self.recognize_button = ttk.Button(
            header,
            text="重新识别",
            command=self._start_recognition,
        )
        self.recognize_button.pack(side=tk.RIGHT)
        ttk.Label(
            header,
            textvariable=self.status_var,
            foreground="#667685",
        ).pack(side=tk.RIGHT, padx=(0, 12))

        self.panes = ttk.Panedwindow(shell, orient=tk.HORIZONTAL)
        self.panes.pack(fill=tk.BOTH, expand=True)
        left = ttk.Frame(self.panes, width=680)
        right = ttk.Frame(self.panes, width=390)
        self.panes.add(left, weight=3)
        self.panes.add(right, weight=2)

        image_toolbar = ttk.Frame(left)
        image_toolbar.pack(fill=tk.X, pady=(0, 7))
        ttk.Label(
            image_toolbar,
            text="原图",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side=tk.LEFT)
        ttk.Button(
            image_toolbar,
            text="+",
            width=3,
            command=lambda: self._zoom(1.25),
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
            command=lambda: self._zoom(0.8),
        ).pack(side=tk.RIGHT)
        ttk.Label(
            image_toolbar,
            textvariable=self.zoom_var,
            foreground="#667685",
            width=7,
            anchor=tk.E,
        ).pack(side=tk.RIGHT, padx=(0, 9))

        canvas_frame = ttk.Frame(left)
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
        self.canvas.bind("<ButtonPress-1>", self._on_image_press)
        self.canvas.bind("<B1-Motion>", self._on_image_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_image_release)
        self.canvas.bind("<Double-Button-1>", self._on_image_double_click)
        self.canvas.bind("<Motion>", self._on_image_motion)
        self.canvas.bind("<Leave>", self._on_image_leave)
        self.canvas.bind("<MouseWheel>", self._on_image_wheel)
        self.canvas.bind("<Control-c>", self._copy_selected)
        self.canvas.bind("<Control-a>", self._select_all_image_text)

        self.image_menu = tk.Menu(self.canvas, tearoff=False)
        self.image_menu.add_command(label="复制选中", command=self._copy_selected)
        self.image_menu.add_command(label="复制全部", command=self._copy_all)
        self.canvas.bind("<Button-3>", self._show_image_menu)

        text_header = ttk.Frame(right)
        text_header.pack(fill=tk.X, padx=(12, 0), pady=(0, 7))
        ttk.Label(
            text_header,
            text="识别文本",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side=tk.LEFT)
        self.copy_all_button = ttk.Button(
            text_header,
            text="复制全部",
            command=self._copy_all,
            state="disabled",
        )
        self.copy_all_button.pack(side=tk.RIGHT)
        self.copy_selected_button = ttk.Button(
            text_header,
            text="复制选中",
            command=self._copy_selected,
            state="disabled",
        )
        self.copy_selected_button.pack(side=tk.RIGHT, padx=(0, 6))

        text_frame = ttk.Frame(right)
        text_frame.pack(fill=tk.BOTH, expand=True, padx=(12, 0))
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)
        self.text = tk.Text(
            text_frame,
            wrap=tk.WORD,
            undo=True,
            exportselection=False,
            relief=tk.FLAT,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground="#C9D3DC",
            highlightcolor=self.ACCENT,
            bg="#FFFFFF",
            fg="#17212B",
            insertbackground="#17212B",
            selectbackground="#B9DDEA",
            font=("Microsoft YaHei UI", 11),
            padx=12,
            pady=12,
        )
        text_scroll = ttk.Scrollbar(
            text_frame,
            orient=tk.VERTICAL,
            command=self.text.yview,
        )
        self.text.configure(yscrollcommand=text_scroll.set)
        self.text.grid(row=0, column=0, sticky=tk.NSEW)
        text_scroll.grid(row=0, column=1, sticky=tk.NS)
        self.text.tag_configure("ocr_image_selection", background=self.ACCENT_SOFT)
        self.text.bind("<Control-a>", self._select_all_text)

    def _start_recognition(self) -> None:
        if self.closed:
            return
        self.recognition_generation += 1
        generation = self.recognition_generation
        self.status_var.set("正在识别...")
        self.recognize_button.configure(state="disabled")
        self.copy_selected_button.configure(state="disabled")
        self.copy_all_button.configure(state="disabled")
        self.canvas.delete("ocr_overlay")

        def recognize() -> None:
            try:
                result = self.backend.recognize(self.image)
            except Exception as exc:
                self.result_queue.put((generation, "error", exc))
            else:
                self.result_queue.put((generation, "result", result))

        threading.Thread(
            target=recognize,
            name="screenshot-ocr",
            daemon=True,
        ).start()
        if self.poll_job:
            try:
                self.after_cancel(self.poll_job)
            except tk.TclError:
                pass
        self.poll_job = self.after(60, self._poll_recognition)

    def _poll_recognition(self) -> None:
        self.poll_job = None
        if self.closed:
            return
        try:
            generation, result_type, value = self.result_queue.get_nowait()
        except queue.Empty:
            self.poll_job = self.after(60, self._poll_recognition)
            return
        if generation != self.recognition_generation:
            self.poll_job = self.after(20, self._poll_recognition)
            return
        self.recognize_button.configure(state="normal")
        if result_type == "error":
            self.status_var.set("识别失败")
            messagebox.showerror("图片文字识别失败", str(value), parent=self)
            if self.status_func:
                self.status_func(f"图片文字识别失败：{value}")
            return
        if not isinstance(value, OcrDocument):
            self.status_var.set("识别失败")
            return
        self._set_document(value)

    def _set_document(self, document: OcrDocument) -> None:
        self.document = document
        self.selection_model = OcrSelectionModel(document)
        self.selected_words.clear()
        self.line_tags.clear()
        self.text.delete("1.0", tk.END)
        for index, line in enumerate(document.lines):
            start = self.text.index(tk.END + "-1c")
            self.text.insert(tk.END, line.text)
            end = self.text.index(tk.END + "-1c")
            tag = f"ocr_source_line_{index}"
            self.text.tag_add(tag, start, end)
            self.line_tags.append(tag)
            if index < len(document.lines) - 1:
                self.text.insert(tk.END, "\n")
        if document.lines:
            self.status_var.set(
                f"已识别 {len(document.lines)} 行 · {document.word_count} 个文字块"
            )
            self.copy_selected_button.configure(state="normal")
            self.copy_all_button.configure(state="normal")
        else:
            self.status_var.set("未识别到文字")
        if self.status_func:
            self.status_func(self.status_var.get())
        self._draw_ocr_overlays()

    def _schedule_render(self, event: Optional[tk.Event] = None) -> None:
        if not self.fit_mode:
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
        canvas_width = max(320, self.canvas.winfo_width())
        canvas_height = max(260, self.canvas.winfo_height())
        if self.fit_mode:
            self.render_scale = min(
                2.0,
                max(
                    0.03,
                    min(
                        (canvas_width - self.CANVAS_MARGIN * 2) / max(1, self.image.width),
                        (canvas_height - self.CANVAS_MARGIN * 2) / max(1, self.image.height),
                    ),
                ),
            )
        max_scale_by_pixels = math.sqrt(
            36_000_000 / max(1, self.image.width * self.image.height)
        )
        self.render_scale = max(
            0.02,
            min(4.0, max_scale_by_pixels, self.render_scale),
        )
        size = (
            max(1, int(round(self.image.width * self.render_scale))),
            max(1, int(round(self.image.height * self.render_scale))),
        )
        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        preview = self.image.resize(size, resampling) if size != self.image.size else self.image
        self.photo = ImageTk.PhotoImage(preview)
        origin_x = (
            max(self.CANVAS_MARGIN, (canvas_width - size[0]) // 2)
            if size[0] < canvas_width
            else self.CANVAS_MARGIN
        )
        origin_y = (
            max(self.CANVAS_MARGIN, (canvas_height - size[1]) // 2)
            if size[1] < canvas_height
            else self.CANVAS_MARGIN
        )
        self.image_origin = origin_x, origin_y
        self.canvas.delete("all")
        self.canvas.create_image(
            origin_x,
            origin_y,
            image=self.photo,
            anchor=tk.NW,
            tags=("ocr_image",),
        )
        self.canvas.configure(
            scrollregion=(
                0,
                0,
                max(canvas_width, origin_x + size[0] + self.CANVAS_MARGIN),
                max(canvas_height, origin_y + size[1] + self.CANVAS_MARGIN),
            )
        )
        self.zoom_var.set(f"{int(round(self.render_scale * 100))}%")
        self._draw_ocr_overlays()

    def _draw_ocr_overlays(self) -> None:
        self.canvas.delete("ocr_overlay")
        if not self.document:
            return
        for line_index, line in enumerate(self.document.lines):
            line_box = self._canvas_box(line.box)
            self.canvas.create_rectangle(
                line_box,
                outline="#19A7CE" if line_index != self.hovered_line else "#00C8F0",
                width=2 if line_index == self.hovered_line else 1,
                tags=("ocr_overlay",),
            )
            for word_index, word in enumerate(line.words):
                if (line_index, word_index) not in self.selected_words:
                    continue
                self.canvas.create_rectangle(
                    self._canvas_box(word.box),
                    fill=self.ACCENT,
                    outline="#FFFFFF",
                    width=1,
                    stipple="gray50",
                    tags=("ocr_overlay",),
                )
        if self.drag_box_image:
            self.canvas.create_rectangle(
                self._canvas_box(self.drag_box_image),
                outline="#FFFFFF",
                width=1,
                dash=(4, 3),
                tags=("ocr_overlay",),
            )

    def _on_image_press(self, event: tk.Event) -> str:
        self.canvas.focus_set()
        self.text.tag_remove(tk.SEL, "1.0", tk.END)
        point = self._image_point(event)
        self.drag_start_image = point
        self.drag_box_image = None
        if self.selection_model:
            line_index = self.selection_model.line_at(point)
            self.selected_words = (
                self.selection_model.line_words(line_index)
                if line_index is not None
                else set()
            )
            self._sync_selection_display()
        return "break"

    def _on_image_drag(self, event: tk.Event) -> str:
        if not self.drag_start_image or not self.selection_model:
            return "break"
        point = self._image_point(event)
        self.drag_box_image = (
            self.drag_start_image[0],
            self.drag_start_image[1],
            point[0],
            point[1],
        )
        self.selected_words = self.selection_model.words_in_box(self.drag_box_image)
        self._sync_selection_display()
        return "break"

    def _on_image_release(self, event: tk.Event) -> str:
        self.drag_start_image = None
        self.drag_box_image = None
        self._draw_ocr_overlays()
        return "break"

    def _on_image_double_click(self, event: tk.Event) -> str:
        if not self.selection_model:
            return "break"
        line_index = self.selection_model.line_at(self._image_point(event))
        if line_index is None:
            return "break"
        self.selected_words = self.selection_model.line_words(line_index)
        self._sync_selection_display()
        self._copy_selected()
        return "break"

    def _on_image_motion(self, event: tk.Event) -> None:
        if self.drag_start_image or not self.selection_model:
            return
        line_index = self.selection_model.line_at(self._image_point(event))
        if line_index == self.hovered_line:
            return
        self.hovered_line = line_index
        self.canvas.configure(cursor="hand2" if line_index is not None else "crosshair")
        self._draw_ocr_overlays()

    def _on_image_leave(self, event: Optional[tk.Event] = None) -> None:
        if self.hovered_line is None:
            return
        self.hovered_line = None
        self._draw_ocr_overlays()

    def _on_image_wheel(self, event: tk.Event) -> str:
        if event.state & 0x0004:
            self._zoom(1.15 if event.delta > 0 else 0.87)
        elif event.state & 0x0001:
            self.canvas.xview_scroll(-1 if event.delta > 0 else 1, "units")
        else:
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def _zoom(self, factor: float) -> None:
        self.fit_mode = False
        self.render_scale = max(0.02, min(4.0, self.render_scale * factor))
        self._render_image()

    def _fit_image(self) -> None:
        self.fit_mode = True
        self._render_image()

    def _set_initial_sash(self) -> None:
        if self.closed:
            return
        try:
            self.panes.sashpos(0, max(460, int(self.panes.winfo_width() * 0.62)))
        except tk.TclError:
            pass
        if self.fit_mode:
            self._render_image()

    def _select_all_image_text(self, event: Optional[tk.Event] = None) -> str:
        self.text.tag_remove(tk.SEL, "1.0", tk.END)
        if self.selection_model:
            self.selected_words = self.selection_model.all_words()
            self._sync_selection_display()
        return "break"

    def _select_all_text(self, event: Optional[tk.Event] = None) -> str:
        self.text.tag_add(tk.SEL, "1.0", tk.END + "-1c")
        self.text.mark_set(tk.INSERT, "1.0")
        self.text.see(tk.INSERT)
        return "break"

    def _sync_selection_display(self) -> None:
        self.text.tag_remove("ocr_image_selection", "1.0", tk.END)
        selected_lines = {line_index for line_index, _ in self.selected_words}
        for line_index in selected_lines:
            if not 0 <= line_index < len(self.line_tags):
                continue
            ranges = self.text.tag_ranges(self.line_tags[line_index])
            if len(ranges) == 2:
                self.text.tag_add("ocr_image_selection", ranges[0], ranges[1])
        self._draw_ocr_overlays()

    def _copy_selected(self, event: Optional[tk.Event] = None) -> str:
        text = ""
        try:
            text = self.text.get(tk.SEL_FIRST, tk.SEL_LAST)
        except tk.TclError:
            if self.selection_model:
                text = self.selection_model.text_for(self.selected_words)
        return self._copy_text(text, "已复制选中文字")

    def _copy_all(self) -> str:
        return self._copy_text(
            self.text.get("1.0", tk.END + "-1c"),
            "已复制全部文字",
        )

    def _copy_text(self, text: str, message: str) -> str:
        value = text.strip()
        if not value:
            self.status_var.set("未选择文字")
            return "break"
        self.clipboard_clear()
        self.clipboard_append(value)
        self.update_idletasks()
        self.status_var.set(message)
        if self.status_func:
            self.status_func(message)
        return "break"

    def _show_image_menu(self, event: tk.Event) -> str:
        try:
            self.image_menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                self.image_menu.grab_release()
            except tk.TclError:
                pass
        return "break"

    def _image_point(self, event: tk.Event) -> Point:
        canvas_x = self.canvas.canvasx(event.x)
        canvas_y = self.canvas.canvasy(event.y)
        x = int(round((canvas_x - self.image_origin[0]) / max(0.001, self.render_scale)))
        y = int(round((canvas_y - self.image_origin[1]) / max(0.001, self.render_scale)))
        return (
            max(0, min(self.image.width - 1, x)),
            max(0, min(self.image.height - 1, y)),
        )

    def _canvas_box(self, box: Box) -> Box:
        left, top, right, bottom = ScreenshotManager._normalize_box(box)
        return (
            self.image_origin[0] + int(round(left * self.render_scale)),
            self.image_origin[1] + int(round(top * self.render_scale)),
            self.image_origin[0] + int(round(right * self.render_scale)),
            self.image_origin[1] + int(round(bottom * self.render_scale)),
        )

    def _center_at_pointer(self) -> None:
        self.update_idletasks()
        pointer = self.winfo_pointerx(), self.winfo_pointery()
        work_area = MonitorWorkArea.at_point(pointer)
        width = max(620, min(self.WIDTH, max(1, work_area.width - 24)))
        height = max(460, min(self.HEIGHT, max(1, work_area.height - 56)))
        self.minsize(min(self.MIN_WIDTH, width), min(self.MIN_HEIGHT, height))
        horizontal_margin = 8
        titlebar_margin = 48
        x = max(
            work_area.left + horizontal_margin,
            min(
                pointer[0] - width // 2,
                work_area.right - width - horizontal_margin,
            ),
        )
        y = max(
            work_area.top + horizontal_margin,
            min(
                pointer[1] - height // 2,
                work_area.bottom - height - titlebar_margin,
            ),
        )
        self.geometry(f"{width}x{height}{x:+d}{y:+d}")

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.poll_job:
            try:
                self.after_cancel(self.poll_job)
            except tk.TclError:
                pass
            self.poll_job = None
        self.destroy()
        if self.on_close_callback:
            self.on_close_callback()


class HistoryDialog(tk.Toplevel):
    """截图历史窗口，可加载、贴图、复制、清空。"""

    def __init__(
        self,
        parent: tk.Tk,
        history: ScreenshotHistoryManager,
        on_load: Callable[[Image.Image], None],
        on_pin: Callable[[Image.Image], None],
        on_copy: Callable[[Image.Image], None],
        on_clear: Callable[[], None],
        on_ocr: Optional[Callable[[Image.Image], None]] = None,
    ) -> None:
        super().__init__(parent)
        self.title("截图历史")
        self.geometry("780x480")
        self.transient(parent)
        self.history = history
        self.on_load = on_load
        self.on_pin = on_pin
        self.on_copy = on_copy
        self.on_clear = on_clear
        self.on_ocr = on_ocr
        self.preview_photo: Optional[ImageTk.PhotoImage] = None

        body = ttk.Frame(self, padding=12)
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        self.listbox = tk.Listbox(body, width=34, activestyle="none")
        self.listbox.grid(row=0, column=0, sticky=tk.NS, padx=(0, 12))
        self.listbox.bind("<<ListboxSelect>>", lambda event: self._render_preview())
        self.listbox.bind("<Double-Button-1>", lambda event: self._load())

        self.preview_canvas = tk.Canvas(body, bg="#ffffff", highlightthickness=1, highlightbackground="#d9e0e7")
        self.preview_canvas.grid(row=0, column=1, sticky=tk.NSEW)

        buttons = ttk.Frame(body)
        buttons.grid(row=1, column=0, columnspan=2, sticky=tk.E, pady=(12, 0))
        ttk.Button(buttons, text="加载到预览", command=self._load).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(buttons, text="贴到屏幕", command=self._pin).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(buttons, text="复制", command=self._copy).pack(side=tk.LEFT, padx=(0, 8))
        if self.on_ocr:
            ttk.Button(buttons, text="提取文字", command=self._ocr).pack(
                side=tk.LEFT,
                padx=(0, 8),
            )
        ttk.Button(buttons, text="清空历史", command=self._clear).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(buttons, text="关闭", command=self.destroy).pack(side=tk.LEFT)

        self._populate()
        self._render_preview()
        self.focus_force()

    def _populate(self) -> None:
        self.listbox.delete(0, tk.END)
        for record in reversed(self.history.all_records()):
            label = f"{record.get('time', '')}  {record.get('width')}x{record.get('height')}  {record.get('kind', '')}"
            self.listbox.insert(tk.END, label)
        if self.listbox.size():
            self.listbox.selection_set(0)

    def _selected_record(self) -> Optional[dict]:
        selection = self.listbox.curselection()
        if not selection:
            return None
        records = list(reversed(self.history.all_records()))
        index = int(selection[0])
        if 0 <= index < len(records):
            return records[index]
        return None

    def _selected_image(self) -> Optional[Image.Image]:
        record = self._selected_record()
        return self.history.load_image(record) if record else None

    def _render_preview(self) -> None:
        self.preview_canvas.delete("all")
        image = self._selected_image()
        if image is None:
            self.preview_canvas.create_text(280, 180, text="暂无历史记录", fill="#64748b")
            return

        canvas_width = max(1, self.preview_canvas.winfo_width() - 24)
        canvas_height = max(1, self.preview_canvas.winfo_height() - 24)
        preview = image.copy()
        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        preview.thumbnail((canvas_width, canvas_height), resampling)
        self.preview_photo = ImageTk.PhotoImage(preview)
        self.preview_canvas.create_image(
            self.preview_canvas.winfo_width() // 2,
            self.preview_canvas.winfo_height() // 2,
            image=self.preview_photo,
            anchor=tk.CENTER,
        )

    def _load(self) -> None:
        image = self._selected_image()
        if image:
            self.on_load(image)

    def _pin(self) -> None:
        image = self._selected_image()
        if image:
            self.on_pin(image)

    def _copy(self) -> None:
        image = self._selected_image()
        if image:
            self.on_copy(image)

    def _ocr(self) -> None:
        image = self._selected_image()
        if image and self.on_ocr:
            self.on_ocr(image)

    def _clear(self) -> None:
        if messagebox.askyesno("确认", "确定清空所有截图历史吗？", parent=self):
            self.history.clear()
            self.on_clear()
            self._populate()
            self._render_preview()


class HotkeyCaptureDialog(tk.Toplevel):
    """捕获一次键盘组合并返回规范化快捷键文本。"""

    def __init__(
        self,
        parent: tk.Toplevel,
        action_label: str,
        current: str,
        global_hotkey: bool,
    ) -> None:
        super().__init__(parent)
        self.title("设置快捷键")
        self.geometry("460x240")
        self.resizable(False, False)
        self.transient(parent)
        self.configure(bg=PreferencesDialog.BG)
        parent_icon = getattr(parent, "window_icon", None)
        if parent_icon is not None:
            self.iconphoto(False, parent_icon)
        self.result: Optional[str] = None
        self.global_hotkey = global_hotkey
        self.candidate = current
        self.value_var = tk.StringVar(value=current or "未设置")
        self.status_var = tk.StringVar(value="按下新的快捷键组合")

        body = ttk.Frame(
            self,
            style="PrefsPage.TFrame",
            padding=(22, 18),
        )
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            body,
            text=action_label,
            style="PrefsSection.TLabel",
        ).pack(anchor=tk.W)
        accent_var = getattr(parent, "accent_color_var", None)
        accent = AppTheme.normalize_accent(
            accent_var.get() if accent_var is not None else AppTheme.DEFAULT_ACCENT
        )
        value_panel = tk.Frame(
            body,
            bg="#ffffff",
            highlightthickness=1,
            highlightbackground=accent,
            height=60,
        )
        value_panel.pack(fill=tk.X, pady=(12, 8))
        value_panel.pack_propagate(False)
        tk.Label(
            value_panel,
            textvariable=self.value_var,
            bg="#ffffff",
            fg=accent,
            font=("Segoe UI", 15, "bold"),
        ).pack(expand=True)
        ttk.Label(
            body,
            textvariable=self.status_var,
            style="PrefsMuted.TLabel",
        ).pack(anchor=tk.W)

        actions = ttk.Frame(body, style="PrefsPage.TFrame")
        actions.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(
            actions,
            text="清除",
            style="Prefs.TButton",
            command=self._clear,
        ).pack(side=tk.LEFT)
        ttk.Button(
            actions,
            text="取消",
            style="Prefs.TButton",
            command=self._cancel,
        ).pack(side=tk.RIGHT)
        ttk.Button(
            actions,
            text="确定",
            style="PrefsPrimary.TButton",
            command=self._accept,
        ).pack(
            side=tk.RIGHT,
            padx=(0, 8),
        )

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<KeyPress>", self._on_key_press)
        self.grab_set()
        self._center(parent)
        self.focus_force()

    @classmethod
    def ask(
        cls,
        parent: tk.Toplevel,
        action_label: str,
        current: str,
        global_hotkey: bool,
    ) -> Optional[str]:
        dialog = cls(parent, action_label, current, global_hotkey)
        parent.wait_window(dialog)
        return dialog.result

    def _on_key_press(self, event: tk.Event) -> str:
        if event.keysym == "Escape":
            self._cancel()
            return "break"
        try:
            captured = HotkeyCodec.from_tk_event(event)
            if captured:
                HotkeyCodec._validate_safe_chord(captured, self.global_hotkey)
                self.candidate = captured
                self.value_var.set(captured)
                self.status_var.set("快捷键已捕获")
        except ValueError as exc:
            self.status_var.set(str(exc))
        return "break"

    def _accept(self) -> None:
        try:
            self.result = HotkeyCodec.normalize(self.candidate)
        except ValueError as exc:
            self.status_var.set(str(exc))
            return
        self.destroy()

    def _clear(self) -> None:
        self.result = ""
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()

    def _center(self, parent: tk.Toplevel) -> None:
        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - height) // 2
        desktop = VirtualDesktopGeometry.detect()
        x = max(desktop.left, min(x, desktop.left + desktop.width - width))
        y = max(desktop.top, min(y, desktop.top + desktop.height - height))
        self.geometry(f"{width}x{height}{x:+d}{y:+d}")


class PreferencesDialog(tk.Toplevel):
    """标签式首选项窗口，所有控件都映射到实际运行配置。"""

    WIDTH = 780
    HEIGHT = 600
    BG = "#F3F5F7"
    SURFACE = "#FFFFFF"
    BORDER = "#D7DEE5"
    TEXT = "#17212B"
    MUTED = "#667685"

    def __init__(
        self,
        parent: tk.Tk,
        config: PersistedAppConfig,
        hotkey_status: str = "",
        hotkey_registration: Optional[dict[str, str]] = None,
    ) -> None:
        super().__init__(parent)
        self.title("ScreenshotTool 首选项")
        self.geometry(f"{self.WIDTH}x{self.HEIGHT}")
        self.minsize(700, 520)
        self.resizable(True, True)
        self.configure(bg=self.BG)
        self.config = config
        self.result: Optional[PersistedAppConfig] = None
        self.about_icon: Optional[ImageTk.PhotoImage] = None
        self.window_icon = ImageTk.PhotoImage(
            TrayIconFactory.make(32, config.accent_color)
        )
        self.iconphoto(False, self.window_icon)
        self.hotkey_registration = dict(hotkey_registration or {})
        self._configure_preferences_styles(config.accent_color)

        self._create_variables(config)
        self.hotkey_status_var.set(hotkey_status)

        shell = ttk.Frame(self, style="PrefsShell.TFrame")
        shell.pack(fill=tk.BOTH, expand=True)
        header = ttk.Frame(shell, style="PrefsHeader.TFrame", padding=(18, 12))
        header.pack(fill=tk.X)
        ttk.Label(
            header,
            image=self.window_icon,
            style="PrefsHeader.TLabel",
        ).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(
            header,
            text="首选项",
            style="PrefsTitle.TLabel",
        ).pack(side=tk.LEFT)
        ttk.Label(
            header,
            text="ScreenshotTool",
            style="PrefsHeaderMuted.TLabel",
        ).pack(side=tk.RIGHT)

        self.notebook = ttk.Notebook(shell, style="Prefs.TNotebook")

        self.tabs: dict[str, ttk.Frame] = {}
        for label in ("常规", "界面", "截图", "贴图", "输出", "控制", "关于"):
            tab = ttk.Frame(
                self.notebook,
                style="PrefsPage.TFrame",
                padding=(20, 16),
            )
            self.notebook.add(tab, text=label)
            self.tabs[label] = tab

        self._build_general_tab(self.tabs["常规"])
        self._build_interface_tab(self.tabs["界面"])
        self._build_capture_tab(self.tabs["截图"])
        self._build_pin_tab(self.tabs["贴图"])
        self._build_output_tab(self.tabs["输出"])
        self._build_control_tab(self.tabs["控制"])
        self._build_about_tab(self.tabs["关于"])

        footer = ttk.Frame(
            shell,
            style="PrefsFooter.TFrame",
            padding=(18, 11),
        )
        footer.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(
            footer,
            text="恢复默认",
            style="Prefs.TButton",
            command=self._restore_defaults,
        ).pack(side=tk.LEFT)
        ttk.Button(
            footer,
            text="取消",
            style="Prefs.TButton",
            command=self._cancel,
        ).pack(side=tk.RIGHT)
        ttk.Button(
            footer,
            text="保存设置",
            style="PrefsPrimary.TButton",
            command=self._accept,
        ).pack(
            side=tk.RIGHT,
            padx=(0, 8),
        )
        self.notebook.pack(
            side=tk.TOP,
            fill=tk.BOTH,
            expand=True,
            padx=14,
        )

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda event: self._cancel())
        self.bind("<Control-Return>", lambda event: self._accept())
        self._center(parent)
        self.lift()
        self.focus_force()

    @classmethod
    def ask(
        cls,
        parent: tk.Tk,
        config: PersistedAppConfig,
        hotkey_status: str = "",
        hotkey_registration: Optional[dict[str, str]] = None,
    ) -> Optional[PersistedAppConfig]:
        dialog = cls(parent, config, hotkey_status, hotkey_registration)
        parent.wait_window(dialog)
        return dialog.result

    def _create_variables(self, config: PersistedAppConfig) -> None:
        self.output_var = tk.StringVar(value=config.output_dir)
        self.quick_var = tk.StringVar(value=config.quick_save_dir)
        self.history_var = tk.StringVar(value=config.history_dir)
        self.max_history_var = tk.IntVar(value=config.max_history)
        self.auto_save_var = tk.BooleanVar(value=config.auto_save)
        self.image_format_var = tk.StringVar(value=config.image_format)
        self.image_quality_var = tk.IntVar(value=config.image_quality)
        self.filename_pattern_var = tk.StringVar(value=config.filename_pattern)
        self.quick_filename_pattern_var = tk.StringVar(
            value=config.quick_filename_pattern
        )
        self.filename_preview_var = tk.StringVar()
        self.quick_filename_preview_var = tk.StringVar()
        self.color_format_var = tk.StringVar(value=config.color_format)
        self.accent_color_var = tk.StringVar(value=config.accent_color)
        self.accent_value_var = tk.StringVar(value=config.accent_color.upper())
        self.accent_swatches: dict[str, tk.Canvas] = {}
        self.smart_selection_var = tk.BooleanVar(value=config.smart_selection)
        self.capture_delay_var = tk.IntVar(value=config.capture_delay)
        self.mask_opacity_var = tk.DoubleVar(value=config.capture_mask_opacity)
        self.mask_opacity_label_var = tk.StringVar()
        self.border_width_var = tk.IntVar(value=config.capture_border_width)
        self.show_handles_var = tk.BooleanVar(value=config.capture_show_handles)
        self.show_crosshair_var = tk.BooleanVar(value=config.capture_show_crosshair)
        self.show_magnifier_var = tk.BooleanVar(value=config.capture_show_magnifier)
        self.magnifier_zoom_var = tk.DoubleVar(value=config.magnifier_zoom_index)
        self.magnifier_zoom_label_var = tk.StringVar()
        self.global_hotkeys_var = tk.BooleanVar(value=config.global_hotkeys)
        self.close_to_tray_var = tk.BooleanVar(value=config.close_to_tray)
        self.start_in_tray_var = tk.BooleanVar(value=config.start_in_tray)
        self.launch_at_startup_var = tk.BooleanVar(value=config.launch_at_startup)
        self.show_main_after_capture_var = tk.BooleanVar(
            value=config.show_main_after_capture
        )
        self.pin_opacity_var = tk.DoubleVar(value=config.pin_default_opacity)
        self.pin_opacity_label_var = tk.StringVar()
        self.pin_always_on_top_var = tk.BooleanVar(value=config.pin_always_on_top)
        self.pin_restore_limit_var = tk.IntVar(value=config.pin_restore_limit)
        self.pin_max_size_var = tk.IntVar(value=config.pin_max_size)
        self.pin_thumbnail_width_var = tk.IntVar(value=config.pin_thumbnail_width)
        self.pin_thumbnail_height_var = tk.IntVar(value=config.pin_thumbnail_height)
        self.config_path_var = tk.StringVar(value=str(ConfigStore.CONFIG_FILE.resolve()))
        self.hotkey_values = HotkeyCodec.merge_with_defaults(config.hotkeys)
        self.original_hotkey_values = dict(self.hotkey_values)
        self.hotkey_status_var = tk.StringVar(value="")
        self._update_opacity_label()
        self._update_mask_opacity_label()
        self._update_magnifier_zoom_label()
        self._update_output_preview()
        self.filename_pattern_var.trace_add(
            "write", lambda *_: self._update_output_preview()
        )
        self.quick_filename_pattern_var.trace_add(
            "write", lambda *_: self._update_output_preview()
        )
        self.image_format_var.trace_add(
            "write", lambda *_: self._update_output_preview()
        )

    def _configure_preferences_styles(self, accent_color: str) -> None:
        style = ttk.Style(self)
        try:
            if style.theme_use() != "clam":
                style.theme_use("clam")
        except tk.TclError:
            pass
        accent = AppTheme.normalize_accent(accent_color)
        accent_hover = AppTheme.blend(accent, "#000000", 0.10)
        accent_soft = AppTheme.blend(accent, "#FFFFFF", 0.90)
        style.configure("PrefsShell.TFrame", background=self.BG)
        style.configure("PrefsHeader.TFrame", background=self.SURFACE)
        style.configure("PrefsFooter.TFrame", background=self.BG)
        style.configure("PrefsPage.TFrame", background=self.SURFACE)
        style.configure("PrefsSection.TFrame", background=self.SURFACE)
        style.configure("Prefs.TSeparator", background=self.BORDER)
        style.configure("PrefsHeader.TLabel", background=self.SURFACE)
        style.configure(
            "PrefsHeaderMuted.TLabel",
            background=self.SURFACE,
            foreground=self.MUTED,
        )
        style.configure(
            "PrefsTitle.TLabel",
            background=self.SURFACE,
            foreground=self.TEXT,
            font=("Microsoft YaHei UI", 13, "bold"),
        )
        style.configure(
            "PrefsSection.TLabel",
            background=self.SURFACE,
            foreground=self.TEXT,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.configure(
            "Prefs.TLabel",
            background=self.SURFACE,
            foreground=self.TEXT,
        )
        style.configure(
            "PrefsMuted.TLabel",
            background=self.SURFACE,
            foreground=self.MUTED,
        )
        style.configure(
            "PrefsValue.TLabel",
            background=self.SURFACE,
            foreground=accent,
            font=("Segoe UI", 9, "bold"),
        )
        style.configure(
            "Prefs.TCheckbutton",
            background=self.SURFACE,
            foreground=self.TEXT,
            padding=(0, 4),
        )
        style.map("Prefs.TCheckbutton", background=[("active", self.SURFACE)])
        self._install_checkbox_style(style, accent)
        style.configure(
            "Prefs.TRadiobutton",
            background=self.SURFACE,
            foreground=self.TEXT,
            padding=(0, 3),
        )
        style.map("Prefs.TRadiobutton", background=[("active", self.SURFACE)])
        style.configure(
            "Prefs.TButton",
            background="#FFFFFF",
            foreground=self.TEXT,
            bordercolor=self.BORDER,
            lightcolor=self.BORDER,
            darkcolor=self.BORDER,
            padding=(12, 7),
        )
        style.map(
            "Prefs.TButton",
            background=[("active", "#EDF1F4"), ("pressed", "#E3E8ED")],
        )
        style.configure(
            "PrefsPrimary.TButton",
            background=accent,
            foreground="#FFFFFF",
            bordercolor=accent,
            padding=(15, 7),
        )
        style.map(
            "PrefsPrimary.TButton",
            background=[("active", accent_hover), ("pressed", accent_hover)],
            foreground=[("disabled", "#DCE8EC")],
        )
        style.configure(
            "Prefs.TNotebook",
            background=self.BG,
            borderwidth=0,
            tabmargins=(0, 8, 0, 0),
        )
        style.configure(
            "Prefs.TNotebook.Tab",
            background=self.BG,
            foreground=self.MUTED,
            padding=(18, 9),
            borderwidth=0,
            font=("Microsoft YaHei UI", 9),
        )
        style.map(
            "Prefs.TNotebook.Tab",
            background=[("selected", self.SURFACE), ("active", "#E9EDF1")],
            foreground=[("selected", accent), ("active", self.TEXT)],
            padding=[("selected", (18, 9)), ("!selected", (18, 9))],
        )
        style.configure(
            "PrefsSub.TNotebook",
            background=self.SURFACE,
            borderwidth=0,
            tabmargins=(0, 0, 0, 8),
        )
        style.configure(
            "PrefsSub.TNotebook.Tab",
            background="#EEF1F4",
            foreground=self.MUTED,
            padding=(16, 7),
            borderwidth=0,
            font=("Microsoft YaHei UI", 9),
        )
        style.map(
            "PrefsSub.TNotebook.Tab",
            background=[("selected", accent_soft), ("active", "#E4E9ED")],
            foreground=[("selected", accent), ("active", self.TEXT)],
            padding=[("selected", (16, 7)), ("!selected", (16, 7))],
        )
        tab_layout = [
            (
                "Notebook.tab",
                {
                    "sticky": "nswe",
                    "children": [
                        (
                            "Notebook.padding",
                            {
                                "side": "top",
                                "sticky": "nswe",
                                "children": [
                                    ("Notebook.label", {"side": "top", "sticky": ""})
                                ],
                            },
                        )
                    ],
                },
            )
        ]
        style.layout("Prefs.TNotebook.Tab", tab_layout)
        style.layout("PrefsSub.TNotebook.Tab", tab_layout)
        style.configure(
            "Prefs.Treeview",
            background="#FFFFFF",
            fieldbackground="#FFFFFF",
            foreground=self.TEXT,
            rowheight=30,
            bordercolor=self.BORDER,
            borderwidth=1,
        )
        style.configure(
            "Prefs.Treeview.Heading",
            background="#EEF2F5",
            foreground="#354351",
            font=("Microsoft YaHei UI", 9, "bold"),
            padding=(8, 7),
        )
        style.map(
            "Prefs.Treeview",
            background=[("selected", accent_soft)],
            foreground=[("selected", self.TEXT)],
        )

    def _install_checkbox_style(self, style: ttk.Style, accent: str) -> None:
        generation = getattr(self, "_checkbox_style_generation", 0) + 1
        self._checkbox_style_generation = generation
        element_name = f"PrefsCheck{id(self)}_{generation}.indicator"

        def make_image(
            fill: str,
            outline: str,
            check: Optional[str] = None,
        ) -> ImageTk.PhotoImage:
            scale = 4
            size = 16
            image = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle(
                (1 * scale, 1 * scale, 15 * scale, 15 * scale),
                radius=2 * scale,
                fill=fill,
                outline=outline,
                width=1 * scale,
            )
            if check:
                draw.line(
                    [
                        (4 * scale, 8 * scale),
                        (7 * scale, 11 * scale),
                        (12 * scale, 5 * scale),
                    ],
                    fill=check,
                    width=2 * scale,
                    joint="curve",
                )
            resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
            return ImageTk.PhotoImage(
                image.resize((size, size), resampling),
                master=self,
            )

        unchecked = make_image("#FFFFFF", "#9AA6B2")
        checked = make_image(accent, accent, "#FFFFFF")
        disabled = make_image("#EEF1F4", "#C9D0D7")
        disabled_checked = make_image("#B8C2CB", "#B8C2CB", "#FFFFFF")
        self._checkbox_style_images = (
            unchecked,
            checked,
            disabled,
            disabled_checked,
        )
        style.element_create(
            element_name,
            "image",
            unchecked,
            ("disabled", "selected", disabled_checked),
            ("disabled", disabled),
            ("selected", checked),
            sticky="",
        )
        style.layout(
            "Prefs.TCheckbutton",
            [
                (
                    "Checkbutton.padding",
                    {
                        "sticky": "nswe",
                        "children": [
                            (element_name, {"side": "left", "sticky": ""}),
                            (
                                "Checkbutton.focus",
                                {
                                    "side": "left",
                                    "sticky": "w",
                                    "children": [
                                        ("Checkbutton.label", {"sticky": "nswe"})
                                    ],
                                },
                            ),
                        ],
                    },
                )
            ],
        )

    def _section(
        self,
        parent: ttk.Frame,
        title: str,
        top_padding: int = 0,
    ) -> ttk.Frame:
        section = ttk.Frame(parent, style="PrefsSection.TFrame")
        section.pack(fill=tk.X, pady=(top_padding, 0))
        ttk.Label(
            section,
            text=title,
            style="PrefsSection.TLabel",
        ).pack(anchor=tk.W)
        ttk.Separator(
            section,
            orient=tk.HORIZONTAL,
            style="Prefs.TSeparator",
        ).pack(fill=tk.X, pady=(7, 9))
        body = ttk.Frame(section, style="PrefsSection.TFrame")
        body.pack(fill=tk.X)
        return body

    def _subtabs(
        self,
        parent: ttk.Frame,
        labels: Tuple[str, ...],
    ) -> dict[str, ttk.Frame]:
        notebook = ttk.Notebook(parent, style="PrefsSub.TNotebook")
        notebook.pack(fill=tk.BOTH, expand=True)
        pages = {}
        for label in labels:
            page = ttk.Frame(
                notebook,
                style="PrefsPage.TFrame",
                padding=(8, 10),
            )
            notebook.add(page, text=label)
            pages[label] = page
        return pages

    def _build_general_tab(self, tab: ttk.Frame) -> None:
        startup = self._section(tab, "启动与后台")
        startup.columnconfigure(0, weight=1)
        startup.columnconfigure(1, weight=1)
        launch = ttk.Checkbutton(
            startup,
            text="登录 Windows 后自动启动",
            variable=self.launch_at_startup_var,
            style="Prefs.TCheckbutton",
        )
        launch.grid(row=0, column=0, sticky=tk.W, pady=2)
        if platform.system() != "Windows":
            launch.configure(state="disabled")
        ttk.Checkbutton(
            startup,
            text="启动时隐藏到系统托盘",
            variable=self.start_in_tray_var,
            style="Prefs.TCheckbutton",
        ).grid(row=0, column=1, sticky=tk.W, pady=2, padx=(24, 0))
        ttk.Checkbutton(
            startup,
            text="关闭主窗口时最小化到托盘",
            variable=self.close_to_tray_var,
            style="Prefs.TCheckbutton",
        ).grid(row=1, column=0, sticky=tk.W, pady=2)
        ttk.Checkbutton(
            startup,
            text="截图结束后显示主窗口",
            variable=self.show_main_after_capture_var,
            style="Prefs.TCheckbutton",
        ).grid(row=1, column=1, sticky=tk.W, pady=2, padx=(24, 0))

        config_group = self._section(tab, "配置文件", top_padding=20)
        config_group.columnconfigure(1, weight=1)
        ttk.Label(config_group, text="路径", style="Prefs.TLabel").grid(
            row=0, column=0, sticky=tk.W
        )
        ttk.Entry(
            config_group,
            textvariable=self.config_path_var,
            state="readonly",
        ).grid(row=0, column=1, sticky=tk.EW, padx=(12, 0), ipady=3)
        config_actions = ttk.Frame(config_group, style="PrefsSection.TFrame")
        config_actions.grid(row=1, column=1, sticky=tk.W, pady=(10, 0))
        ttk.Button(
            config_actions,
            text="打开所在文件夹",
            style="Prefs.TButton",
            command=self._open_config_folder,
        ).pack(side=tk.LEFT)
        ttk.Button(
            config_actions,
            text="打开配置文件",
            style="Prefs.TButton",
            command=self._open_config_file,
        ).pack(side=tk.LEFT, padx=(8, 0))

    def _build_interface_tab(self, tab: ttk.Frame) -> None:
        pages = self._subtabs(tab, ("外观", "取色与放大镜"))

        appearance = self._section(pages["外观"], "主题")
        appearance.columnconfigure(1, weight=1)
        ttk.Label(appearance, text="强调色", style="Prefs.TLabel").grid(
            row=0, column=0, sticky=tk.W
        )
        swatches = ttk.Frame(appearance, style="PrefsSection.TFrame")
        swatches.grid(row=0, column=1, sticky=tk.W, padx=(18, 0))
        self._build_accent_swatches(swatches)
        ttk.Label(
            appearance,
            textvariable=self.accent_value_var,
            style="PrefsValue.TLabel",
        ).grid(row=0, column=2, sticky=tk.E, padx=(12, 0))

        typography = self._section(pages["外观"], "界面", top_padding=20)
        ttk.Label(typography, text="界面字体", style="Prefs.TLabel").grid(
            row=0, column=0, sticky=tk.W
        )
        ttk.Label(
            typography,
            text="Microsoft YaHei UI · 9",
            style="PrefsMuted.TLabel",
        ).grid(row=0, column=1, sticky=tk.W, padx=(18, 0))

        color_group = self._section(pages["取色与放大镜"], "颜色显示")
        ttk.Label(color_group, text="默认格式", style="Prefs.TLabel").grid(
            row=0, column=0, sticky=tk.W
        )
        ttk.Radiobutton(
            color_group,
            text="HEX",
            value="hex",
            variable=self.color_format_var,
            style="Prefs.TRadiobutton",
        ).grid(row=0, column=1, sticky=tk.W, padx=(24, 8))
        ttk.Radiobutton(
            color_group,
            text="RGB",
            value="rgb",
            variable=self.color_format_var,
            style="Prefs.TRadiobutton",
        ).grid(row=0, column=2, sticky=tk.W)

        zoom = self._section(pages["取色与放大镜"], "默认放大倍率", top_padding=20)
        ttk.Scale(
            zoom,
            from_=0,
            to=len(CaptureMagnifier.ZOOM_SAMPLES) - 1,
            variable=self.magnifier_zoom_var,
            command=lambda value: self._update_magnifier_zoom_label(),
            length=360,
        ).grid(row=0, column=0, sticky=tk.W)
        ttk.Label(
            zoom,
            textvariable=self.magnifier_zoom_label_var,
            style="PrefsValue.TLabel",
            width=7,
            anchor=tk.E,
        ).grid(row=0, column=1, sticky=tk.W, padx=(12, 0))

    def _build_capture_tab(self, tab: ttk.Frame) -> None:
        pages = self._subtabs(tab, ("显示", "行为", "历史"))

        display = self._section(pages["显示"], "选区外观")
        display.columnconfigure(1, weight=1)
        ttk.Label(display, text="边框宽度", style="Prefs.TLabel").grid(
            row=0, column=0, sticky=tk.W, pady=4
        )
        ttk.Spinbox(
            display,
            from_=1,
            to=6,
            textvariable=self.border_width_var,
            width=7,
            justify=tk.CENTER,
        ).grid(row=0, column=1, sticky=tk.W, padx=(18, 6), pady=4)
        ttk.Label(display, text="像素", style="PrefsMuted.TLabel").grid(
            row=0, column=2, sticky=tk.W, pady=4
        )
        ttk.Label(display, text="遮罩深度", style="Prefs.TLabel").grid(
            row=1, column=0, sticky=tk.W, pady=4
        )
        ttk.Scale(
            display,
            from_=20,
            to=85,
            variable=self.mask_opacity_var,
            command=lambda value: self._update_mask_opacity_label(),
            length=330,
        ).grid(row=1, column=1, sticky=tk.W, padx=(18, 8), pady=4)
        ttk.Label(
            display,
            textvariable=self.mask_opacity_label_var,
            style="PrefsValue.TLabel",
            width=6,
            anchor=tk.E,
        ).grid(row=1, column=2, sticky=tk.W, pady=4)

        display_options = self._section(pages["显示"], "辅助显示", top_padding=18)
        display_options.columnconfigure(0, weight=1)
        display_options.columnconfigure(1, weight=1)
        ttk.Checkbutton(
            display_options,
            text="显示选区控制点",
            variable=self.show_handles_var,
            style="Prefs.TCheckbutton",
        ).grid(row=0, column=0, sticky=tk.W)
        ttk.Checkbutton(
            display_options,
            text="显示全屏十字线",
            variable=self.show_crosshair_var,
            style="Prefs.TCheckbutton",
        ).grid(row=0, column=1, sticky=tk.W, padx=(24, 0))
        ttk.Checkbutton(
            display_options,
            text="显示像素放大镜",
            variable=self.show_magnifier_var,
            style="Prefs.TCheckbutton",
        ).grid(row=1, column=0, sticky=tk.W)

        selection = self._section(pages["行为"], "区域选择")
        ttk.Checkbutton(
            selection,
            text="自动识别鼠标下的窗口和原生控件",
            variable=self.smart_selection_var,
            style="Prefs.TCheckbutton",
        ).grid(row=0, column=0, columnspan=3, sticky=tk.W)
        ttk.Label(selection, text="截图延迟", style="Prefs.TLabel").grid(
            row=1, column=0, sticky=tk.W, pady=(14, 4)
        )
        ttk.Spinbox(
            selection,
            from_=0,
            to=10,
            textvariable=self.capture_delay_var,
            width=7,
            justify=tk.CENTER,
        ).grid(row=1, column=1, sticky=tk.W, padx=(18, 6), pady=(14, 4))
        ttk.Label(selection, text="秒", style="PrefsMuted.TLabel").grid(
            row=1, column=2, sticky=tk.W, pady=(14, 4)
        )

        history = self._section(pages["历史"], "截图历史")
        ttk.Label(history, text="最多保留", style="Prefs.TLabel").pack(side=tk.LEFT)
        ttk.Spinbox(
            history,
            from_=1,
            to=500,
            textvariable=self.max_history_var,
            width=8,
            justify=tk.CENTER,
        ).pack(side=tk.LEFT, padx=(10, 6))
        ttk.Label(history, text="条记录", style="PrefsMuted.TLabel").pack(side=tk.LEFT)

    def _build_pin_tab(self, tab: ttk.Frame) -> None:
        pages = self._subtabs(tab, ("显示", "关闭与恢复"))
        behavior = self._section(pages["显示"], "新建贴图")
        behavior.columnconfigure(1, weight=1)
        ttk.Checkbutton(
            behavior,
            text="默认保持窗口置顶",
            variable=self.pin_always_on_top_var,
            style="Prefs.TCheckbutton",
        ).grid(row=0, column=0, columnspan=3, sticky=tk.W, pady=(0, 10))
        ttk.Label(behavior, text="默认透明度", style="Prefs.TLabel").grid(
            row=1, column=0, sticky=tk.W
        )
        ttk.Scale(
            behavior,
            from_=15,
            to=100,
            variable=self.pin_opacity_var,
            command=lambda value: self._update_opacity_label(),
            length=280,
        ).grid(row=1, column=1, sticky=tk.W, padx=(12, 8))
        ttk.Label(
            behavior,
            textvariable=self.pin_opacity_label_var,
            width=6,
            anchor=tk.E,
            style="PrefsValue.TLabel",
        ).grid(row=1, column=2, sticky=tk.E)

        sizing = self._section(pages["显示"], "尺寸限制", top_padding=18)
        ttk.Label(sizing, text="最大边长", style="Prefs.TLabel").grid(
            row=0, column=0, sticky=tk.W, pady=4
        )
        ttk.Spinbox(
            sizing,
            from_=320,
            to=20000,
            increment=100,
            textvariable=self.pin_max_size_var,
            width=9,
            justify=tk.CENTER,
        ).grid(row=0, column=1, sticky=tk.W, padx=(18, 6), pady=4)
        ttk.Label(sizing, text="像素", style="PrefsMuted.TLabel").grid(
            row=0, column=2, sticky=tk.W, pady=4
        )
        ttk.Label(sizing, text="快捷缩略图", style="Prefs.TLabel").grid(
            row=1, column=0, sticky=tk.W, pady=4
        )
        ttk.Spinbox(
            sizing,
            from_=40,
            to=600,
            textvariable=self.pin_thumbnail_width_var,
            width=7,
            justify=tk.CENTER,
        ).grid(row=1, column=1, sticky=tk.W, padx=(18, 6), pady=4)
        ttk.Label(sizing, text="×", style="PrefsMuted.TLabel").grid(
            row=1, column=2, sticky=tk.W, pady=4
        )
        ttk.Spinbox(
            sizing,
            from_=40,
            to=600,
            textvariable=self.pin_thumbnail_height_var,
            width=7,
            justify=tk.CENTER,
        ).grid(row=1, column=3, sticky=tk.W, padx=(6, 6), pady=4)
        ttk.Label(sizing, text="像素", style="PrefsMuted.TLabel").grid(
            row=1, column=4, sticky=tk.W, pady=4
        )

        restore = self._section(pages["关闭与恢复"], "恢复队列")
        ttk.Label(restore, text="最多可恢复", style="Prefs.TLabel").pack(side=tk.LEFT)
        ttk.Spinbox(
            restore,
            from_=1,
            to=20,
            textvariable=self.pin_restore_limit_var,
            width=8,
            justify=tk.CENTER,
        ).pack(side=tk.LEFT, padx=(10, 6))
        ttk.Label(
            restore,
            text="张已关闭贴图",
            style="PrefsMuted.TLabel",
        ).pack(side=tk.LEFT)

    def _build_output_tab(self, tab: ttk.Frame) -> None:
        pages = self._subtabs(tab, ("文件", "存储位置"))
        save_group = self._section(pages["文件"], "保存行为")
        ttk.Checkbutton(
            save_group,
            text="截图完成后自动保存",
            variable=self.auto_save_var,
            style="Prefs.TCheckbutton",
        ).grid(row=0, column=0, columnspan=3, sticky=tk.W)
        ttk.Label(save_group, text="图像格式", style="Prefs.TLabel").grid(
            row=1, column=0, sticky=tk.W, pady=(12, 4)
        )
        format_box = ttk.Combobox(
            save_group,
            textvariable=self.image_format_var,
            values=("PNG", "JPEG"),
            state="readonly",
            width=11,
        )
        format_box.grid(row=1, column=1, sticky=tk.W, padx=(18, 20), pady=(12, 4))
        ttk.Label(save_group, text="质量", style="Prefs.TLabel").grid(
            row=1, column=2, sticky=tk.W, pady=(12, 4)
        )
        self.image_quality_spin = ttk.Spinbox(
            save_group,
            from_=1,
            to=100,
            textvariable=self.image_quality_var,
            width=7,
            justify=tk.CENTER,
        )
        self.image_quality_spin.grid(
            row=1, column=3, sticky=tk.W, padx=(8, 0), pady=(12, 4)
        )
        self._update_output_preview()

        names = self._section(pages["文件"], "文件名规则", top_padding=18)
        names.columnconfigure(1, weight=1)
        ttk.Label(names, text="普通保存", style="Prefs.TLabel").grid(
            row=0, column=0, sticky=tk.W, pady=4
        )
        ttk.Entry(names, textvariable=self.filename_pattern_var).grid(
            row=0, column=1, sticky=tk.EW, padx=(14, 0), pady=4, ipady=3
        )
        ttk.Label(names, text="预览", style="PrefsMuted.TLabel").grid(
            row=1, column=0, sticky=tk.W, pady=(0, 8)
        )
        ttk.Label(
            names,
            textvariable=self.filename_preview_var,
            style="PrefsValue.TLabel",
        ).grid(row=1, column=1, sticky=tk.W, padx=(14, 0), pady=(0, 8))
        ttk.Label(names, text="快捷保存", style="Prefs.TLabel").grid(
            row=2, column=0, sticky=tk.W, pady=4
        )
        ttk.Entry(names, textvariable=self.quick_filename_pattern_var).grid(
            row=2, column=1, sticky=tk.EW, padx=(14, 0), pady=4, ipady=3
        )
        ttk.Label(names, text="预览", style="PrefsMuted.TLabel").grid(
            row=3, column=0, sticky=tk.W
        )
        ttk.Label(
            names,
            textvariable=self.quick_filename_preview_var,
            style="PrefsValue.TLabel",
        ).grid(row=3, column=1, sticky=tk.W, padx=(14, 0))

        paths = self._section(pages["存储位置"], "目录")
        paths.columnconfigure(1, weight=1)
        self._path_row(paths, 0, "截图目录", self.output_var)
        self._path_row(paths, 1, "快捷保存", self.quick_var)
        self._path_row(paths, 2, "历史目录", self.history_var)

    def _build_control_tab(self, tab: ttk.Frame) -> None:
        top = ttk.Frame(tab, style="PrefsPage.TFrame")
        top.pack(fill=tk.X, pady=(0, 9))
        ttk.Checkbutton(
            top,
            text="启用系统全局快捷键",
            variable=self.global_hotkeys_var,
            style="Prefs.TCheckbutton",
        ).pack(side=tk.LEFT)
        ttk.Label(
            top,
            textvariable=self.hotkey_status_var,
            style="PrefsMuted.TLabel",
        ).pack(side=tk.RIGHT)

        table_frame = ttk.Frame(tab, style="PrefsPage.TFrame")
        table_frame.pack(fill=tk.BOTH, expand=True)
        self.hotkey_table = ttk.Treeview(
            table_frame,
            columns=("action", "shortcut", "scope", "status"),
            show="headings",
            height=11,
            selectmode="browse",
            style="Prefs.Treeview",
        )
        self.hotkey_table.heading("action", text="操作")
        self.hotkey_table.heading("shortcut", text="快捷键")
        self.hotkey_table.heading("scope", text="范围")
        self.hotkey_table.heading("status", text="状态")
        self.hotkey_table.column("action", width=245, anchor=tk.W, stretch=True)
        self.hotkey_table.column("shortcut", width=175, anchor=tk.CENTER, stretch=False)
        self.hotkey_table.column("scope", width=70, anchor=tk.CENTER, stretch=False)
        self.hotkey_table.column("status", width=95, anchor=tk.CENTER, stretch=False)
        scrollbar = ttk.Scrollbar(
            table_frame,
            orient=tk.VERTICAL,
            command=self.hotkey_table.yview,
        )
        self.hotkey_table.configure(yscrollcommand=scrollbar.set)
        self.hotkey_table.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.hotkey_table.bind("<Double-Button-1>", self._edit_selected_hotkey)
        self.hotkey_table.tag_configure("ready", foreground="#16835A")
        self.hotkey_table.tag_configure("pending", foreground="#A15C00")
        self.hotkey_table.tag_configure("unavailable", foreground="#B23B47")

        actions = ttk.Frame(tab, style="PrefsPage.TFrame")
        actions.pack(fill=tk.X, pady=(9, 0))
        ttk.Button(
            actions,
            text="修改",
            style="Prefs.TButton",
            command=self._edit_selected_hotkey,
        ).pack(side=tk.LEFT)
        ttk.Button(
            actions,
            text="清除",
            style="Prefs.TButton",
            command=self._clear_selected_hotkey,
        ).pack(
            side=tk.LEFT,
            padx=(8, 0),
        )
        ttk.Button(
            actions,
            text="恢复默认快捷键",
            style="Prefs.TButton",
            command=self._restore_default_hotkeys,
        ).pack(side=tk.RIGHT)
        self._refresh_hotkey_table()

    def _refresh_hotkey_table(self, selected: Optional[str] = None) -> None:
        current = selected
        if current is None and self.hotkey_table.selection():
            current = self.hotkey_table.selection()[0]
        for item in self.hotkey_table.get_children():
            self.hotkey_table.delete(item)
        for definition in HOTKEY_ACTIONS:
            value = self.hotkey_values.get(definition.identifier, "")
            changed = value != self.original_hotkey_values.get(definition.identifier, "")
            status = (
                "待保存"
                if changed
                else self.hotkey_registration.get(
                    definition.identifier,
                    "窗口内" if not definition.global_hotkey else "未注册",
                )
            )
            if changed or "待保存" in status:
                row_tag = "pending"
            elif any(word in status for word in ("占用", "未注册", "关闭", "禁用")):
                row_tag = "unavailable"
            elif "已注册" in status or "窗口内" in status:
                row_tag = "ready"
            else:
                row_tag = ""
            self.hotkey_table.insert(
                "",
                tk.END,
                iid=definition.identifier,
                values=(
                    definition.label,
                    value or "未设置",
                    "全局" if definition.global_hotkey else "窗口内",
                    status,
                ),
                tags=(row_tag,) if row_tag else (),
            )
        target = current or HOTKEY_ACTIONS[0].identifier
        if self.hotkey_table.exists(target):
            self.hotkey_table.selection_set(target)
            self.hotkey_table.focus(target)
            self.hotkey_table.see(target)

    def _selected_hotkey_definition(self) -> Optional[HotkeyActionDefinition]:
        selection = self.hotkey_table.selection()
        if not selection:
            return None
        identifier = selection[0]
        return next(
            (
                definition
                for definition in HOTKEY_ACTIONS
                if definition.identifier == identifier
            ),
            None,
        )

    def _edit_selected_hotkey(self, event: Optional[tk.Event] = None) -> None:
        if event is not None:
            row = self.hotkey_table.identify_row(event.y)
            if row:
                self.hotkey_table.selection_set(row)
                self.hotkey_table.focus(row)
        definition = self._selected_hotkey_definition()
        if definition is None:
            return
        value = HotkeyCaptureDialog.ask(
            self,
            definition.label,
            self.hotkey_values.get(definition.identifier, ""),
            definition.global_hotkey,
        )
        if value is None:
            return
        proposed = dict(self.hotkey_values)
        proposed[definition.identifier] = value
        try:
            self.hotkey_values = HotkeyCodec.validate_mapping(proposed)
        except ValueError as exc:
            messagebox.showwarning("快捷键冲突", str(exc), parent=self)
            return
        self.hotkey_status_var.set("已修改，保存后生效")
        self._refresh_hotkey_table(definition.identifier)

    def _clear_selected_hotkey(self) -> None:
        definition = self._selected_hotkey_definition()
        if definition is None:
            return
        self.hotkey_values[definition.identifier] = ""
        self.hotkey_status_var.set("已清除，保存后生效")
        self._refresh_hotkey_table(definition.identifier)

    def _restore_default_hotkeys(self) -> None:
        self.hotkey_values = default_hotkey_map()
        self.hotkey_status_var.set("已恢复默认快捷键")
        self._refresh_hotkey_table()

    def _build_about_tab(self, tab: ttk.Frame) -> None:
        header = ttk.Frame(tab, style="PrefsPage.TFrame")
        header.pack(fill=tk.X, pady=(10, 22))
        self.about_icon = ImageTk.PhotoImage(
            TrayIconFactory.make(72, self.accent_color_var.get())
        )
        ttk.Label(
            header,
            image=self.about_icon,
            style="Prefs.TLabel",
        ).pack(side=tk.LEFT, padx=(8, 18))
        title = ttk.Frame(header, style="PrefsPage.TFrame")
        title.pack(side=tk.LEFT, fill=tk.Y)
        ttk.Label(
            title,
            text="ScreenshotTool",
            style="PrefsTitle.TLabel",
        ).pack(anchor=tk.W, pady=(6, 2))
        ttk.Label(
            title,
            text="本地桌面截图与贴图工具",
            style="PrefsMuted.TLabel",
        ).pack(anchor=tk.W)

        details = self._section(tab, "应用信息")
        ttk.Label(details, text="版本", style="Prefs.TLabel").grid(
            row=0, column=0, sticky=tk.W, pady=5
        )
        ttk.Label(details, text="1.2.0", style="PrefsValue.TLabel").grid(
            row=0, column=1, sticky=tk.W, padx=(30, 0), pady=5
        )
        ttk.Label(details, text="运行环境", style="Prefs.TLabel").grid(
            row=1, column=0, sticky=tk.W, pady=5
        )
        ttk.Label(
            details,
            text=f"Python {platform.python_version()} · Tkinter · Pillow",
            style="PrefsMuted.TLabel",
        ).grid(
            row=1,
            column=1,
            sticky=tk.W,
            padx=(30, 0),
            pady=5,
        )
        ttk.Label(details, text="数据处理", style="Prefs.TLabel").grid(
            row=2, column=0, sticky=tk.W, pady=5
        )
        ttk.Label(
            details,
            text="截图、配置与历史记录均保存在本机",
            style="PrefsMuted.TLabel",
        ).grid(
            row=2,
            column=1,
            sticky=tk.W,
            padx=(30, 0),
            pady=5,
        )

        support = self._section(tab, "支持", top_padding=20)
        ttk.Button(
            support,
            text="打开使用说明",
            style="Prefs.TButton",
            command=lambda: self._open_path(Path("README.md").resolve()),
        ).pack(side=tk.LEFT)
        ttk.Button(
            support,
            text="打开程序目录",
            style="Prefs.TButton",
            command=lambda: self._open_path(Path.cwd()),
        ).pack(side=tk.LEFT, padx=(8, 0))

    def _build_accent_swatches(self, parent: ttk.Frame) -> None:
        for color in AppTheme.ACCENTS:
            canvas = tk.Canvas(
                parent,
                width=30,
                height=30,
                bg=self.SURFACE,
                highlightthickness=0,
                cursor="hand2",
            )
            canvas.pack(side=tk.LEFT, padx=(0, 7))
            canvas.bind(
                "<Button-1>",
                lambda event, selected=color: self._select_accent(selected),
            )
            self.accent_swatches[color] = canvas
        self._render_accent_swatches()

    def _select_accent(self, color: str) -> None:
        accent = AppTheme.normalize_accent(color)
        self.accent_color_var.set(accent)
        self.accent_value_var.set(accent)
        self._configure_preferences_styles(accent)
        self._render_accent_swatches()

    def _render_accent_swatches(self) -> None:
        selected = AppTheme.normalize_accent(self.accent_color_var.get())
        for color, canvas in self.accent_swatches.items():
            canvas.delete("all")
            is_selected = color == selected
            canvas.create_rectangle(
                3,
                3,
                27,
                27,
                fill=color,
                outline="#18212A" if is_selected else self.BORDER,
                width=2 if is_selected else 1,
            )
            if is_selected:
                canvas.create_line(
                    9,
                    15,
                    13,
                    19,
                    21,
                    10,
                    fill="#FFFFFF",
                    width=2,
                    capstyle=tk.ROUND,
                    joinstyle=tk.ROUND,
                )

    def _path_row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
    ) -> None:
        ttk.Label(parent, text=label, style="Prefs.TLabel").grid(
            row=row, column=0, sticky=tk.W, pady=6
        )
        ttk.Entry(parent, textvariable=variable).grid(
            row=row,
            column=1,
            sticky=tk.EW,
            pady=6,
            padx=(14, 10),
            ipady=3,
        )
        actions = ttk.Frame(parent, style="PrefsSection.TFrame")
        actions.grid(row=row, column=2, sticky=tk.W, pady=6)
        ttk.Button(
            actions,
            text="打开",
            style="Prefs.TButton",
            width=6,
            command=lambda: self._open_variable_dir(variable),
        ).pack(side=tk.LEFT)
        ttk.Button(
            actions,
            text="更改",
            style="Prefs.TButton",
            width=6,
            command=lambda: self._choose_dir(variable),
        ).pack(side=tk.LEFT, padx=(6, 0))

    def _choose_dir(self, variable: tk.StringVar) -> None:
        path = filedialog.askdirectory(parent=self, initialdir=variable.get() or None)
        if path:
            variable.set(path)

    def _update_opacity_label(self) -> None:
        self.pin_opacity_label_var.set(f"{int(round(self.pin_opacity_var.get()))}%")

    def _update_mask_opacity_label(self) -> None:
        self.mask_opacity_label_var.set(
            f"{int(round(self.mask_opacity_var.get()))}%"
        )

    def _update_magnifier_zoom_label(self) -> None:
        index = max(
            0,
            min(
                len(CaptureMagnifier.ZOOM_SAMPLES) - 1,
                int(round(self.magnifier_zoom_var.get())),
            ),
        )
        columns, _ = CaptureMagnifier.ZOOM_SAMPLES[index]
        ratio = CaptureMagnifier.PREVIEW_WIDTH / max(1, columns)
        self.magnifier_zoom_label_var.set(f"{ratio:.0f}x")

    def _update_output_preview(self) -> None:
        image_format = OutputNamePolicy.normalize_format(self.image_format_var.get())
        extension = OutputNamePolicy.FORMAT_EXTENSIONS[image_format]
        self.filename_preview_var.set(
            f"{OutputNamePolicy.render_stem(self.filename_pattern_var.get())}{extension}"
        )
        self.quick_filename_preview_var.set(
            f"{OutputNamePolicy.render_stem(self.quick_filename_pattern_var.get())}{extension}"
        )
        if hasattr(self, "image_quality_spin"):
            self.image_quality_spin.configure(
                state="normal" if image_format == "JPEG" else "disabled"
            )

    def _open_variable_dir(self, variable: tk.StringVar) -> None:
        path = Path(variable.get().strip() or ".").expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("目录不可用", str(exc), parent=self)
            return
        self._open_path(path.resolve())

    def _open_config_folder(self) -> None:
        self._open_path(ConfigStore.CONFIG_FILE.resolve().parent)

    def _open_config_file(self) -> None:
        path = ConfigStore.CONFIG_FILE.resolve()
        if not path.exists():
            messagebox.showinfo("配置文件", "保存一次首选项后会生成配置文件。", parent=self)
            return
        self._open_path(path)

    def _open_path(self, path: Path) -> None:
        try:
            if platform.system() == "Windows":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc), parent=self)

    def _restore_defaults(self) -> None:
        if not messagebox.askyesno(
            "恢复默认",
            "将所有页面中的设置恢复为默认值？",
            parent=self,
        ):
            return
        self._set_values(PersistedAppConfig())

    def _set_values(self, config: PersistedAppConfig) -> None:
        self.output_var.set(config.output_dir)
        self.quick_var.set(config.quick_save_dir)
        self.history_var.set(config.history_dir)
        self.max_history_var.set(config.max_history)
        self.auto_save_var.set(config.auto_save)
        self.image_format_var.set(config.image_format)
        self.image_quality_var.set(config.image_quality)
        self.filename_pattern_var.set(config.filename_pattern)
        self.quick_filename_pattern_var.set(config.quick_filename_pattern)
        self.color_format_var.set(config.color_format)
        self.accent_color_var.set(config.accent_color)
        self.accent_value_var.set(config.accent_color.upper())
        self.smart_selection_var.set(config.smart_selection)
        self.capture_delay_var.set(config.capture_delay)
        self.mask_opacity_var.set(config.capture_mask_opacity)
        self.border_width_var.set(config.capture_border_width)
        self.show_handles_var.set(config.capture_show_handles)
        self.show_crosshair_var.set(config.capture_show_crosshair)
        self.show_magnifier_var.set(config.capture_show_magnifier)
        self.magnifier_zoom_var.set(config.magnifier_zoom_index)
        self.global_hotkeys_var.set(config.global_hotkeys)
        self.close_to_tray_var.set(config.close_to_tray)
        self.start_in_tray_var.set(config.start_in_tray)
        self.launch_at_startup_var.set(config.launch_at_startup)
        self.show_main_after_capture_var.set(config.show_main_after_capture)
        self.pin_opacity_var.set(config.pin_default_opacity)
        self.pin_always_on_top_var.set(config.pin_always_on_top)
        self.pin_restore_limit_var.set(config.pin_restore_limit)
        self.pin_max_size_var.set(config.pin_max_size)
        self.pin_thumbnail_width_var.set(config.pin_thumbnail_width)
        self.pin_thumbnail_height_var.set(config.pin_thumbnail_height)
        self.hotkey_values = HotkeyCodec.merge_with_defaults(config.hotkeys)
        self.hotkey_status_var.set("已恢复默认设置")
        self._refresh_hotkey_table()
        self._update_opacity_label()
        self._update_mask_opacity_label()
        self._update_magnifier_zoom_label()
        self._update_output_preview()
        self._configure_preferences_styles(config.accent_color)
        self._render_accent_swatches()

    def _accept(self) -> None:
        try:
            max_history = max(1, min(500, int(self.max_history_var.get())))
            pin_restore_limit = max(
                1,
                min(20, int(self.pin_restore_limit_var.get())),
            )
            pin_opacity = max(15, min(100, int(round(self.pin_opacity_var.get()))))
            image_quality = max(1, min(100, int(self.image_quality_var.get())))
            capture_delay = max(0, min(10, int(self.capture_delay_var.get())))
            mask_opacity = max(
                20,
                min(85, int(round(self.mask_opacity_var.get()))),
            )
            border_width = max(1, min(6, int(self.border_width_var.get())))
            magnifier_zoom_index = max(
                0,
                min(
                    len(CaptureMagnifier.ZOOM_SAMPLES) - 1,
                    int(round(self.magnifier_zoom_var.get())),
                ),
            )
            pin_max_size = max(320, min(20000, int(self.pin_max_size_var.get())))
            pin_thumbnail_width = max(
                40,
                min(600, int(self.pin_thumbnail_width_var.get())),
            )
            pin_thumbnail_height = max(
                40,
                min(600, int(self.pin_thumbnail_height_var.get())),
            )
        except (tk.TclError, ValueError):
            messagebox.showwarning(
                "设置无效",
                "请检查数量、透明度、延迟、边框和图像质量。",
                parent=self,
            )
            return
        try:
            hotkeys = HotkeyCodec.validate_mapping(self.hotkey_values)
        except ValueError as exc:
            messagebox.showwarning("快捷键无效", str(exc), parent=self)
            self.notebook.select(self.tabs["控制"])
            return

        self.result = PersistedAppConfig(
            output_dir=self.output_var.get().strip() or "screenshots",
            quick_save_dir=self.quick_var.get().strip() or "quick-save",
            history_dir=self.history_var.get().strip() or "history",
            auto_save=bool(self.auto_save_var.get()),
            image_format=OutputNamePolicy.normalize_format(
                self.image_format_var.get()
            ),
            image_quality=image_quality,
            filename_pattern=(
                self.filename_pattern_var.get().strip()
                or OutputNamePolicy.DEFAULT_PATTERN
            ),
            quick_filename_pattern=(
                self.quick_filename_pattern_var.get().strip()
                or OutputNamePolicy.DEFAULT_QUICK_PATTERN
            ),
            max_history=max_history,
            color_format=(
                self.color_format_var.get()
                if self.color_format_var.get() in {"hex", "rgb"}
                else "hex"
            ),
            accent_color=AppTheme.normalize_accent(self.accent_color_var.get()),
            smart_selection=bool(self.smart_selection_var.get()),
            capture_delay=capture_delay,
            capture_mask_opacity=mask_opacity,
            capture_border_width=border_width,
            capture_show_handles=bool(self.show_handles_var.get()),
            capture_show_crosshair=bool(self.show_crosshair_var.get()),
            capture_show_magnifier=bool(self.show_magnifier_var.get()),
            magnifier_zoom_index=magnifier_zoom_index,
            global_hotkeys=bool(self.global_hotkeys_var.get()),
            close_to_tray=bool(self.close_to_tray_var.get()),
            start_in_tray=bool(self.start_in_tray_var.get()),
            show_main_after_capture=bool(self.show_main_after_capture_var.get()),
            launch_at_startup=bool(self.launch_at_startup_var.get()),
            pin_default_opacity=pin_opacity,
            pin_always_on_top=bool(self.pin_always_on_top_var.get()),
            pin_restore_limit=pin_restore_limit,
            pin_max_size=pin_max_size,
            pin_thumbnail_width=pin_thumbnail_width,
            pin_thumbnail_height=pin_thumbnail_height,
            hotkeys=hotkeys,
        )
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()

    def _center(self, parent: tk.Tk) -> None:
        self.update_idletasks()
        desktop = VirtualDesktopGeometry.detect()
        window_width = min(self.WIDTH, max(640, desktop.width - 32))
        window_height = min(self.HEIGHT, max(500, desktop.height - 56))
        self.minsize(
            min(700, window_width),
            min(520, window_height),
        )
        if parent.state() == "withdrawn":
            x = parent.winfo_pointerx() - window_width // 2
            y = parent.winfo_pointery() - window_height // 2
        else:
            parent_x = parent.winfo_rootx()
            parent_y = parent.winfo_rooty()
            parent_width = max(1, parent.winfo_width())
            parent_height = max(1, parent.winfo_height())
            x = parent_x + (parent_width - window_width) // 2
            y = parent_y + (parent_height - window_height) // 2
        x = max(desktop.left, min(x, desktop.left + desktop.width - window_width))
        y = max(desktop.top, min(y, desktop.top + desktop.height - window_height))
        self.geometry(f"{window_width}x{window_height}{x:+d}{y:+d}")


class ScreenshotApp:
    """Tkinter 桌面端截图工具主界面。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.app_config = ConfigStore.load()
        if self.app_config.start_in_tray and platform.system() == "Windows":
            self.root.withdraw()
        self.settings = ScreenshotSettings(
            output_dir=Path(self.app_config.output_dir),
            auto_save=self.app_config.auto_save,
            image_format=self.app_config.image_format,
            image_quality=self.app_config.image_quality,
            filename_pattern=self.app_config.filename_pattern,
        )
        self.manager = ScreenshotManager(self.settings)
        self.history = ScreenshotHistoryManager(
            Path(self.app_config.history_dir),
            self.app_config.max_history,
        )

        self.last_image: Optional[Image.Image] = None
        self.last_file: Optional[Path] = None
        self.last_capture_position: Optional[Point] = None
        self.preview_photo: Optional[ImageTk.PhotoImage] = None
        self.preview_resize_job: Optional[str] = None
        self.pending_screen: Optional[Image.Image] = None
        self.long_controller: Optional[LongScreenshotController] = None
        self.long_result_dialog: Optional[LongScreenshotResultDialog] = None
        self.pending_long_output: Optional[LongCaptureOutput] = None
        self.ocr_backend = WindowsOcrBackend()
        self.ocr_dialog: Optional[OcrResultDialog] = None
        self.preferences_dialog: Optional[PreferencesDialog] = None
        self.preferences_hidden_for_capture = False
        self.global_hotkey_manager: Optional[WindowsGlobalHotkeyManager] = None
        self.tray_controller: Optional[SystemTrayController] = None
        self._shortcut_last_run: dict[str, float] = {}
        self.local_hotkey_sequences: set[str] = set()
        self.capture_return_state: Optional[str] = None
        self.is_closing = False

        self.pin_manager = PinManager(
            self.root,
            copy_func=self.manager.copy_to_clipboard,
            save_func=self._save_pinned_image,
            status_func=self._set_status,
            default_opacity=self.app_config.pin_default_opacity / 100,
            always_on_top=self.app_config.pin_always_on_top,
            restore_limit=self.app_config.pin_restore_limit,
            max_window_size=self.app_config.pin_max_size,
            thumbnail_size=(
                self.app_config.pin_thumbnail_width,
                self.app_config.pin_thumbnail_height,
            ),
            ocr_func=self.extract_text_from_image,
        )

        self.auto_save_var = tk.BooleanVar(value=self.settings.auto_save)
        self.delay_var = tk.IntVar(value=self.app_config.capture_delay)
        self.status_var = tk.StringVar(value="准备就绪")
        self.info_var = tk.StringVar(value="还没有截图")
        self.output_dir_var = tk.StringVar(value=f"保存目录：{self.settings.output_dir}")
        self.app_icon: Optional[ImageTk.PhotoImage] = None

        self._configure_window()
        self._build_ui()
        self._bind_shortcuts()
        self._configure_global_hotkeys()
        self._configure_tray()
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)
        self._apply_startup_visibility()

    def _configure_window(self) -> None:
        self.root.title("轻量截图工具")
        self.app_icon = ImageTk.PhotoImage(
            TrayIconFactory.make(64, self.app_config.accent_color)
        )
        self.root.iconphoto(True, self.app_icon)
        self.root.geometry("1040x700")
        self.root.minsize(900, 680)
        self.root.configure(bg="#f4f6f8")

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("TFrame", background="#f4f6f8")
        style.configure("Panel.TFrame", background="#ffffff")
        style.configure("TLabel", background="#f4f6f8", foreground="#1f2933")
        style.configure("Panel.TLabel", background="#ffffff", foreground="#1f2933")
        style.configure("Muted.TLabel", background="#ffffff", foreground="#637083")
        style.configure("Status.TLabel", background="#e9eef3", foreground="#324256")
        style.configure("TButton", padding=(12, 7))
        style.configure(
            "Primary.TButton",
            padding=(14, 9),
            foreground="#ffffff",
        )
        self._apply_accent_styles(style)
        style.configure("Capture.TButton", padding=(12, 9))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TCheckbutton", background="#f4f6f8", foreground="#1f2933")
        style.configure("TNotebook", background="#f4f6f8", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(13, 7))
        style.configure("TLabelframe", background="#f4f6f8")
        style.configure("TLabelframe.Label", background="#f4f6f8", foreground="#243442")
        style.configure("Treeview", rowheight=28)

    def _apply_accent_styles(self, style: Optional[ttk.Style] = None) -> None:
        style = style or ttk.Style()
        accent = AppTheme.normalize_accent(self.app_config.accent_color)
        hover = AppTheme.blend(accent, "#000000", 0.12)
        pressed = AppTheme.blend(accent, "#000000", 0.24)
        style.configure("Primary.TButton", background=accent, foreground="#ffffff")
        style.map(
            "Primary.TButton",
            background=[("active", hover), ("pressed", pressed)],
            foreground=[("disabled", "#d6e7ed")],
        )
        self.app_icon = ImageTk.PhotoImage(TrayIconFactory.make(64, accent))
        self.root.iconphoto(True, self.app_icon)

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=16)
        container.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(container)
        header.pack(fill=tk.X)
        title_area = ttk.Frame(header)
        title_area.pack(side=tk.LEFT)
        ttk.Label(title_area, text="ScreenshotTool", style="Title.TLabel").pack(
            anchor=tk.W
        )
        ttk.Label(
            title_area,
            textvariable=self.info_var,
            foreground="#617282",
        ).pack(anchor=tk.W, pady=(2, 0))

        capture_options = ttk.Frame(header)
        capture_options.pack(side=tk.RIGHT, pady=(4, 0))
        ttk.Label(capture_options, text="延迟").pack(side=tk.LEFT, padx=(0, 6))
        delay_spin = ttk.Spinbox(
            capture_options,
            from_=0,
            to=10,
            textvariable=self.delay_var,
            width=4,
            justify=tk.CENTER,
        )
        delay_spin.pack(side=tk.LEFT)
        delay_spin.bind("<FocusOut>", lambda event: self._sync_settings())
        delay_spin.bind("<Return>", lambda event: self._sync_settings())
        ttk.Label(capture_options, text="秒").pack(side=tk.LEFT, padx=(6, 16))
        ttk.Checkbutton(
            capture_options,
            text="自动保存",
            variable=self.auto_save_var,
            command=self._sync_settings,
        ).pack(side=tk.LEFT)

        capture_group = ttk.LabelFrame(container, text="截图", padding=10)
        capture_group.pack(fill=tk.X, pady=(14, 12))
        capture_actions = (
            ("全屏截图", self.capture_full_screen, "Capture.TButton"),
            ("活动窗口", self.capture_active_window, "Capture.TButton"),
            ("框选区域", self.capture_region, "Primary.TButton"),
            ("长截图", self.capture_long_screenshot, "Capture.TButton"),
            ("取色", self.pick_color, "Capture.TButton"),
            ("白板", self.open_whiteboard, "Capture.TButton"),
        )
        for column, (label, command, style_name) in enumerate(capture_actions):
            capture_group.columnconfigure(column, weight=1, uniform="capture")
            ttk.Button(
                capture_group,
                text=label,
                style=style_name,
                command=command,
            ).grid(
                row=0,
                column=column,
                sticky=tk.EW,
                padx=(0 if column == 0 else 4, 0 if column == 5 else 4),
            )

        content = ttk.Frame(container)
        content.pack(fill=tk.BOTH, expand=True)
        content.columnconfigure(0, weight=1)
        content.columnconfigure(2, minsize=238)
        content.rowconfigure(0, weight=1)

        preview_area = ttk.Frame(content)
        preview_area.grid(row=0, column=0, sticky=tk.NSEW)
        preview_header = ttk.Frame(preview_area)
        preview_header.pack(fill=tk.X, pady=(0, 7))
        ttk.Label(preview_header, text="预览", style="Section.TLabel").pack(side=tk.LEFT)
        ttk.Label(
            preview_header,
            textvariable=self.output_dir_var,
            foreground="#617282",
            wraplength=390,
            justify=tk.RIGHT,
        ).pack(side=tk.RIGHT)

        self.preview_canvas = tk.Canvas(
            preview_area,
            bg="#ffffff",
            highlightthickness=1,
            highlightbackground="#cbd5df",
        )
        self.preview_canvas.pack(fill=tk.BOTH, expand=True)
        self.preview_canvas.create_text(
            320,
            230,
            text="暂无截图",
            fill="#748391",
            font=("Microsoft YaHei UI", 13),
            tags=("placeholder",),
        )
        self.preview_canvas.bind("<Configure>", self._schedule_preview_render)

        ttk.Separator(content, orient=tk.VERTICAL).grid(
            row=0,
            column=1,
            sticky=tk.NS,
            padx=14,
        )
        sidebar = ttk.Frame(content)
        sidebar.grid(row=0, column=2, sticky=tk.NSEW)

        ttk.Label(sidebar, text="当前图片", style="Section.TLabel").pack(
            anchor=tk.W,
            pady=(0, 7),
        )
        current_actions = ttk.Frame(sidebar)
        current_actions.pack(fill=tk.X)
        for column in range(2):
            current_actions.columnconfigure(column, weight=1, uniform="current")
        for index, (label, command) in enumerate(
            (
                ("复制", self.copy_last_image),
                ("保存为", self.save_as),
                ("贴到屏幕", self.pin_last_image),
                ("快捷保存", self.quick_save_last_image),
                ("提取文字", self.extract_text_from_current),
            )
        ):
            is_full_width = index == 4
            ttk.Button(current_actions, text=label, command=command).grid(
                row=index // 2,
                column=index % 2,
                columnspan=2 if is_full_width else 1,
                sticky=tk.EW,
                padx=(0, 0)
                if is_full_width
                else ((0, 4) if index % 2 == 0 else (4, 0)),
                pady=(0, 8),
            )

        ttk.Separator(sidebar, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(5, 12))
        ttk.Label(sidebar, text="贴图", style="Section.TLabel").pack(
            anchor=tk.W,
            pady=(0, 7),
        )
        ttk.Button(
            sidebar,
            text="从剪贴板贴图",
            command=self.paste_from_clipboard,
        ).pack(fill=tk.X, pady=(0, 8))
        pin_actions = ttk.Frame(sidebar)
        pin_actions.pack(fill=tk.X)
        for column in range(2):
            pin_actions.columnconfigure(column, weight=1, uniform="pin")
        for index, (label, command) in enumerate(
            (
                ("显示/隐藏", self.toggle_pinned_images),
                ("恢复关闭", self.restore_closed_pin),
                ("销毁全部", self.pin_manager.destroy_all),
            )
        ):
            is_full_width = index == 2
            ttk.Button(pin_actions, text=label, command=command).grid(
                row=index // 2,
                column=index % 2,
                columnspan=2 if is_full_width else 1,
                sticky=tk.EW,
                padx=(0, 0)
                if is_full_width
                else ((0, 4) if index % 2 == 0 else (4, 0)),
                pady=(0, 8),
            )

        ttk.Separator(sidebar, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(5, 12))
        ttk.Label(sidebar, text="管理", style="Section.TLabel").pack(
            anchor=tk.W,
            pady=(0, 7),
        )
        management = ttk.Frame(sidebar)
        management.pack(fill=tk.X)
        for column in range(2):
            management.columnconfigure(column, weight=1, uniform="manage")
        ttk.Button(management, text="历史", command=self.open_history).grid(
            row=0,
            column=0,
            sticky=tk.EW,
            padx=(0, 4),
        )
        ttk.Button(management, text="首选项", command=self.open_preferences).grid(
            row=0,
            column=1,
            sticky=tk.EW,
            padx=(4, 0),
        )
        ttk.Button(
            sidebar,
            text="打开保存目录",
            command=self.open_output_folder,
        ).pack(fill=tk.X, pady=(8, 0))

        ttk.Label(
            container,
            textvariable=self.status_var,
            style="Status.TLabel",
            padding=(10, 6),
            anchor=tk.W,
        ).pack(fill=tk.X, pady=(12, 0))

    def _bind_shortcuts(self) -> None:
        for sequence in self.local_hotkey_sequences:
            self.root.unbind(sequence)
        self.local_hotkey_sequences.clear()
        actions = self._shortcut_actions()
        for definition in HOTKEY_ACTIONS:
            value = self.app_config.hotkeys.get(definition.identifier, "")
            try:
                sequence = HotkeyCodec.to_tk_sequence(value)
            except ValueError:
                continue
            callback = actions.get(definition.identifier)
            if not sequence or callback is None:
                continue
            self.root.bind(
                sequence,
                lambda event, action_id=definition.identifier, func=callback: self._invoke_shortcut(
                    action_id,
                    func,
                ),
            )
            self.local_hotkey_sequences.add(sequence)

    def _shortcut_actions(self) -> dict[str, Callable[[], None]]:
        return {
            "region": self.capture_region,
            "full": self.capture_full_screen,
            "active": self.capture_active_window,
            "long": self.capture_long_screenshot,
            "color": self.pick_color,
            "whiteboard": self.open_whiteboard,
            "paste": self.paste_from_clipboard,
            "toggle_pins": self.toggle_pinned_images,
            "pin_current": self.pin_last_image,
            "quick_save": self.quick_save_last_image,
            "preferences": self.open_preferences,
            "save_as": self.save_as,
            "copy_current": self.copy_last_image,
            "ocr_current": self.extract_text_from_current,
        }

    def _configure_global_hotkeys(self) -> None:
        if self.global_hotkey_manager:
            self.global_hotkey_manager.stop()
            self.global_hotkey_manager = None

        if not self.app_config.global_hotkeys or platform.system() != "Windows":
            return

        actions = self._shortcut_actions()
        bindings: List[GlobalHotkeyBinding] = []
        for index, definition in enumerate(HOTKEY_ACTIONS, start=101):
            if not definition.global_hotkey:
                continue
            value = self.app_config.hotkeys.get(definition.identifier, "")
            if not value:
                continue
            try:
                modifiers, virtual_key = HotkeyCodec.to_windows(value)
            except ValueError:
                continue
            callback = actions.get(definition.identifier)
            if callback is None:
                continue
            bindings.append(
                GlobalHotkeyBinding(
                    index,
                    modifiers,
                    virtual_key,
                    definition.label,
                    lambda action_id=definition.identifier, func=callback: self._invoke_shortcut(
                        action_id,
                        func,
                    ),
                )
            )
        if not bindings:
            self._set_status("没有启用的全局快捷键")
            return
        self.global_hotkey_manager = WindowsGlobalHotkeyManager(
            self.root,
            bindings,
            self._set_status,
        )
        self.global_hotkey_manager.start()
        self._set_status(self.global_hotkey_manager.summary())

    def _hotkey_registration_statuses(self) -> dict[str, str]:
        statuses: dict[str, str] = {}
        active_ids = (
            self.global_hotkey_manager.active_ids
            if self.global_hotkey_manager
            else set()
        )
        failed_names = set(
            self.global_hotkey_manager.failed_names
            if self.global_hotkey_manager
            else []
        )
        for index, definition in enumerate(HOTKEY_ACTIONS, start=101):
            value = self.app_config.hotkeys.get(definition.identifier, "")
            if not value:
                statuses[definition.identifier] = "未设置"
            elif not definition.global_hotkey:
                statuses[definition.identifier] = "窗口内"
            elif not self.app_config.global_hotkeys:
                statuses[definition.identifier] = "已关闭"
            elif index in active_ids:
                statuses[definition.identifier] = "已注册"
            elif definition.label in failed_names:
                statuses[definition.identifier] = "被占用"
            else:
                statuses[definition.identifier] = "未注册"
        return statuses

    def _configure_tray(self) -> None:
        if self.tray_controller:
            self.tray_controller.stop()
            self.tray_controller = None
        if platform.system() != "Windows":
            return

        self.tray_controller = SystemTrayController(
            self.root,
            {
                "capture": lambda: self._invoke_shortcut("region", self.capture_region),
                "paste": lambda: self._invoke_shortcut("paste", self.paste_from_clipboard),
                "toggle_pins": lambda: self._invoke_shortcut("toggle-pins", self.toggle_pinned_images),
                "history": self._open_history_from_tray,
                "preferences": self._open_preferences_from_tray,
                "show": self._show_main_window,
                "quit": self._close_app,
            },
            self._set_status,
            self.app_config.accent_color,
        )
        if not self.tray_controller.start():
            self.tray_controller = None

    def _tray_available(self) -> bool:
        return bool(self.tray_controller and self.tray_controller.running)

    def _apply_startup_visibility(self) -> None:
        state = MainWindowVisibilityPolicy.startup_state(
            self.app_config,
            self._tray_available(),
        )
        self._apply_main_window_state(state, focus=state == "normal")
        if state == "withdrawn":
            self._set_status("正在系统托盘后台运行")

    def _show_main_window(self) -> None:
        self._apply_main_window_state("normal", focus=True)

    def _apply_main_window_state(self, state: str, focus: bool = False) -> None:
        if state == "withdrawn":
            self.root.withdraw()
            return
        self.root.deiconify()
        if state == "iconic":
            self.root.iconify()
            return
        if focus:
            self.root.lift()
            self.root.focus_force()

    def _open_history_from_tray(self) -> None:
        self._show_main_window()
        self.open_history()

    def _open_preferences_from_tray(self) -> None:
        self.open_preferences()

    def _invoke_shortcut(
        self,
        name: str,
        callback: Callable[[], None],
    ) -> str:
        now = time.monotonic()
        if now - self._shortcut_last_run.get(name, 0.0) < 0.35:
            return "break"

        try:
            grab_owner = self.root.grab_current()
        except tk.TclError:
            return "break"
        if grab_owner is not None and grab_owner is not self.root:
            return "break"

        self._shortcut_last_run[name] = now
        callback()
        return "break"

    def _on_window_close(self) -> None:
        if (
            self.app_config.close_to_tray
            and self.tray_controller
            and self.tray_controller.running
        ):
            self.root.withdraw()
            self._set_status("主窗口已隐藏到系统托盘")
            return
        self._close_app()

    def _close_app(self) -> None:
        if self.is_closing:
            return
        self.is_closing = True
        if self.tray_controller:
            self.tray_controller.stop()
            self.tray_controller = None
        if self.global_hotkey_manager:
            self.global_hotkey_manager.stop()
            self.global_hotkey_manager = None
        self.root.destroy()

    def capture_full_screen(self) -> None:
        self._run_capture_after_delay(self._capture_full_screen_now)

    def capture_active_window(self) -> None:
        self._run_capture_after_delay(self._capture_active_window_now)

    def capture_region(self) -> None:
        self._run_capture_after_delay(self._start_region_capture)

    def capture_long_screenshot(self) -> None:
        if self.long_controller and self.long_controller.running:
            self.long_controller.stop()
            return
        if platform.system() != "Windows":
            self._show_warning("长截图目前仅支持 Windows。")
            return

        settings = LongScreenshotOptionsDialog.ask(self.root)
        if settings is None:
            self._set_status("已取消长截图")
            return

        self._run_capture_after_delay(
            lambda: self._start_long_region_capture(settings)
        )

    def pick_color(self) -> None:
        self._run_capture_after_delay(self._start_color_picker)

    def open_whiteboard(self, color: str = "#ffffff") -> None:
        self._begin_capture_visibility()
        self.root.after(200, lambda: self._start_whiteboard(color))

    def paste_from_clipboard(self) -> None:
        self.pin_manager.paste_from_clipboard()

    def pin_last_image(self) -> None:
        if self.last_image is None:
            self._show_warning("请先截图或从历史加载图片，再贴图。")
            return
        self.pin_manager.pin_image(
            self.last_image,
            "当前截图贴图",
            position=self.last_capture_position,
        )

    def extract_text_from_current(self) -> None:
        if self.last_image is None:
            self._show_warning("请先截图或从历史加载图片，再提取文字。")
            return
        self.extract_text_from_image(self.last_image, "当前图片")

    def extract_text_from_image(
        self,
        image: Image.Image,
        title: str = "图片文字",
    ) -> None:
        if platform.system() != "Windows":
            self._show_warning("图片文字识别目前仅支持 Windows。")
            return
        if self.ocr_dialog and self.ocr_dialog.winfo_exists():
            self.ocr_dialog.close()
        self._set_status("正在提取图片文字...")
        self.ocr_dialog = OcrResultDialog(
            self.root,
            image.copy(),
            backend=self.ocr_backend,
            title=title,
            on_close=self._on_ocr_dialog_close,
            status_func=self._set_status,
        )

    def _on_ocr_dialog_close(self) -> None:
        self.ocr_dialog = None

    def toggle_pinned_images(self) -> None:
        self.pin_manager.toggle_all()

    def restore_closed_pin(self) -> None:
        self.pin_manager.restore_last_closed()

    def quick_save_last_image(self) -> None:
        if self.last_image is None:
            self._show_warning("请先截图，再快捷保存。")
            return

        quick_dir = Path(self.app_config.quick_save_dir)
        quick_dir.mkdir(parents=True, exist_ok=True)
        target = OutputNamePolicy.next_target(
            quick_dir,
            self.app_config.quick_filename_pattern,
            self.settings.image_format,
        )
        try:
            self.manager.save(self.last_image, target)
            self._set_status(f"已快捷保存：{target}")
        except Exception as exc:
            self._show_error(f"快捷保存失败：{exc}")

    def open_history(self) -> None:
        HistoryDialog(
            self.root,
            self.history,
            on_load=self._load_history_image,
            on_pin=lambda image: self.pin_manager.pin_image(image, "历史贴图"),
            on_copy=self._copy_image_direct,
            on_clear=lambda: self._set_status("截图历史已清空"),
            on_ocr=lambda image: self.extract_text_from_image(image, "历史图片"),
        )

    def open_preferences(self) -> None:
        if self.is_closing:
            return
        if self.preferences_dialog and self.preferences_dialog.winfo_exists():
            self.preferences_dialog.deiconify()
            self.preferences_dialog.lift()
            self.preferences_dialog.focus_force()
            return

        previous_state = self.root.state()
        self.root.withdraw()
        hotkey_status = (
            self.global_hotkey_manager.summary()
            if self.global_hotkey_manager
            else "全局快捷键已关闭"
        )
        dialog: Optional[PreferencesDialog] = None
        new_config: Optional[PersistedAppConfig] = None
        try:
            dialog = PreferencesDialog(
                self.root,
                self.app_config,
                hotkey_status,
                self._hotkey_registration_statuses(),
            )
            self.preferences_dialog = dialog
            self.root.wait_window(dialog)
            new_config = dialog.result
        finally:
            self.preferences_dialog = None
            self.preferences_hidden_for_capture = False
            if not self.is_closing and previous_state != "withdrawn":
                try:
                    self._apply_main_window_state(previous_state, focus=True)
                except tk.TclError:
                    pass
        if self.is_closing:
            return
        if new_config is None:
            return

        previous_config = self.app_config
        if previous_config.launch_at_startup != new_config.launch_at_startup:
            try:
                WindowsStartupManager.set_enabled(new_config.launch_at_startup)
            except Exception as exc:
                new_config.launch_at_startup = previous_config.launch_at_startup
                messagebox.showwarning(
                    "开机启动设置失败",
                    str(exc),
                    parent=self.root,
                )
        self.app_config = new_config
        ConfigStore.save(self.app_config)
        self.settings.output_dir = Path(self.app_config.output_dir)
        self.settings.auto_save = self.app_config.auto_save
        self.settings.image_format = self.app_config.image_format
        self.settings.image_quality = self.app_config.image_quality
        self.settings.filename_pattern = self.app_config.filename_pattern
        self.auto_save_var.set(self.app_config.auto_save)
        self.delay_var.set(self.app_config.capture_delay)
        self.history = ScreenshotHistoryManager(
            Path(self.app_config.history_dir),
            self.app_config.max_history,
        )
        self.pin_manager.configure_defaults(
            self.app_config.pin_default_opacity / 100,
            self.app_config.pin_always_on_top,
            self.app_config.pin_restore_limit,
            self.app_config.pin_max_size,
            (
                self.app_config.pin_thumbnail_width,
                self.app_config.pin_thumbnail_height,
            ),
        )
        self._apply_accent_styles()
        self._bind_shortcuts()
        self._configure_global_hotkeys()
        self._configure_tray()
        self.output_dir_var.set(f"保存目录：{self.settings.output_dir}")
        self._set_status("首选项已保存")

    def handle_cli(self, args: List[str]) -> bool:
        if not args:
            return False

        command = args[0]
        rest = args[1:]
        if command == "paste":
            self._handle_cli_paste(rest)
            return True
        if command == "pick-color":
            self.root.after(150, self.pick_color)
            return True
        if command == "whiteboard":
            color = self._cli_option_value(rest, "--color") or "#ffffff"
            self.root.after(150, lambda: self.open_whiteboard(color))
            return True
        if command == "clear-snip-history":
            self.history.clear()
            self._set_status("截图历史已清空")
            return True
        if command == "open-preferences":
            self.root.after(150, self.open_preferences)
            return True
        if command == "toggle-images":
            self.root.after(150, self.toggle_pinned_images)
            return True
        if command == "show-images":
            self.root.after(150, lambda: [window.show() for window in self.pin_manager.windows])
            return True
        if command == "hide-images":
            self.root.after(150, lambda: [window.hide() for window in self.pin_manager.windows])
            return True
        if command == "snip":
            self._handle_cli_snip(rest)
            return True

        self._set_status(f"未知命令：{command}")
        return False

    def _handle_cli_paste(self, args: List[str]) -> None:
        try:
            position = self._cli_position(args)
        except ValueError as exc:
            self._show_error(f"命令行贴图失败：{exc}")
            return
        if "--plain" in args:
            text = self._cli_option_value(args, "--plain") or ""
            self.root.after(
                150,
                lambda: self.pin_manager.pin_image(
                    PinContentFactory.from_text(text),
                    "命令行文本贴图",
                    position=position,
                ),
            )
            return

        if "--html" in args:
            text = self._cli_option_value(args, "--html") or ""
            text = html_lib.unescape(re.sub(r"<[^>]+>", "", text))
            self.root.after(
                150,
                lambda: self.pin_manager.pin_image(
                    PinContentFactory.from_text(text),
                    "命令行 HTML 贴图",
                    position=position,
                ),
            )
            return

        if "--files" in args:
            paths = self._cli_file_args(args, "--files")
            self.root.after(150, lambda: self._pin_cli_files(paths, position))
            return

        self.root.after(150, self.paste_from_clipboard)

    def _handle_cli_snip(self, args: List[str]) -> None:
        output = self._cli_option_value(args, "-o", "--output") or "success"
        try:
            delay = float(self._cli_option_value(args, "--delay") or 0)
        except ValueError:
            self._show_error("命令行截图失败：--delay 必须是数字。")
            return

        def run() -> None:
            try:
                image = self._cli_capture_image(args)
                if image is None:
                    self.capture_region()
                    return
                self._apply_cli_output(image, output)
            except Exception as exc:
                self._show_error(f"命令行截图失败：{exc}")

        self.root.after(max(0, int(delay * 1000)), run)

    def _cli_capture_image(self, args: List[str]) -> Optional[Image.Image]:
        if "--full" in args:
            return self.manager.capture_screen()
        if "--active-window" in args:
            return ActiveWindowCapture.capture()
        if "--area" in args:
            index = args.index("--area")
            try:
                x = int(args[index + 1])
                y = int(args[index + 2])
                width = int(args[index + 3])
                height = int(args[index + 4])
            except (IndexError, ValueError) as exc:
                raise ValueError("用法：snip --area X Y WIDTH HEIGHT") from exc
            return ScreenshotManager.grab_box((x, y, x + width, y + height))
        return None

    def _apply_cli_output(self, image: Image.Image, output: str) -> None:
        self._receive_capture(
            image,
            record_history=True,
            kind="cli",
            allow_auto_save=output != "no-auto-save",
        )
        if output == "clipboard":
            self.copy_last_image()
        elif output == "pin":
            self.pin_manager.pin_image(image, "命令行贴图")
        elif output == "quick-save":
            self.quick_save_last_image()
        elif output == "file-dialog":
            self.save_as()
        elif output in {"success", "no-auto-save", "silent"}:
            self._set_status("命令行截图完成")
        else:
            target = Path(output)
            self.manager.save(image, target)
            self._set_status(f"命令行截图已保存：{target}")

    def _pin_cli_files(self, paths: List[Path], position: Optional[Point]) -> None:
        if not paths:
            self._set_status("命令行贴图失败：没有文件路径。")
            return

        base_x, base_y = position or (
            self.root.winfo_pointerx() + 24,
            self.root.winfo_pointery() + 24,
        )
        pinned = 0
        for index, path in enumerate(paths):
            try:
                image = Image.open(path).convert("RGB")
            except Exception:
                image = PinContentFactory.from_text(str(path))

            self.pin_manager.pin_image(
                image,
                f"命令行文件贴图 {path.name}",
                position=(base_x + index * 24, base_y + index * 24),
            )
            pinned += 1

        self._set_status(f"命令行已贴出 {pinned} 个内容。")

    @staticmethod
    def _cli_file_args(args: List[str], marker: str) -> List[Path]:
        if marker not in args:
            return []

        index = args.index(marker) + 1
        paths: List[Path] = []
        base_dir: Optional[Path] = None
        while index < len(args):
            token = args[index]
            if token.startswith("--"):
                break

            candidate = Path(token)
            if token.endswith(("\\", "/")):
                base_dir = candidate
                index += 1
                continue

            if not candidate.is_absolute() and base_dir is not None:
                candidate = base_dir / candidate
            paths.append(candidate)
            index += 1
        return paths

    @staticmethod
    def _cli_position(args: List[str]) -> Optional[Point]:
        if "--pos" not in args:
            return None
        index = args.index("--pos")
        try:
            return int(args[index + 1]), int(args[index + 2])
        except (IndexError, ValueError) as exc:
            raise ValueError("用法：--pos X Y") from exc

    @staticmethod
    def _cli_option_value(args: List[str], *names: str) -> Optional[str]:
        for name in names:
            if name in args:
                index = args.index(name)
                if index + 1 < len(args):
                    return args[index + 1]
        return None

    def save_as(self) -> None:
        if self.last_image is None:
            self._show_warning("请先截图，再保存。")
            return

        image_format = OutputNamePolicy.normalize_format(self.settings.image_format)
        extension = OutputNamePolicy.FORMAT_EXTENSIONS[image_format]
        initial = f"{OutputNamePolicy.render_stem(self.settings.filename_pattern)}{extension}"
        filetypes = (
            [("JPEG 图片", "*.jpg *.jpeg"), ("PNG 图片", "*.png")]
            if image_format == "JPEG"
            else [("PNG 图片", "*.png"), ("JPEG 图片", "*.jpg *.jpeg")]
        )
        filetypes.append(("所有文件", "*.*"))
        target = filedialog.asksaveasfilename(
            title="保存截图",
            initialdir=str(self.settings.output_dir),
            initialfile=initial,
            defaultextension=extension,
            filetypes=filetypes,
        )
        if not target:
            return

        try:
            self.last_file = self.manager.save(self.last_image, Path(target))
            self._set_status(f"已保存：{self.last_file}")
        except Exception as exc:
            self._show_error(f"保存失败：{exc}")

    def copy_last_image(self) -> None:
        if self.last_image is None:
            self._show_warning("请先截图，再复制。")
            return

        try:
            self.manager.copy_to_clipboard(self.last_image)
            self._set_status("截图已复制到剪贴板")
        except Exception as exc:
            self._show_error(f"复制失败：{exc}")

    def open_output_folder(self) -> None:
        self.settings.output_dir.mkdir(parents=True, exist_ok=True)
        path = str(self.settings.output_dir)

        try:
            if platform.system() == "Windows":
                os.startfile(path)  # type: ignore[attr-defined]
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:
            self._show_error(f"打开目录失败：{exc}")

    def _run_capture_after_delay(self, callback: Callable[[], None]) -> None:
        try:
            delay = max(0, min(10, int(self.delay_var.get())))
        except (tk.TclError, ValueError):
            delay = 0
            self.delay_var.set(delay)

        self._set_status("准备截图...")
        self._begin_capture_visibility()
        self.root.after(250 + delay * 1000, callback)

    def _begin_capture_visibility(self) -> None:
        if self.capture_return_state is None:
            self.capture_return_state = self.root.state()
        if (
            self.preferences_dialog
            and self.preferences_dialog.winfo_exists()
            and self.preferences_dialog.state() != "withdrawn"
        ):
            self.preferences_dialog.withdraw()
            self.preferences_hidden_for_capture = True
        self.root.withdraw()

    def _capture_full_screen_now(self) -> None:
        try:
            snapshot = self.manager.capture_snapshot()
            self._receive_capture(
                snapshot.image,
                kind="full",
                origin=snapshot.geometry.origin,
            )
        except Exception as exc:
            self._restore_window(force_show=True)
            self._show_error(f"截图失败：{exc}")

    def _capture_active_window_now(self) -> None:
        try:
            image, position = ActiveWindowCapture.capture_with_position()
            self._receive_capture(
                image,
                kind="active-window",
                origin=position,
            )
        except Exception as exc:
            self._restore_window(force_show=True)
            self._show_error(f"活动窗口截图失败：{exc}")

    def _start_region_capture(self) -> None:
        try:
            snapshot = self.manager.capture_snapshot()
            self.pending_screen = snapshot.image
            CaptureOverlay(
                self.root,
                self.pending_screen,
                on_capture=self._finish_region_capture,
                on_cancel=self._cancel_region_capture,
                smart_selection=self.app_config.smart_selection,
                color_format=self.app_config.color_format,
                screen_origin=snapshot.geometry.origin,
                **self._capture_overlay_display_options(),
            )
        except Exception as exc:
            self.pending_screen = None
            self._restore_window(force_show=True)
            self._show_error(f"框选截图失败：{exc}")

    def _start_long_region_capture(self, settings: LongScreenshotSettings) -> None:
        try:
            snapshot = self.manager.capture_snapshot()
            RegionSelectorOverlay(
                self.root,
                snapshot.image,
                on_select=lambda box: self._begin_long_capture(box, settings),
                on_cancel=self._cancel_long_capture,
                screen_origin=snapshot.geometry.origin,
            )
        except Exception as exc:
            self._restore_window(force_show=True)
            self._show_error(f"长截图选择失败：{exc}")

    def _start_color_picker(self) -> None:
        try:
            snapshot = self.manager.capture_snapshot()
            ColorPickerOverlay(
                self.root,
                snapshot.image,
                on_pick=self._finish_pick_color,
                on_cancel=self._cancel_color_picker,
                color_format=self.app_config.color_format,
                screen_origin=snapshot.geometry.origin,
                magnifier_zoom_index=self.app_config.magnifier_zoom_index,
            )
        except Exception as exc:
            self._restore_window(force_show=True)
            self._show_error(f"取色失败：{exc}")

    def _start_whiteboard(self, color: str = "#ffffff") -> None:
        try:
            snapshot = self.manager.capture_snapshot()
            whiteboard = Image.new(
                "RGB",
                snapshot.image.size,
                self._resolve_whiteboard_color(color),
            )
            overlay = CaptureOverlay(
                self.root,
                whiteboard,
                on_capture=self._finish_whiteboard,
                on_cancel=self._cancel_whiteboard,
                smart_selection=False,
                color_format=self.app_config.color_format,
                screen_origin=snapshot.geometry.origin,
                **self._capture_overlay_display_options(),
            )
            overlay.selection_box = (0, 0, whiteboard.width, whiteboard.height)
            overlay.tracker.set_box(overlay.selection_box)
            overlay._redraw_selection()
            overlay._set_cursor("crosshair")
        except Exception as exc:
            self._restore_window(force_show=True)
            self._show_error(f"白板启动失败：{exc}")

    @staticmethod
    def _resolve_whiteboard_color(color: str) -> Tuple[int, int, int]:
        try:
            resolved = ImageColor.getrgb(color.strip() or "#ffffff")
        except ValueError:
            resolved = ImageColor.getrgb("#ffffff")
        return resolved[:3]

    def _capture_overlay_display_options(self) -> dict[str, object]:
        return {
            "accent_color": self.app_config.accent_color,
            "mask_opacity": self.app_config.capture_mask_opacity,
            "border_width": self.app_config.capture_border_width,
            "show_handles": self.app_config.capture_show_handles,
            "show_crosshair": self.app_config.capture_show_crosshair,
            "show_magnifier": self.app_config.capture_show_magnifier,
            "magnifier_zoom_index": self.app_config.magnifier_zoom_index,
        }

    def _begin_long_capture(
        self, box: Box, settings: LongScreenshotSettings
    ) -> None:
        self.long_controller = LongScreenshotController(
            self.root,
            box,
            settings,
            on_progress=self._set_status,
            on_complete=self._finish_long_capture,
            on_error=self._handle_long_capture_error,
            on_cancel=self._cancel_long_capture,
        )
        self.long_controller.start()

    def _finish_long_capture(self, output: LongCaptureOutput) -> None:
        self.long_controller = None
        self.pending_long_output = output
        self._restore_window()
        self.long_result_dialog = LongScreenshotResultDialog(
            self.root,
            output,
            on_action=self._accept_long_result,
            on_cancel=self._cancel_long_result,
        )

    def _accept_long_result(
        self,
        image: Image.Image,
        action: str,
        crop_top: int,
    ) -> None:
        del crop_top
        output = self.pending_long_output
        self.pending_long_output = None
        self.long_result_dialog = None
        position = (output.source_box[0], output.source_box[1]) if output else None
        self._receive_capture(
            image,
            force_save=action == "save",
            kind="long",
            origin=position,
        )
        if CaptureActionPolicy.copies_to_clipboard(action):
            self.copy_last_image()
        elif action == "pin":
            self.pin_manager.pin_image(image, "长截图贴图", position=position)
        if output and output.partial:
            self._set_status("长截图部分完成：已保留能够可靠匹配的内容")

    def _cancel_long_result(self) -> None:
        self.pending_long_output = None
        self.long_result_dialog = None
        self._restore_window()
        self._set_status("已取消长截图结果")

    def _handle_long_capture_error(self, exc: Exception) -> None:
        self.long_controller = None
        self._restore_window(force_show=True)
        self._show_error(f"长截图失败：{exc}")

    def _cancel_long_capture(self) -> None:
        self.long_controller = None
        self._restore_window()
        self._set_status("已取消长截图")

    def _finish_region_capture(
        self,
        image: Image.Image,
        action: str,
        position: Optional[Point] = None,
    ) -> None:
        self.pending_screen = None
        self._receive_capture(
            image,
            force_save=action == "save",
            kind="region",
            origin=position,
        )

        if CaptureActionPolicy.copies_to_clipboard(action):
            self.copy_last_image()
        elif action == "pin":
            self.pin_manager.pin_image(image, "截图贴图", position=position)

    def _cancel_region_capture(self) -> None:
        self.pending_screen = None
        self._restore_window()
        self._set_status("已取消框选截图")

    def _finish_pick_color(self, color: Tuple[int, int, int], value: str) -> None:
        self._restore_window()
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self.last_image = PinContentFactory.color_card(color)
        self.last_capture_position = None
        self.info_var.set(f"当前取色：{value}")
        self._render_preview()
        self._set_status(f"颜色已复制：{value}")

    def _cancel_color_picker(self) -> None:
        self._restore_window()
        self._set_status("已取消取色")

    def _finish_whiteboard(
        self,
        image: Image.Image,
        action: str,
        position: Optional[Point] = None,
    ) -> None:
        self._receive_capture(
            image,
            force_save=action == "save",
            kind="whiteboard",
            origin=position,
        )
        if CaptureActionPolicy.copies_to_clipboard(action):
            self.copy_last_image()
        elif action == "pin":
            self.pin_manager.pin_image(image, "白板贴图", position=position)

    def _cancel_whiteboard(self) -> None:
        self._restore_window()
        self._set_status("已退出白板")

    def _load_history_image(self, image: Image.Image) -> None:
        self._receive_capture(image, force_save=False, record_history=False, kind="history")
        self._set_status("已从历史加载截图")

    def _copy_image_direct(self, image: Image.Image) -> None:
        try:
            self.manager.copy_to_clipboard(image)
            self._set_status("历史图片已复制到剪贴板")
        except Exception as exc:
            self._show_error(f"复制失败：{exc}")

    def _save_pinned_image(self, image: Image.Image) -> Path:
        path = self.manager.save(image)
        self._set_status(f"贴图已保存：{path.name}")
        return path

    def _receive_capture(
        self,
        image: Image.Image,
        force_save: bool = False,
        record_history: bool = True,
        kind: str = "capture",
        allow_auto_save: bool = True,
        origin: Optional[Point] = None,
    ) -> None:
        self.last_image = image
        self.last_file = None
        self.last_capture_position = origin
        self._restore_window()

        if record_history:
            try:
                self.history.add(image, kind=kind)
            except Exception as exc:
                self._set_status(f"截图完成，但写入历史失败：{exc}")

        if (allow_auto_save and self.auto_save_var.get()) or force_save:
            try:
                self.last_file = self.manager.save(image)
                action_text = "已保存" if force_save else "已自动保存"
                self._set_status(f"{action_text}：{self.last_file.name}")
            except Exception as exc:
                self._show_error(f"保存失败：{exc}")
        else:
            self._set_status("截图完成，尚未保存")

        self.info_var.set(f"当前截图：{image.width} x {image.height}")
        self._render_preview()

    def _restore_window(self, force_show: bool = False) -> None:
        state = MainWindowVisibilityPolicy.after_capture_state(
            self.app_config,
            self.capture_return_state,
            self._tray_available(),
            force_show=force_show,
        )
        self.capture_return_state = None
        if (
            self.preferences_hidden_for_capture
            and self.preferences_dialog
            and self.preferences_dialog.winfo_exists()
        ):
            state = "withdrawn"
        if state is not None:
            self._apply_main_window_state(state, focus=state == "normal")
        if (
            self.preferences_hidden_for_capture
            and self.preferences_dialog
            and self.preferences_dialog.winfo_exists()
        ):
            self.preferences_hidden_for_capture = False
            self.preferences_dialog.deiconify()
            self.preferences_dialog.lift()
            self.preferences_dialog.focus_force()

    def _sync_settings(self) -> None:
        self.settings.auto_save = bool(self.auto_save_var.get())
        self.app_config.auto_save = self.settings.auto_save
        try:
            capture_delay = max(0, min(10, int(self.delay_var.get())))
        except (tk.TclError, ValueError):
            capture_delay = self.app_config.capture_delay
        self.delay_var.set(capture_delay)
        self.app_config.capture_delay = capture_delay
        ConfigStore.save(self.app_config)

    def _schedule_preview_render(self, event: Optional[tk.Event] = None) -> None:
        if self.preview_resize_job:
            self.root.after_cancel(self.preview_resize_job)
        self.preview_resize_job = self.root.after(80, self._render_preview)

    def _render_preview(self) -> None:
        self.preview_resize_job = None
        self.preview_canvas.delete("all")

        width = max(1, self.preview_canvas.winfo_width() - 24)
        height = max(1, self.preview_canvas.winfo_height() - 24)

        if self.last_image is None:
            self.preview_canvas.create_text(
                self.preview_canvas.winfo_width() // 2,
                self.preview_canvas.winfo_height() // 2,
                text="截图预览会显示在这里",
                fill="#637083",
                font=("Microsoft YaHei UI", 14),
            )
            return

        preview = self.last_image.copy()
        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        preview.thumbnail((width, height), resample)
        self.preview_photo = ImageTk.PhotoImage(preview)
        self.preview_canvas.create_image(
            self.preview_canvas.winfo_width() // 2,
            self.preview_canvas.winfo_height() // 2,
            image=self.preview_photo,
            anchor=tk.CENTER,
        )

    def _set_status(self, message: str) -> None:
        self.status_var.set(message)

    def _show_warning(self, message: str) -> None:
        self._set_status(message)
        messagebox.showwarning("提示", message)

    def _show_error(self, message: str) -> None:
        self._set_status(message)
        messagebox.showerror("错误", message)


def main() -> None:
    enable_high_dpi()
    root = tk.Tk()
    app = ScreenshotApp(root)
    if len(sys.argv) > 1:
        app.handle_cli(sys.argv[1:])
    root.mainloop()


if __name__ == "__main__":
    main()
