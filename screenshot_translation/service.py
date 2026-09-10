from __future__ import annotations

import hashlib
import threading
from collections import Counter, OrderedDict
from typing import Optional

from PIL import Image

from .models import (
    TranslationCancelled,
    TranslationDocument,
    TranslationError,
    TranslationRegion,
)
from .providers import ProgressCallback
from .renderer import ImageTilePlanner, LocalTranslationRenderer


class ImageTranslationService:
    MODES = {"local", "tencent_smart", "smart", "cloud", "privacy"}

    def __init__(
        self,
        provider: object,
        ocr_backend: Optional[object] = None,
        renderer: Optional[LocalTranslationRenderer] = None,
        tile_planner: Optional[ImageTilePlanner] = None,
        cache_size: int = 6,
    ) -> None:
        self.provider = provider
        self.ocr_backend = ocr_backend
        self.renderer = renderer or LocalTranslationRenderer()
        self.tile_planner = tile_planner or ImageTilePlanner()
        self.cache_size = max(0, int(cache_size))
        self._cache: OrderedDict[str, TranslationDocument] = OrderedDict()
        self._cache_lock = threading.Lock()

    def translate(
        self,
        image: Image.Image,
        target_language: str,
        mode: str = "local",
        quality_mode: int = 0,
        progress: Optional[ProgressCallback] = None,
        cancel_event: Optional[threading.Event] = None,
        source_language: str = "auto",
    ) -> TranslationDocument:
        normalized_mode = mode if mode in self.MODES else "local"
        key = self._cache_key(
            image,
            source_language,
            target_language,
            normalized_mode,
            quality_mode,
        )
        cached = self._cached(key)
        if cached is not None:
            if progress:
                progress("已载入本次翻译结果")
            return cached
        if cancel_event and cancel_event.is_set():
            raise TranslationCancelled()

        if normalized_mode in {"local", "privacy"}:
            result = self._translate_privately(
                image,
                target_language,
                progress,
                cancel_event,
                source_language,
            )
        elif normalized_mode in {"smart", "tencent_smart"}:
            try:
                result = self._translate_cloud(
                    image, target_language, quality_mode, progress, cancel_event
                )
            except TranslationError as exc:
                if exc.code not in {"ImageTooLarge", "InvalidImage"}:
                    raise
                if progress:
                    progress("同版式翻译不可用，正在改用仅上传文字模式...")
                result = self._translate_privately(
                    image,
                    target_language,
                    progress,
                    cancel_event,
                    source_language,
                )
        else:
            result = self._translate_cloud(
                image,
                target_language,
                quality_mode,
                progress,
                cancel_event,
            )
        self._store(key, result)
        return result

    def clear_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    def _translate_cloud(
        self,
        image: Image.Image,
        target_language: str,
        quality_mode: int,
        progress: Optional[ProgressCallback],
        cancel_event: Optional[threading.Event],
    ) -> TranslationDocument:
        tiles = self.tile_planner.plan(image)
        if len(tiles) == 1:
            return self.provider.translate_image(
                image,
                target_language,
                quality_mode,
                progress,
                cancel_event,
            )

        translated_image = Image.new("RGB", image.size, "white")
        regions: list[TranslationRegion] = []
        source_texts: list[str] = []
        target_texts: list[str] = []
        source_languages: list[str] = []
        request_ids: list[str] = []
        for index, box in enumerate(tiles, start=1):
            if cancel_event and cancel_event.is_set():
                raise TranslationCancelled()
            if progress:
                progress(f"正在翻译长图分段 {index}/{len(tiles)}...")
            left, top, right, bottom = box
            tile = image.crop(box)
            document = self.provider.translate_image(
                tile,
                target_language,
                quality_mode,
                None,
                cancel_event,
            )
            translated_tile = document.translated_image.convert("RGB")
            tile_width = right - left
            tile_height = bottom - top
            scale_x = tile_width / max(1, translated_tile.width)
            scale_y = tile_height / max(1, translated_tile.height)
            if translated_tile.size != (tile_width, tile_height):
                translated_tile = translated_tile.resize(
                    (tile_width, tile_height),
                    getattr(getattr(Image, "Resampling", Image), "LANCZOS"),
                )
            translated_image.paste(translated_tile, (left, top))
            for region in document.regions:
                scaled = TranslationRegion(
                    region.source_text,
                    region.target_text,
                    tuple(
                        (
                            left + int(round(x * scale_x)),
                            top + int(round(y * scale_y)),
                        )
                        for x, y in region.polygon
                    ),
                    int(round(region.line_height * scale_y)),
                    region.line_count,
                )
                regions.append(scaled)
            if document.source_text:
                source_texts.append(document.source_text)
            if document.target_text:
                target_texts.append(document.target_text)
            if document.source_language:
                source_languages.append(document.source_language)
            if document.request_id:
                request_ids.append(document.request_id)

        source_language = (
            Counter(source_languages).most_common(1)[0][0]
            if source_languages
            else "auto"
        )
        return TranslationDocument(
            translated_image,
            source_language,
            target_language,
            "\n".join(source_texts),
            "\n".join(target_texts),
            tuple(regions),
            request_id=",".join(request_ids),
            provider="TencentCloud/ImageTranslateLLM",
        )

    def _translate_privately(
        self,
        image: Image.Image,
        target_language: str,
        progress: Optional[ProgressCallback],
        cancel_event: Optional[threading.Event],
        requested_source_language: str = "auto",
    ) -> TranslationDocument:
        if self.ocr_backend is None or not hasattr(self.ocr_backend, "recognize"):
            raise TranslationError("隐私翻译需要可用的本地 OCR 引擎。", code="OcrUnavailable")
        if progress:
            progress("正在本地识别文字...")
        if (
            requested_source_language not in {"", "auto"}
            and hasattr(self.ocr_backend, "recognize_for_language")
        ):
            document = self.ocr_backend.recognize_for_language(
                image,
                requested_source_language,
            )
        else:
            document = self.ocr_backend.recognize(image)
        lines = [str(line.text).strip() for line in getattr(document, "lines", ())]
        source_lines = [line for line in lines if line]
        if not source_lines:
            raise TranslationError("图片中没有识别到可翻译的文字。", code="NoText")
        translations, source_language, request_ids = self.provider.translate_texts(
            source_lines,
            target_language,
            source_language=requested_source_language,
            progress=progress,
            cancel_event=cancel_event,
        )
        regions: list[TranslationRegion] = []
        translation_index = 0
        for line in getattr(document, "lines", ()):
            source_text = str(getattr(line, "text", "")).strip()
            if not source_text:
                continue
            target_text = (
                translations[translation_index]
                if translation_index < len(translations)
                else ""
            )
            translation_index += 1
            left, top, right, bottom = tuple(getattr(line, "box"))
            regions.append(
                TranslationRegion(
                    source_text,
                    target_text,
                    ((left, top), (right, top), (right, bottom), (left, bottom)),
                    bottom - top,
                    1,
                )
            )
        if progress:
            progress("正在本地生成译图...")
        translated_image = self.renderer.render(image, regions, target_language)
        return TranslationDocument(
            translated_image,
            source_language,
            target_language,
            "\n".join(region.source_text for region in regions),
            "\n".join(region.target_text for region in regions),
            tuple(regions),
            request_id=",".join(request_ids),
            provider=f"LocalOCR/{getattr(self.provider, 'provider_name', 'TextTranslate')}",
        )

    def _cache_key(
        self,
        image: Image.Image,
        source_language: str,
        target_language: str,
        mode: str,
        quality_mode: int,
    ) -> str:
        digest = hashlib.sha256()
        digest.update(image.mode.encode("ascii", errors="ignore"))
        digest.update(f"{image.width}x{image.height}".encode("ascii"))
        digest.update(image.tobytes())
        digest.update(
            f"{source_language}|{target_language}|{mode}|{quality_mode}".encode("utf-8")
        )
        return digest.hexdigest()

    def _cached(self, key: str) -> Optional[TranslationDocument]:
        if self.cache_size <= 0:
            return None
        with self._cache_lock:
            value = self._cache.pop(key, None)
            if value is not None:
                self._cache[key] = value
            return value

    def _store(self, key: str, value: TranslationDocument) -> None:
        if self.cache_size <= 0:
            return
        with self._cache_lock:
            self._cache[key] = value
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
