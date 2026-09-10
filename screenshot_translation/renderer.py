from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat

from .models import Box, TranslationRegion


class ImageTilePlanner:
    """Splits unusually tall screenshots near low-detail horizontal rows."""

    def __init__(self, max_tile_height: int = 3200, search_radius: int = 180) -> None:
        self.max_tile_height = max(800, int(max_tile_height))
        self.search_radius = max(20, int(search_radius))

    def plan(self, image: Image.Image) -> list[Box]:
        if image.height <= self.max_tile_height:
            return [(0, 0, image.width, image.height)]
        bounds: list[Box] = []
        top = 0
        while image.height - top > self.max_tile_height:
            desired = top + self.max_tile_height
            split = self._quiet_row(image, desired, top + 640)
            split = max(top + 640, min(image.height - 320, split))
            bounds.append((0, top, image.width, split))
            top = split
        bounds.append((0, top, image.width, image.height))
        return bounds

    def _quiet_row(self, image: Image.Image, desired: int, minimum: int) -> int:
        left = max(minimum, desired - self.search_radius)
        right = min(image.height - 1, desired + self.search_radius)
        if right <= left:
            return desired
        sample = image.convert("L")
        if sample.width > 480:
            ratio = 480 / sample.width
            sample = sample.resize(
                (480, max(1, int(sample.height * ratio))),
                getattr(getattr(Image, "Resampling", Image), "BILINEAR"),
            )
            scale_y = sample.height / image.height
        else:
            scale_y = 1.0
        best_y = desired
        best_score = float("inf")
        for original_y in range(left, right + 1, 3):
            y = max(1, min(sample.height - 2, int(original_y * scale_y)))
            row = sample.crop((0, y - 1, sample.width, y + 2))
            stat = ImageStat.Stat(row)
            score = stat.var[0] + abs(stat.extrema[0][1] - stat.extrema[0][0]) * 0.25
            if score < best_score:
                best_score = score
                best_y = original_y
        return best_y


class LocalTranslationRenderer:
    """Creates a readable local translated image without changing the source."""

    FONT_CANDIDATES = {
        "ja": ("YuGothM.ttc", "YuGothR.ttc", "msgothic.ttc"),
        "ko": ("malgun.ttf", "malgunsl.ttf"),
        "default": ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf", "arial.ttf"),
    }

    def render(
        self,
        image: Image.Image,
        regions: Sequence[TranslationRegion],
        target_language: str = "zh",
    ) -> Image.Image:
        output = image.convert("RGB").copy()
        for region in regions:
            text = region.target_text.strip()
            left, top, right, bottom = self._clip_box(region.box, output.size)
            if not text or right - left < 4 or bottom - top < 4:
                continue
            background, complex_background = self._background_color(
                output,
                (left, top, right, bottom),
            )
            mask = Image.new("L", output.size, 0)
            mask_draw = ImageDraw.Draw(mask)
            polygon = region.polygon or (
                (left, top),
                (right, top),
                (right, bottom),
                (left, bottom),
            )
            mask_draw.polygon(polygon, fill=255)
            if complex_background:
                softened = output.filter(ImageFilter.GaussianBlur(radius=8))
                output.paste(softened, mask=mask)
                overlay = Image.new("RGB", output.size, background)
                alpha = mask.point(lambda value: int(value * 0.72))
                output.paste(overlay, mask=alpha)
            else:
                fill = Image.new("RGB", output.size, background)
                output.paste(fill, mask=mask)

            draw = ImageDraw.Draw(output)
            foreground = self._foreground(background)
            font, lines, line_height = self._fit_text(
                draw,
                text,
                (left, top, right, bottom),
                target_language,
            )
            total_height = line_height * len(lines)
            y = top + max(1, (bottom - top - total_height) // 2)
            for line in lines:
                draw.text((left + 2, y), line, font=font, fill=foreground)
                y += line_height
        return output

    def _fit_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        box: Box,
        target_language: str,
    ) -> tuple[ImageFont.ImageFont, list[str], int]:
        left, top, right, bottom = box
        width = max(4, right - left - 4)
        height = max(4, bottom - top - 2)
        maximum = max(9, min(72, int(height * 0.9)))
        best_font = self._font(9, target_language)
        best_lines = [text]
        best_line_height = 11
        low, high = 8, maximum
        while low <= high:
            size = (low + high) // 2
            font = self._font(size, target_language)
            lines = self._wrap(draw, text, font, width)
            line_height = self._line_height(draw, font)
            widest = max((self._text_width(draw, line, font) for line in lines), default=0)
            if widest <= width and line_height * len(lines) <= height:
                best_font = font
                best_lines = lines
                best_line_height = line_height
                low = size + 1
            else:
                high = size - 1
        return best_font, best_lines, best_line_height

    def _wrap(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.ImageFont,
        width: int,
    ) -> list[str]:
        lines: list[str] = []
        for paragraph in text.splitlines() or [text]:
            tokens = self._tokens(paragraph)
            current = ""
            for token in tokens:
                candidate = f"{current}{token}"
                if current and self._text_width(draw, candidate, font) > width:
                    lines.append(current.rstrip())
                    current = token.lstrip()
                else:
                    current = candidate
            lines.append(current.rstrip() or " ")
        return lines or [""]

    @staticmethod
    def _tokens(text: str) -> list[str]:
        if any("\u3400" <= char <= "\u9fff" for char in text):
            return list(text)
        words = text.split(" ")
        return [word + (" " if index < len(words) - 1 else "") for index, word in enumerate(words)]

    @staticmethod
    def _text_width(
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.ImageFont,
    ) -> int:
        if hasattr(draw, "textlength"):
            return int(math.ceil(draw.textlength(text, font=font)))
        box = draw.textbbox((0, 0), text, font=font)
        return box[2] - box[0]

    @staticmethod
    def _line_height(draw: ImageDraw.ImageDraw, font: ImageFont.ImageFont) -> int:
        box = draw.textbbox((0, 0), "国Ag", font=font)
        return max(1, box[3] - box[1] + 2)

    def _font(self, size: int, target_language: str) -> ImageFont.ImageFont:
        windir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        names = self.FONT_CANDIDATES.get(
            target_language,
            self.FONT_CANDIDATES["default"],
        ) + self.FONT_CANDIDATES["default"]
        for name in dict.fromkeys(names):
            path = windir / name
            if path.exists():
                try:
                    return ImageFont.truetype(str(path), max(8, int(size)))
                except OSError:
                    continue
        return ImageFont.load_default()

    @staticmethod
    def _background_color(image: Image.Image, box: Box) -> tuple[tuple[int, int, int], bool]:
        left, top, right, bottom = box
        margin = 3
        outer = image.crop(
            (
                max(0, left - margin),
                max(0, top - margin),
                min(image.width, right + margin),
                min(image.height, bottom + margin),
            )
        ).convert("RGB")
        stat = ImageStat.Stat(outer)
        color = tuple(int(round(value)) for value in stat.median[:3])
        variance = sum(stat.var[:3]) / 3
        return color, variance > 900

    @staticmethod
    def _foreground(background: tuple[int, int, int]) -> tuple[int, int, int]:
        red, green, blue = background
        luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
        return (20, 24, 29) if luminance > 145 else (248, 250, 252)

    @staticmethod
    def _clip_box(box: Box, size: tuple[int, int]) -> Box:
        left, top, right, bottom = box
        return (
            max(0, min(size[0], int(left))),
            max(0, min(size[1], int(top))),
            max(0, min(size[0], int(right))),
            max(0, min(size[1], int(bottom))),
        )
