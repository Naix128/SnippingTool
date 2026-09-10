from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import math
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Optional, Protocol, Sequence

from PIL import Image

from .models import (
    TranslationCancelled,
    TranslationCredentials,
    TranslationDocument,
    TranslationError,
    TranslationRegion,
)

ProgressCallback = Callable[[str], None]


class JsonTransport(Protocol):
    def post(
        self,
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout: int,
    ) -> dict:
        ...


class UrllibJsonTransport:
    def post(
        self,
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout: int,
    ) -> dict:
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                parsed = json.loads(payload.decode("utf-8", errors="replace"))
            except (ValueError, TypeError):
                parsed = {}
            detail = _response_error(parsed)
            if detail is not None:
                raise detail from exc
            raise TranslationError(f"翻译服务返回 HTTP {exc.code}。", code=str(exc.code)) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            raise TranslationError("无法连接翻译服务，请检查网络后重试。", code="NetworkError") from exc

        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TranslationError("翻译服务返回了无法解析的数据。", code="InvalidResponse") from exc
        if not isinstance(value, dict):
            raise TranslationError("翻译服务返回格式不正确。", code="InvalidResponse")
        return value


def _response_error(payload: object) -> Optional[TranslationError]:
    if not isinstance(payload, dict):
        return None
    response = payload.get("Response", payload)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    if not isinstance(error, dict):
        return None
    code = str(error.get("Code", ""))
    request_id = str(response.get("RequestId", ""))
    friendly = {
        "AuthFailure.SecretIdNotFound": "SecretId 不存在，请重新配置腾讯云密钥。",
        "AuthFailure.SignatureFailure": "SecretKey 校验失败，请重新配置腾讯云密钥。",
        "FailedOperation.UserNotRegistered": "腾讯云机器翻译服务尚未开通。",
        "FailedOperation.NoFreeAmount": "图片翻译额度已用完。",
        "RequestLimitExceeded": "翻译请求过于频繁，请稍后重试。",
        "LimitExceeded": "翻译额度或调用频率已达到上限。",
        "UnsupportedOperation.UnSupportedTargetLanguage": "当前目标语言不受支持。",
    }.get(code)
    message = friendly or str(error.get("Message", "翻译服务调用失败。"))
    return TranslationError(message, code=code, request_id=request_id)


