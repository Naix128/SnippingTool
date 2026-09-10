from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Optional, Sequence

from .models import TranslationCancelled, TranslationError

ProgressCallback = Callable[[str], None]


@dataclass(frozen=True)
class LocalModelSpec:
    source_language: str
    target_language: str
    archive_name: str
    sha256: str
    download_size: int
    urls: tuple[str, ...]
    source_name: str = ""
    target_name: str = ""
    package_version: str = ""

    @property
    def pair_name(self) -> str:
        return f"{self.source_language}_{self.target_language}"

    @property
    def size_mb(self) -> float:
        return self.download_size / (1024 * 1024)


_MODELSCOPE_ROOT = "https://www.modelscope.cn/models/wer277/translate/resolve/master"
_ARGOS_ROOT = "https://argos-net.com/v1"
_HF_REVISION = "5d4c74df5d008755aaeea29fc6c962381a4bcb38"
_HF_ROOT = (
    "https://huggingface.co/TiberiuCristianLeon/Argostranslate/resolve/"
    f"{_HF_REVISION}"
)
_HF_MIRROR_ROOT = (
    "https://hf-mirror.com/TiberiuCristianLeon/Argostranslate/resolve/"
    f"{_HF_REVISION}"
)
_LIBRE_MODELS_ROOT = (
    "https://github.com/LibreTranslate/LibreTranslate-Models/raw/refs/heads/main"
)
_HF_MANIFEST_URL = (
    "https://hf-mirror.com/api/models/TiberiuCristianLeon/Argostranslate/tree/"
    f"{_HF_REVISION}?recursive=true&expand=false&limit=1000"
)
_CATALOG_URLS = (
    "https://cdn.jsdelivr.net/gh/argosopentech/argospm-index@main/index.json",
    "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json",
)

LOCAL_MODEL_SPECS: dict[tuple[str, str], LocalModelSpec] = {
    ("en", "zh"): LocalModelSpec(
        "en",
        "zh",
        "translate-en_zh-1_9.argosmodel",
        "433e7c4f034d87fbe2353161e05f18646d7999452f801a4e1f0378522b9850ab",
        70_743_021,
        (
            f"{_MODELSCOPE_ROOT}/translate-en_zh-1_9.argosmodel",
            f"{_ARGOS_ROOT}/translate-en_zh-1_9.argosmodel",
            f"{_HF_ROOT}/translate-en_zh-1_9.argosmodel",
        ),
        "English",
        "Chinese",
        "1.9",
    ),
    ("zh", "en"): LocalModelSpec(
        "zh",
        "en",
        "translate-zh_en-1_9.argosmodel",
        "62e7af5a3a48b530e47b7b3e5c78c2de79073ecd815750d2bf3ab35b4a67da2d",
        74_481_402,
        (
            f"{_MODELSCOPE_ROOT}/translate-zh_en-1_9.argosmodel",
            f"{_ARGOS_ROOT}/translate-zh_en-1_9.argosmodel",
            f"{_HF_ROOT}/translate-zh_en-1_9.argosmodel",
        ),
        "Chinese",
        "English",
        "1.9",
    ),
}


def default_local_model_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "ScreenshotTool" / "models" / "translation"
    return Path.home() / ".screenshot-tool" / "models" / "translation"


