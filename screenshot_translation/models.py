from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from PIL import Image

Point = Tuple[int, int]
Box = Tuple[int, int, int, int]


LANGUAGE_LABELS: dict[str, str] = {
    "zh": "简体中文",
    "zt": "繁体中文（本地模型）",
    "zh-TW": "繁体中文（中国台湾）",
    "zh-HK": "繁体中文（中国香港）",
    "en": "英语",
    "ja": "日语",
    "ko": "韩语",
    "ar": "阿拉伯语",
    "az": "阿塞拜疆语",
    "bg": "保加利亚语",
    "bn": "孟加拉语",
    "ca": "加泰罗尼亚语",
    "cs": "捷克语",
    "da": "丹麦语",
    "de": "德语",
    "el": "希腊语",
    "eo": "世界语",
    "es": "西班牙语",
    "et": "爱沙尼亚语",
    "eu": "巴斯克语",
    "fa": "波斯语",
    "fi": "芬兰语",
    "fr": "法语",
    "ga": "爱尔兰语",
    "gl": "加利西亚语",
    "he": "希伯来语",
    "hi": "印地语",
    "hu": "匈牙利语",
    "id": "印度尼西亚语",
    "it": "意大利语",
    "ky": "吉尔吉斯语",
    "lt": "立陶宛语",
    "lv": "拉脱维亚语",
    "ms": "马来语",
    "nb": "挪威语",
    "nl": "荷兰语",
    "pb": "葡萄牙语（巴西，本地）",
    "pl": "波兰语",
    "pt": "葡萄牙语",
    "ro": "罗马尼亚语",
    "ru": "俄语",
    "sk": "斯洛伐克语",
    "sl": "斯洛文尼亚语",
    "sq": "阿尔巴尼亚语",
    "sv": "瑞典语",
    "sw": "斯瓦希里语",
    "th": "泰语",
    "tl": "他加禄语",
    "tr": "土耳其语",
    "uk": "乌克兰语",
    "ur": "乌尔都语",
    "vi": "越南语",
}


def language_label(code: object) -> str:
    value = str(code or "").strip()
    if value in {"", "auto"}:
        return "自动检测"
    return LANGUAGE_LABELS.get(value, value or "自动检测")


def language_code_for_label(label: object, fallback: str = "zh") -> str:
    value = str(label or "").strip()
    if value in {"auto", "自动检测"}:
        return "auto"
    if value in LANGUAGE_LABELS:
        return value
    for code, name in LANGUAGE_LABELS.items():
        if name == value:
            return code
    return fallback if fallback in LANGUAGE_LABELS else "zh"


@dataclass(frozen=True)
class TranslationCredentials:
    secret_id: str
    secret_key: str

    @property
    def complete(self) -> bool:
        return bool(self.secret_id.strip() and self.secret_key.strip())

    @property
    def masked_id(self) -> str:
        value = self.secret_id.strip()
        if len(value) <= 8:
            return "*" * len(value)
        return f"{value[:4]}...{value[-4:]}"


@dataclass(frozen=True)
class TranslationRegion:
    source_text: str
    target_text: str
    polygon: Tuple[Point, ...]
    line_height: int = 0
    line_count: int = 0

    @property
    def box(self) -> Box:
        if not self.polygon:
            return 0, 0, 0, 0
        xs = [point[0] for point in self.polygon]
        ys = [point[1] for point in self.polygon]
        return min(xs), min(ys), max(xs), max(ys)

    def contains(self, point: Point, padding: int = 3) -> bool:
        left, top, right, bottom = self.box
        x, y = point
        if not (
            left - padding <= x <= right + padding
            and top - padding <= y <= bottom + padding
        ):
            return False
        if len(self.polygon) < 3:
            return True

        inside = False
        previous = self.polygon[-1]
        for current in self.polygon:
            x1, y1 = previous
            x2, y2 = current
            crosses = (y1 > y) != (y2 > y)
            if crosses:
                boundary_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
                if x < boundary_x:
                    inside = not inside
            previous = current
        return inside

    def offset(self, dx: int = 0, dy: int = 0) -> "TranslationRegion":
        return TranslationRegion(
            self.source_text,
            self.target_text,
            tuple((x + int(dx), y + int(dy)) for x, y in self.polygon),
            self.line_height,
            self.line_count,
        )


@dataclass
class TranslationDocument:
    translated_image: Image.Image
    source_language: str
    target_language: str
    source_text: str
    target_text: str
    regions: Tuple[TranslationRegion, ...]
    request_id: str = ""
    provider: str = ""
    angle: float = 0.0

    def region_at(self, point: Point) -> Optional[int]:
        candidates = [
            (index, region)
            for index, region in enumerate(self.regions)
            if region.contains(point)
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda item: max(1, item[1].box[2] - item[1].box[0])
            * max(1, item[1].box[3] - item[1].box[1]),
        )[0]


class TranslationError(RuntimeError):
    def __init__(self, message: str, code: str = "", request_id: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id


class TranslationCancelled(TranslationError):
    def __init__(self) -> None:
        super().__init__("已取消图片翻译。", code="Cancelled")