class TencentCloudSigner:
    ALGORITHM = "TC3-HMAC-SHA256"

    @classmethod
    def headers(
        cls,
        credentials: TranslationCredentials,
        service: str,
        host: str,
        action: str,
        version: str,
        region: str,
        payload: bytes,
        timestamp: Optional[int] = None,
    ) -> dict[str, str]:
        if not credentials.complete:
            raise TranslationError("尚未配置腾讯云翻译密钥。", code="MissingCredentials")
        timestamp = int(time.time() if timestamp is None else timestamp)
        date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
        content_type = "application/json; charset=utf-8"
        canonical_headers = (
            f"content-type:{content_type}\n"
            f"host:{host}\n"
            f"x-tc-action:{action.lower()}\n"
        )
        signed_headers = "content-type;host;x-tc-action"
        hashed_payload = hashlib.sha256(payload).hexdigest()
        canonical_request = "\n".join(
            ["POST", "/", "", canonical_headers, signed_headers, hashed_payload]
        )
        credential_scope = f"{date}/{service}/tc3_request"
        string_to_sign = "\n".join(
            [
                cls.ALGORITHM,
                str(timestamp),
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        secret_date = cls._hmac(("TC3" + credentials.secret_key).encode("utf-8"), date)
        secret_service = cls._hmac(secret_date, service)
        secret_signing = cls._hmac(secret_service, "tc3_request")
        signature = hmac.new(
            secret_signing,
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        authorization = (
            f"{cls.ALGORITHM} Credential={credentials.secret_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return {
            "Authorization": authorization,
            "Content-Type": content_type,
            "Host": host,
            "X-TC-Action": action,
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Version": version,
            "X-TC-Region": region,
            "X-TC-Language": "zh-CN",
        }

    @staticmethod
    def _hmac(key: bytes, message: str) -> bytes:
        return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


class TencentCloudClient:
    SERVICE = "tmt"
    HOST = "tmt.tencentcloudapi.com"
    VERSION = "2018-03-21"
    RETRYABLE_CODES = {
        "NetworkError", "RequestLimitExceeded", "InternalError",
        "InternalError.BackendTimeout", "InternalError.RequestFailed",
        "429", "500", "503",
    }

    def __init__(
        self,
        credentials: TranslationCredentials,
        region: str = "ap-guangzhou",
        timeout_seconds: int = 60,
        transport: Optional[JsonTransport] = None,
        minimum_interval: float = 0.0,
        retry_delays: Sequence[float] = (0.4, 1.0),
        sleep_func: Callable[[float], None] = time.sleep,
    ) -> None:
        self.credentials = credentials
        self.region = str(region or "ap-guangzhou")
        self.timeout_seconds = max(5, int(timeout_seconds))
        self.transport = transport or UrllibJsonTransport()
        self.minimum_interval = max(0.0, float(minimum_interval))
        self.retry_delays = tuple(max(0.0, float(value)) for value in retry_delays)
        self.sleep_func = sleep_func
        self._rate_lock = threading.Lock()
        self._last_request = 0.0

    def call(self, action: str, parameters: dict) -> dict:
        body = json.dumps(
            parameters,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        for attempt in range(len(self.retry_delays) + 1):
            headers = TencentCloudSigner.headers(
                self.credentials, self.SERVICE, self.HOST, action,
                self.VERSION, self.region, body,
            )
            try:
                self._wait_for_rate_limit()
                payload = self.transport.post(
                    f"https://{self.HOST}/", headers, body, self.timeout_seconds
                )
                error = _response_error(payload)
                if error is not None:
                    raise error
                response = payload.get("Response")
                if not isinstance(response, dict):
                    raise TranslationError(
                        "翻译服务缺少 Response 数据。", code="InvalidResponse"
                    )
                return response
            except TranslationError as exc:
                if exc.code not in self.RETRYABLE_CODES or attempt >= len(self.retry_delays):
                    raise
                self.sleep_func(self.retry_delays[attempt])
        raise TranslationError("翻译服务调用失败。", code="RetryExhausted")

    def _wait_for_rate_limit(self) -> None:
        if self.minimum_interval <= 0:
            return
        with self._rate_lock:
            now = time.monotonic()
            remaining = self.minimum_interval - (now - self._last_request)
            if remaining > 0:
                self.sleep_func(remaining)
            self._last_request = time.monotonic()


class CloudImageEncoder:
    MAX_BASE64_BYTES = 9_000_000

    @classmethod
    def encode(cls, image: Image.Image) -> tuple[str, tuple[int, int], str]:
        source = cls._flatten(image)
        png = cls._save(source, "PNG")
        if cls._encoded_size(png) <= cls.MAX_BASE64_BYTES:
            return base64.b64encode(png).decode("ascii"), source.size, "PNG"

        candidate = source
        for _ in range(8):
            for quality in (94, 88, 82, 74, 64, 54):
                jpeg = cls._save(candidate, "JPEG", quality=quality)
                if cls._encoded_size(jpeg) <= cls.MAX_BASE64_BYTES:
                    return base64.b64encode(jpeg).decode("ascii"), candidate.size, "JPEG"
            ratio = math.sqrt(cls.MAX_BASE64_BYTES / max(1, cls._encoded_size(jpeg)))
            ratio = max(0.55, min(0.90, ratio * 0.94))
            next_size = (
                max(1, int(candidate.width * ratio)),
                max(1, int(candidate.height * ratio)),
            )
            if next_size == candidate.size:
                break
            resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
            candidate = candidate.resize(next_size, resampling)
        raise TranslationError("图片过大，压缩后仍超过翻译接口的 9MB 限制。", code="ImageTooLarge")

    @staticmethod
    def _flatten(image: Image.Image) -> Image.Image:
        if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
            rgba = image.convert("RGBA")
            background = Image.new("RGBA", rgba.size, "white")
            return Image.alpha_composite(background, rgba).convert("RGB")
        return image.convert("RGB")

    @staticmethod
    def _save(image: Image.Image, image_format: str, **options: object) -> bytes:
        output = io.BytesIO()
        image.save(output, image_format, optimize=True, **options)
        return output.getvalue()

    @staticmethod
    def _encoded_size(data: bytes) -> int:
        return ((len(data) + 2) // 3) * 4


class TencentTranslationProvider:
    provider_name = "腾讯云 TextTranslate"

    def __init__(
        self,
        credentials: TranslationCredentials,
        region: str = "ap-guangzhou",
        timeout_seconds: int = 75,
        transport: Optional[JsonTransport] = None,
    ) -> None:
        self.image_client = TencentCloudClient(
            credentials,
            region=region,
            timeout_seconds=timeout_seconds,
            transport=transport,
            minimum_interval=1.02,
        )
        self.text_client = TencentCloudClient(
            credentials,
            region=region,
            timeout_seconds=timeout_seconds,
            transport=transport,
            minimum_interval=0.22,
        )
        self.client = self.image_client

    def translate_image(
        self,
        image: Image.Image,
        target_language: str,
        quality_mode: int = 0,
        progress: Optional[ProgressCallback] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> TranslationDocument:
        self._check_cancel(cancel_event)
        if progress:
            progress("正在准备图片...")
        encoded, request_size, _ = CloudImageEncoder.encode(image)
        self._check_cancel(cancel_event)
        if progress:
            progress("正在上传并翻译图片...")
        response = self.image_client.call(
            "ImageTranslateLLM",
            {
                "Data": encoded,
                "Target": target_language,
                "Mode": 1 if int(quality_mode) == 1 else 0,
            },
        )
        self._check_cancel(cancel_event)
        if progress:
            progress("正在整理译图...")
        return self.parse_image_response(response, request_size)

    def translate_texts(
        self,
        texts: Sequence[str],
        target_language: str,
        source_language: str = "auto",
        progress: Optional[ProgressCallback] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> tuple[list[str], str, list[str]]:
        translations: list[str] = []
        detected_source = source_language
        request_ids: list[str] = []
        non_empty = [text.strip() for text in texts if text.strip()]
        total = len(non_empty)
        for index, text in enumerate(non_empty, start=1):
            self._check_cancel(cancel_event)
            if progress:
                progress(f"正在翻译文字 {index}/{total}...")
            translated_parts: list[str] = []
            for chunk in self._text_chunks(text):
                self._check_cancel(cancel_event)
                response = self.text_client.call(
                    "TextTranslate",
                    {
                        "SourceText": chunk,
                        "Source": source_language or "auto",
                        "Target": target_language,
                        "ProjectId": 0,
                    },
                )
                translated_parts.append(str(response.get("TargetText", "")))
                detected_source = str(response.get("Source", detected_source))
                request_id = str(response.get("RequestId", ""))
                if request_id:
                    request_ids.append(request_id)
            translations.append("".join(translated_parts))
        return translations, detected_source, request_ids

    @staticmethod
    def _text_chunks(text: str, limit: int = 1800) -> tuple[str, ...]:
        value = text.strip()
        if not value:
            return ()
        return tuple(value[index : index + limit] for index in range(0, len(value), limit))

    @staticmethod
    def parse_image_response(
        response: dict,
        request_size: tuple[int, int],
    ) -> TranslationDocument:
        encoded = str(response.get("Data", ""))
        for _ in range(2):
            decoded = urllib.parse.unquote(encoded)
            if decoded == encoded:
                break
            encoded = decoded
        try:
            image_data = base64.b64decode(encoded, validate=False)
            translated = Image.open(io.BytesIO(image_data)).convert("RGB")
            translated.load()
        except Exception as exc:
            raise TranslationError("翻译服务没有返回有效的译图。", code="InvalidImage") from exc

        scale_x = translated.width / max(1, request_size[0])
        scale_y = translated.height / max(1, request_size[1])
        details = response.get("TransDetails", [])
        if isinstance(details, dict):
            details = [details]
        regions: list[TranslationRegion] = []
        for item in details if isinstance(details, list) else []:
            if not isinstance(item, dict):
                continue
            polygon = TencentTranslationProvider._detail_polygon(item)
            polygon = tuple(
                (int(round(x * scale_x)), int(round(y * scale_y)))
                for x, y in polygon
            )
            if not polygon:
                continue
            regions.append(
                TranslationRegion(
                    str(item.get("SourceLineText", "")).strip(),
                    str(item.get("TargetLineText", "")).strip(),
                    polygon,
                    int(item.get("LineHeight", 0) or 0),
                    int(item.get("LinesCount", 0) or 0),
                )
            )

        source_text = str(response.get("SourceText", "")).strip()
        target_text = str(response.get("TargetText", "")).strip()
        if not source_text:
            source_text = "\n".join(region.source_text for region in regions if region.source_text)
        if not target_text:
            target_text = "\n".join(region.target_text for region in regions if region.target_text)
        return TranslationDocument(
            translated,
            str(response.get("Source", "auto")),
            str(response.get("Target", "")),
            source_text,
            target_text,
            tuple(regions),
            request_id=str(response.get("RequestId", "")),
            provider="Tencent ImageTranslateLLM",
            angle=float(response.get("Angle", 0.0) or 0.0),
        )

    @staticmethod
    def _detail_polygon(item: dict) -> tuple[tuple[int, int], ...]:
        rotated = item.get("RotateParagraphRect")
        if isinstance(rotated, dict) and rotated.get("Valid"):
            coordinates = rotated.get("Coord", [])
            points = []
            for point in coordinates if isinstance(coordinates, list) else []:
                if isinstance(point, dict):
                    try:
                        points.append((int(point.get("X", 0)), int(point.get("Y", 0))))
                    except (TypeError, ValueError):
                        continue
            if len(points) >= 3:
                return tuple(points)
        box = item.get("BoundingBox", {})
        if not isinstance(box, dict):
            return ()
        try:
            left = int(box.get("X", 0))
            top = int(box.get("Y", 0))
            right = left + int(box.get("Width", 0))
            bottom = top + int(box.get("Height", 0))
        except (TypeError, ValueError):
            return ()
        if right <= left or bottom <= top:
            return ()
        return (left, top), (right, top), (right, bottom), (left, bottom)

    @staticmethod
    def _check_cancel(cancel_event: Optional[threading.Event]) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise TranslationCancelled()