class LocalModelManager:
    """Downloads verified OPUS/Argos model packages and installs them atomically."""

    MAX_ARCHIVE_MEMBERS = 100
    MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
    MAX_EXTRACTED_BYTES = 512 * 1024 * 1024

    def __init__(
        self,
        root: Optional[Path] = None,
        opener: Optional[Callable[..., object]] = None,
    ) -> None:
        self.root = Path(root) if root is not None else default_local_model_root()
        self.opener = opener or urllib.request.urlopen
        self._lock = threading.RLock()
        self._catalog: dict[tuple[str, str], LocalModelSpec] = {}
        self.last_catalog_error = ""

    @property
    def catalog_cache_path(self) -> Path:
        return self.root / ".catalog.json"

    def catalog(
        self,
        refresh: bool = False,
        progress: Optional[ProgressCallback] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> tuple[LocalModelSpec, ...]:
        with self._lock:
            if self._catalog and not refresh:
                return self._sorted_specs(self._catalog.values())
            cached = self._load_catalog_cache()
            if refresh or not cached:
                try:
                    if progress:
                        progress("正在刷新开源模型目录...")
                    payload = self._fetch_catalog(cancel_event)
                    cached = self._parse_catalog(payload)
                    self._save_catalog_cache(payload)
                    self.last_catalog_error = ""
                except TranslationCancelled:
                    raise
                except (OSError, TranslationError, ValueError, urllib.error.URLError) as exc:
                    self.last_catalog_error = str(exc)
            cached.update(LOCAL_MODEL_SPECS)
            self._catalog = cached
            return self._sorted_specs(cached.values())

    def spec_for(self, source_language: str, target_language: str) -> LocalModelSpec:
        key = self._normalize_code(source_language), self._normalize_code(target_language)
        curated = LOCAL_MODEL_SPECS.get(key)
        if curated is not None:
            return curated
        spec = next(
            (
                item
                for item in self.catalog()
                if (item.source_language, item.target_language) == key
            ),
            None,
        )
        if spec is None:
            raise TranslationError(
                f"开源模型目录中没有 {key[0]}→{key[1]} 的直连模型。",
                code="LocalLanguagePairUnsupported",
            )
        return spec

    def model_directory(self, source_language: str, target_language: str) -> Path:
        source = self._normalize_code(source_language)
        target = self._normalize_code(target_language)
        if not all(re.fullmatch(r"[a-z]{2,3}", code) for code in (source, target)):
            raise TranslationError("本地翻译语言代码无效。", code="LocalLanguageInvalid")
        return self.root / f"{source}_{target}"

    def is_installed(self, source_language: str, target_language: str) -> bool:
        source = self._normalize_code(source_language)
        target = self._normalize_code(target_language)
        try:
            directory = self.model_directory(source, target)
        except TranslationError:
            return False
        return self._valid_model_directory(directory, source, target)

    def status_text(self) -> str:
        count = len(self.installed_pairs())
        return f"已安装 {count} 个模型" if count else "尚未安装模型"

    def installed_size(self, source_language: str, target_language: str) -> int:
        directory = self.model_directory(source_language, target_language)
        if not directory.is_dir():
            return 0
        try:
            return sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())
        except OSError:
            return 0

    def installed_pairs(self) -> tuple[tuple[str, str], ...]:
        if not self.root.is_dir():
            return ()
        pairs: list[tuple[str, str]] = []
        for directory in self.root.iterdir():
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            metadata = self._read_model_metadata(directory)
            if not metadata:
                continue
            source = self._normalize_code(metadata.get("from_code", ""))
            target = self._normalize_code(metadata.get("to_code", ""))
            if self._valid_model_directory(directory, source, target):
                pairs.append((source, target))
        return tuple(sorted(set(pairs)))

    def route_is_installed(self, source_language: str, target_language: str) -> bool:
        source = self._normalize_code(source_language)
        target = self._normalize_code(target_language)
        if source == target:
            return True
        if self.is_installed(source, target):
            return True
        return (
            source != "en"
            and target != "en"
            and self.is_installed(source, "en")
            and self.is_installed("en", target)
        )

    def resolve_route(
        self,
        source_language: str,
        target_language: str,
    ) -> tuple[LocalModelSpec, ...]:
        source = self._normalize_code(source_language)
        target = self._normalize_code(target_language)
        if source == target:
            return ()
        curated = LOCAL_MODEL_SPECS.get((source, target))
        if curated is not None:
            return (curated,)
        catalog = {
            (item.source_language, item.target_language): item
            for item in self.catalog()
        }
        direct = catalog.get((source, target))
        if direct:
            return (direct,)
        first = catalog.get((source, "en"))
        second = catalog.get(("en", target))
        if source != "en" and target != "en" and first and second:
            return first, second
        raise TranslationError(
            f"开源模型目录中没有可用的 {source}→{target} 翻译路径。",
            code="LocalLanguagePairUnsupported",
        )

    def ensure_model(
        self,
        source_language: str,
        target_language: str,
        progress: Optional[ProgressCallback] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Path:
        spec = self.spec_for(source_language, target_language)
        return self.ensure_spec(spec, progress, cancel_event)

    def ensure_spec(
        self,
        spec: LocalModelSpec,
        progress: Optional[ProgressCallback] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Path:
        destination = self.root / spec.pair_name
        if self._valid_model_directory(
            destination,
            spec.source_language,
            spec.target_language,
        ):
            return destination

        with self._lock:
            if self._valid_model_directory(
                destination,
                spec.source_language,
                spec.target_language,
            ):
                return destination
            self.root.mkdir(parents=True, exist_ok=True)
            archive_path = self.root / f".{spec.archive_name}.part"
            try:
                self._download(spec, archive_path, progress, cancel_event)
                self._verify_archive(archive_path, spec)
                return self._install_archive(archive_path, destination, spec)
            finally:
                try:
                    archive_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def remove(self, source_language: str, target_language: str) -> None:
        directory = self.model_directory(source_language, target_language)
        with self._lock:
            if directory.exists():
                shutil.rmtree(directory)

    def remove_all(self) -> None:
        with self._lock:
            for source, target in self.installed_pairs():
                directory = self.model_directory(source, target)
                if directory.is_dir():
                    shutil.rmtree(directory)

    def _fetch_catalog(
        self,
        cancel_event: Optional[threading.Event],
    ) -> object:
        errors: list[str] = []
        for url in _CATALOG_URLS:
            self._check_cancel(cancel_event)
            try:
                request = urllib.request.Request(
                    url,
                    headers={"User-Agent": "ScreenshotTool/1.5"},
                )
                with self.opener(request, timeout=12) as response:
                    data = response.read(2 * 1024 * 1024 + 1)
                if len(data) > 2 * 1024 * 1024:
                    raise ValueError("模型目录响应过大")
                index_payload = json.loads(data.decode("utf-8"))
                self._check_cancel(cancel_event)
                try:
                    manifest_request = urllib.request.Request(
                        _HF_MANIFEST_URL,
                        headers={"User-Agent": "ScreenshotTool/1.5"},
                    )
                    with self.opener(manifest_request, timeout=12) as manifest_response:
                        manifest_data = manifest_response.read(2 * 1024 * 1024 + 1)
                    self._check_cancel(cancel_event)
                    manifest_payload = (
                        json.loads(manifest_data.decode("utf-8"))
                        if len(manifest_data) <= 2 * 1024 * 1024
                        else []
                    )
                except (OSError, ValueError, urllib.error.URLError):
                    manifest_payload = []
                return {"index": index_payload, "files": manifest_payload}
            except TranslationCancelled:
                raise
            except (OSError, ValueError, urllib.error.URLError) as exc:
                errors.append(str(exc))
        raise TranslationError(
            f"刷新模型目录失败：{errors[-1] if errors else '网络不可用'}",
            code="LocalCatalogUnavailable",
        )

    def _load_catalog_cache(self) -> dict[tuple[str, str], LocalModelSpec]:
        try:
            payload = json.loads(self.catalog_cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return self._parse_catalog(payload)

    def _save_catalog_cache(self, payload: object) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.catalog_cache_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(self.catalog_cache_path)

    @classmethod
    def _parse_catalog(cls, payload: object) -> dict[tuple[str, str], LocalModelSpec]:
        parsed: dict[tuple[str, str], LocalModelSpec] = {}
        if isinstance(payload, dict):
            entries = payload.get("index", ())
            files = payload.get("files", ())
        else:
            entries = payload
            files = ()
        file_metadata: dict[str, tuple[int, str]] = {}
        for item in files if isinstance(files, list) else ():
            if not isinstance(item, dict) or item.get("type") != "file":
                continue
            name = Path(str(item.get("path", ""))).name
            lfs = item.get("lfs", {})
            sha256 = str(lfs.get("oid", "")) if isinstance(lfs, dict) else ""
            try:
                size = int(item.get("size", 0) or 0)
            except (TypeError, ValueError):
                size = 0
            if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
                sha256 = ""
            if size < 0 or size > cls.MAX_DOWNLOAD_BYTES:
                size = 0
            if name.endswith(".argosmodel"):
                file_metadata[name] = size, sha256

        for item in entries if isinstance(entries, list) else ():
            if not isinstance(item, dict) or item.get("type", "translate") != "translate":
                continue
            source = cls._normalize_code(item.get("from_code", ""))
            target = cls._normalize_code(item.get("to_code", ""))
            if not all(re.fullmatch(r"[a-z]{2,3}", code) for code in (source, target)):
                continue
            links = tuple(
                str(link)
                for link in item.get("links", ())
                if str(link).startswith(("https://", "http://"))
            )
            archive_name = next(
                (
                    Path(urllib.parse.urlparse(link).path).name
                    for link in links
                    if Path(urllib.parse.urlparse(link).path).name.endswith(".argosmodel")
                ),
                f"translate-{source}_{target}.argosmodel",
            )
            mirrors = (
                f"{_HF_MIRROR_ROOT}/{archive_name}",
                f"{_HF_ROOT}/{archive_name}",
                *links,
                f"{_LIBRE_MODELS_ROOT}/{source}_{target}.argosmodel",
            )
            urls = tuple(dict.fromkeys(mirrors))
            download_size, sha256 = file_metadata.get(archive_name, (0, ""))
            spec = LocalModelSpec(
                source,
                target,
                archive_name,
                sha256,
                download_size,
                urls,
                str(item.get("from_name", source)),
                str(item.get("to_name", target)),
                str(item.get("package_version", "")),
            )
            key = source, target
            current = parsed.get(key)
            if current is None or cls._version_key(spec.package_version) > cls._version_key(
                current.package_version
            ):
                parsed[key] = spec
        return parsed

    @staticmethod
    def _sorted_specs(specs: object) -> tuple[LocalModelSpec, ...]:
        return tuple(
            sorted(
                specs,
                key=lambda item: (
                    item.source_name.casefold(),
                    item.target_name.casefold(),
                    item.source_language,
                    item.target_language,
                ),
            )
        )

    @staticmethod
    def _version_key(value: str) -> tuple[int, ...]:
        numbers = re.findall(r"\d+", str(value))
        return tuple(int(number) for number in numbers) or (0,)

    @staticmethod
    def _normalize_code(value: object) -> str:
        aliases = {"zh-tw": "zt", "zh-hk": "zt"}
        code = str(value or "").strip().lower()
        return aliases.get(code, code)

    def _download(
        self,
        spec: LocalModelSpec,
        target: Path,
        progress: Optional[ProgressCallback],
        cancel_event: Optional[threading.Event],
    ) -> None:
        errors: list[str] = []
        for url in spec.urls:
            self._check_cancel(cancel_event)
            try:
                request = urllib.request.Request(
                    url,
                    headers={"User-Agent": "ScreenshotTool/1.5"},
                )
                with self.opener(request, timeout=60) as response, target.open("wb") as output:
                    total = int(response.headers.get("Content-Length", 0) or 0)
                    if total > self.MAX_DOWNLOAD_BYTES:
                        raise TranslationError(
                            "本地翻译模型超过允许的最大下载大小。",
                            code="LocalModelTooLarge",
                        )
                    received = 0
                    while True:
                        self._check_cancel(cancel_event)
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        received += len(chunk)
                        if received > self.MAX_DOWNLOAD_BYTES:
                            raise TranslationError(
                                "本地翻译模型超过允许的最大下载大小。",
                                code="LocalModelTooLarge",
                            )
                        if progress:
                            expected = total or spec.download_size
                            if expected:
                                detail = f"{min(99, int(received * 100 / expected))}%"
                            else:
                                detail = f"{received / (1024 * 1024):.1f} MB"
                            progress(
                                f"正在下载本地翻译模型 {spec.source_language}→"
                                f"{spec.target_language} · {detail}"
                            )
                return
            except TranslationCancelled:
                raise
            except (OSError, ValueError, urllib.error.URLError) as exc:
                errors.append(str(exc))
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
        detail = errors[-1] if errors else "没有可用下载地址"
        raise TranslationError(
            f"本地翻译模型下载失败：{detail}",
            code="LocalModelDownloadFailed",
        )

    @staticmethod
    def _verify_archive(path: Path, spec: LocalModelSpec) -> None:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        if spec.sha256 and digest.hexdigest().lower() != spec.sha256.lower():
            raise TranslationError(
                "本地翻译模型校验失败，已放弃安装。",
                code="LocalModelChecksumMismatch",
            )

    def _install_archive(
        self,
        archive_path: Path,
        destination: Path,
        spec: LocalModelSpec,
    ) -> Path:
        temporary_root = Path(
            tempfile.mkdtemp(prefix=f".{spec.pair_name}-", dir=str(self.root))
        )
        try:
            with zipfile.ZipFile(archive_path) as archive:
                members = archive.infolist()
                if (
                    len(members) > self.MAX_ARCHIVE_MEMBERS
                    or sum(item.file_size for item in members) > self.MAX_EXTRACTED_BYTES
                ):
                    raise TranslationError(
                        "本地翻译模型压缩包异常。",
                        code="LocalModelInvalidArchive",
                    )
                for member in members:
                    self._extract_member(archive, member, temporary_root)

            metadata_paths = list(temporary_root.rglob("metadata.json"))
            candidates = [
                item.parent
                for item in metadata_paths
                if self._valid_model_directory(
                    item.parent,
                    spec.source_language,
                    spec.target_language,
                )
            ]
            if len(candidates) != 1:
                raise TranslationError(
                    "本地翻译模型内容不完整。",
                    code="LocalModelInvalidArchive",
                )
            if destination.exists():
                shutil.rmtree(destination)
            candidates[0].replace(destination)
            return destination
        except (OSError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
            raise TranslationError(
                f"安装本地翻译模型失败：{exc}",
                code="LocalModelInstallFailed",
            ) from exc
        finally:
            shutil.rmtree(temporary_root, ignore_errors=True)

    @staticmethod
    def _extract_member(
        archive: zipfile.ZipFile,
        member: zipfile.ZipInfo,
        destination: Path,
    ) -> None:
        relative = PurePosixPath(member.filename.replace("\\", "/"))
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or any(":" in part for part in relative.parts)
        ):
            raise TranslationError(
                "本地翻译模型包含不安全路径。",
                code="LocalModelInvalidArchive",
            )
        file_type = (member.external_attr >> 16) & 0o170000
        if file_type == stat.S_IFLNK:
            raise TranslationError(
                "本地翻译模型包含不支持的链接。",
                code="LocalModelInvalidArchive",
            )
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            return
        with archive.open(member) as source, target.open("wb") as output:
            shutil.copyfileobj(source, output, 1024 * 1024)

    @staticmethod
    def _valid_model_directory(directory: Path, source: str, target: str) -> bool:
        metadata_path = directory / "metadata.json"
        if not (
            metadata_path.is_file()
            and (directory / "sentencepiece.model").is_file()
            and (directory / "model" / "model.bin").is_file()
        ):
            return False
        metadata = LocalModelManager._read_model_metadata(directory)
        return metadata.get("from_code") == source and metadata.get("to_code") == target

    @staticmethod
    def _read_model_metadata(directory: Path) -> dict:
        try:
            value = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _check_cancel(cancel_event: Optional[threading.Event]) -> None:
        if cancel_event and cancel_event.is_set():
            raise TranslationCancelled()


class LocalCTranslateProvider:
    """Runs verified OPUS-MT models locally through CTranslate2."""

    MAX_SOURCE_TOKENS = 180
    provider_name = "OPUS-MT/CTranslate2（本地）"

    def __init__(self, model_manager: Optional[LocalModelManager] = None) -> None:
        self.model_manager = model_manager or LocalModelManager()
        self._engines: dict[tuple[str, str], tuple[object, object]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def is_available() -> bool:
        try:
            importlib.import_module("ctranslate2")
            importlib.import_module("sentencepiece")
        except (AttributeError, ImportError, OSError):
            return False
        return True

    @classmethod
    def detect_source_language(cls, text: str, target_language: str) -> str:
        value = str(text or "")
        if re.search(r"[\u3040-\u30ff]", value):
            source = "ja"
        elif re.search(r"[\uac00-\ud7af]", value):
            source = "ko"
        elif re.search(r"[ІіЇїЄєҐґ]", value):
            source = "uk"
        elif re.search(r"[\u0400-\u04ff]", value):
            source = "ru"
        elif re.search(r"[\u0600-\u06ff]", value):
            source = "ar"
        elif re.search(r"[\u0590-\u05ff]", value):
            source = "he"
        elif re.search(r"[\u0370-\u03ff]", value):
            source = "el"
        elif re.search(r"[\u0900-\u097f]", value):
            source = "hi"
        elif re.search(r"[\u0980-\u09ff]", value):
            source = "bn"
        elif re.search(r"[\u0e00-\u0e7f]", value):
            source = "th"
        else:
            source = ""
        chinese_count = len(re.findall(r"[\u3400-\u9fff]", value))
        latin_count = len(re.findall(r"[A-Za-z]", value))
        if not source:
            if chinese_count > latin_count:
                source = "zh"
            elif latin_count:
                source = "en"
            else:
                source = "en" if target_language != "en" else "zh"
        normalized_target = LocalModelManager._normalize_code(target_language)
        if source == normalized_target:
            raise TranslationError(
                "识别出的源语言与目标语言相同，请切换目标语言。",
                code="LocalSourceEqualsTarget",
            )
        return source

    def translate_texts(
        self,
        texts: Sequence[str],
        target_language: str,
        source_language: str = "auto",
        progress: Optional[ProgressCallback] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> tuple[list[str], str, list[str]]:
        values = [str(text).strip() for text in texts if str(text).strip()]
        if not values:
            return [], source_language, []
        target = LocalModelManager._normalize_code(target_language)
        detected_source = (
            self.detect_source_language("\n".join(values), target)
            if source_language in {"", "auto"}
            else LocalModelManager._normalize_code(source_language)
        )
        self._check_cancel(cancel_event)
        if not self.is_available():
            raise TranslationError(
                "本地翻译组件未安装，请安装 ctranslate2 和 sentencepiece。",
                code="LocalRuntimeUnavailable",
            )
        route = self.model_manager.resolve_route(detected_source, target)
        translations = values
        for index, spec in enumerate(route, start=1):
            self._check_cancel(cancel_event)
            if progress:
                progress(
                    f"正在准备本地模型 {spec.source_language}→{spec.target_language} "
                    f"({index}/{len(route)})..."
                )
            model_directory = self.model_manager.ensure_spec(
                spec,
                progress,
                cancel_event,
            )
            if progress:
                progress(
                    f"正在本机翻译 {spec.source_language}→{spec.target_language} "
                    f"({index}/{len(route)})..."
                )
            translator, tokenizer = self._engine(
                spec.source_language,
                spec.target_language,
                model_directory,
            )
            translations = self._translate_batch(
                translator,
                tokenizer,
                translations,
                spec.target_language,
                cancel_event,
            )
        if progress:
            progress("本地翻译完成，正在生成译图...")
        return translations, detected_source, []

    def _engine(
        self,
        source_language: str,
        target_language: str,
        model_directory: Path,
    ) -> tuple[object, object]:
        key = source_language, target_language
        with self._lock:
            cached = self._engines.get(key)
            if cached is not None:
                return cached
            ctranslate2 = importlib.import_module("ctranslate2")
            sentencepiece = importlib.import_module("sentencepiece")
            translator = ctranslate2.Translator(
                str(model_directory / "model"),
                device="cpu",
                compute_type="auto",
                inter_threads=1,
                intra_threads=0,
            )
            tokenizer = sentencepiece.SentencePieceProcessor(
                model_file=str(model_directory / "sentencepiece.model")
            )
            self._engines[key] = translator, tokenizer
            return translator, tokenizer

    def _translate_batch(
        self,
        translator: object,
        tokenizer: object,
        values: Sequence[str],
        target_language: str,
        cancel_event: Optional[threading.Event],
    ) -> list[str]:
        token_batches: list[list[str]] = []
        chunk_counts: list[int] = []
        for value in values:
            tokens = list(tokenizer.encode(value, out_type=str))
            chunks = [
                tokens[index : index + self.MAX_SOURCE_TOKENS]
                for index in range(0, len(tokens), self.MAX_SOURCE_TOKENS)
            ] or [[]]
            token_batches.extend(chunks)
            chunk_counts.append(len(chunks))

        self._check_cancel(cancel_event)
        with self._lock:
            results = translator.translate_batch(
                token_batches,
                beam_size=4,
                max_batch_size=32,
                batch_type="tokens",
                replace_unknowns=True,
                no_repeat_ngram_size=2,
            )
        self._check_cancel(cancel_event)
        decoded = [
            self._clean_output(
                tokenizer.decode_pieces(result.hypotheses[0]),
                target_language,
            )
            for result in results
        ]
        translations: list[str] = []
        cursor = 0
        separator = "" if target_language == "zh" else " "
        for count in chunk_counts:
            translations.append(separator.join(decoded[cursor : cursor + count]).strip())
            cursor += count
        return translations

    @staticmethod
    def _clean_output(value: object, target_language: str) -> str:
        text = str(value or "").replace("\u2581", " ").replace("_", " ")
        text = re.sub(r"\s+", " ", text).strip()
        if target_language in {"zh", "zt"}:
            text = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", text)
            text = re.sub(r"\s+([，。！？；：、])", r"\1", text)
        return text

    @staticmethod
    def _check_cancel(cancel_event: Optional[threading.Event]) -> None:
        if cancel_event and cancel_event.is_set():
            raise TranslationCancelled()
