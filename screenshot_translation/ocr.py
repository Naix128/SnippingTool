from __future__ import annotations

import importlib
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from PIL import Image


@dataclass(frozen=True)
class DetectedTextLine:
    text: str
    polygon: tuple[tuple[int, int], ...]
    confidence: float | None = None

    @property
    def box(self) -> tuple[int, int, int, int]:
        if not self.polygon:
            return 0, 0, 0, 0
        xs = [point[0] for point in self.polygon]
        ys = [point[1] for point in self.polygon]
        return min(xs), min(ys), max(xs), max(ys)


class RapidOcrUnavailable(RuntimeError):
    pass


class RapidOcrAdapter:
    """Loads RapidOCR lazily so the base application remains lightweight."""

    def __init__(self, quality: str = "balanced") -> None:
        self.quality = quality if quality in {"balanced", "accurate"} else "balanced"
        self._engine: Any = None

    @staticmethod
    def is_available() -> bool:
        try:
            importlib.import_module("rapidocr")
            importlib.import_module("onnxruntime")
        except ImportError:
            return False
        return True

    def recognize(self, image: Image.Image) -> tuple[DetectedTextLine, ...]:
        engine = self._get_engine()
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="screenshot_rapidocr_",
                suffix=".png",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                image.convert("RGB").save(temporary, "PNG")
            output = engine(str(temporary_path))
            return self.parse_output(output)
        finally:
            if temporary_path:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _get_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        try:
            rapidocr = importlib.import_module("rapidocr")
            RapidOCR = rapidocr.RapidOCR
        except ImportError as exc:
            raise RapidOcrUnavailable(
                "高精度 OCR 组件未安装，请安装 rapidocr 和 onnxruntime。"
            ) from exc

        if self.quality == "accurate":
            try:
                self._engine = RapidOCR(
                    params={
                        "Det.engine_type": rapidocr.EngineType.ONNXRUNTIME,
                        "Det.lang_type": rapidocr.LangDet.CH,
                        "Det.model_type": rapidocr.ModelType.MEDIUM,
                        "Det.ocr_version": rapidocr.OCRVersion.PPOCRV6,
                        "Rec.engine_type": rapidocr.EngineType.ONNXRUNTIME,
                        "Rec.lang_type": rapidocr.LangRec.CH,
                        "Rec.model_type": rapidocr.ModelType.MEDIUM,
                        "Rec.ocr_version": rapidocr.OCRVersion.PPOCRV6,
                    }
                )
            except (ImportError, AttributeError, TypeError, ValueError):
                self._engine = RapidOCR()
        else:
            self._engine = RapidOCR()
        return self._engine

    @classmethod
    def parse_output(cls, output: Any) -> tuple[DetectedTextLine, ...]:
        lines: list[DetectedTextLine] = []
        if output is None:
            return ()

        boxes = getattr(output, "boxes", None)
        texts = getattr(output, "txts", None)
        scores = getattr(output, "scores", None)
        if boxes is not None and texts is not None:
            score_values = list(scores) if scores is not None else []
            for index, (box, text) in enumerate(zip(boxes, texts)):
                score = score_values[index] if index < len(score_values) else None
                line = cls._line(box, text, score)
                if line:
                    lines.append(line)
        else:
            rows = output[0] if isinstance(output, tuple) and output else output
            for row in rows if isinstance(rows, Iterable) else ():
                if not isinstance(row, (list, tuple)) or len(row) < 2:
                    continue
                score = row[2] if len(row) > 2 else None
                line = cls._line(row[0], row[1], score)
                if line:
                    lines.append(line)

        lines.sort(key=lambda item: (item.box[1], item.box[0]))
        return tuple(lines)

    @staticmethod
    def _line(box: Any, text: Any, score: Any) -> DetectedTextLine | None:
        value = str(text or "").strip()
        if not value:
            return None
        if hasattr(box, "tolist"):
            box = box.tolist()
        points: list[tuple[int, int]] = []
        try:
            for point in box:
                points.append((int(round(float(point[0]))), int(round(float(point[1])))))
            confidence = None if score is None else float(score)
        except (TypeError, ValueError, IndexError):
            return None
        if len(points) < 3:
            return None
        return DetectedTextLine(value, tuple(points), confidence)
