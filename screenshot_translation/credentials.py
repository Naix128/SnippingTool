from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import platform
from typing import Optional

from .models import TranslationCredentials, TranslationError


class _CredentialW(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.wintypes.DWORD),
        ("Type", ctypes.wintypes.DWORD),
        ("TargetName", ctypes.wintypes.LPWSTR),
        ("Comment", ctypes.wintypes.LPWSTR),
        ("LastWritten", ctypes.wintypes.FILETIME),
        ("CredentialBlobSize", ctypes.wintypes.DWORD),
        ("CredentialBlob", ctypes.c_void_p),
        ("Persist", ctypes.wintypes.DWORD),
        ("AttributeCount", ctypes.wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.wintypes.LPWSTR),
        ("UserName", ctypes.wintypes.LPWSTR),
    ]


class TencentCredentialVault:
    """Stores Tencent Cloud secrets in the current user's Windows vault."""

    TARGET_NAME = "ScreenshotTool/TencentCloudTMT"
    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168

    def load(self) -> Optional[TranslationCredentials]:
        environment = self._from_environment()
        if environment is not None:
            return environment
        if platform.system() != "Windows":
            return None

        advapi32 = self._advapi32()
        advapi32.CredReadW.argtypes = [
            ctypes.wintypes.LPCWSTR,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CredentialW)),
        ]
        advapi32.CredReadW.restype = ctypes.wintypes.BOOL
        advapi32.CredFree.argtypes = [ctypes.c_void_p]
        advapi32.CredFree.restype = None

        pointer = ctypes.POINTER(_CredentialW)()
        if not advapi32.CredReadW(
            self.TARGET_NAME,
            self.CRED_TYPE_GENERIC,
            0,
            ctypes.byref(pointer),
        ):
            error = ctypes.get_last_error()
            if error in {0, self.ERROR_NOT_FOUND}:
                return None
            raise TranslationError(f"读取 Windows 凭据失败：{ctypes.WinError(error)}")

        try:
            credential = pointer.contents
            secret_id = credential.UserName or ""
            secret_key = (
                ctypes.string_at(
                    credential.CredentialBlob,
                    credential.CredentialBlobSize,
                ).decode("utf-16-le")
                if credential.CredentialBlob and credential.CredentialBlobSize
                else ""
            )
            value = TranslationCredentials(secret_id, secret_key)
            return value if value.complete else None
        finally:
            advapi32.CredFree(pointer)

    def save(self, credentials: TranslationCredentials) -> None:
        if not credentials.complete:
            raise TranslationError("SecretId 和 SecretKey 不能为空。")
        if platform.system() != "Windows":
            raise TranslationError("安全保存云端密钥目前仅支持 Windows。")

        blob = credentials.secret_key.strip().encode("utf-16-le")
        if len(blob) > 2560:
            raise TranslationError("SecretKey 超出 Windows 凭据库允许的长度。")
        blob_buffer = ctypes.create_string_buffer(blob)
        credential = _CredentialW()
        credential.Type = self.CRED_TYPE_GENERIC
        credential.TargetName = self.TARGET_NAME
        credential.Comment = "ScreenshotTool image translation"
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(blob_buffer, ctypes.c_void_p)
        credential.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = credentials.secret_id.strip()
        advapi32 = self._advapi32()
        advapi32.CredWriteW.argtypes = [
            ctypes.POINTER(_CredentialW),
            ctypes.wintypes.DWORD,
        ]
        advapi32.CredWriteW.restype = ctypes.wintypes.BOOL
        ctypes.set_last_error(0)
        if not advapi32.CredWriteW(ctypes.byref(credential), 0):
            error = ctypes.get_last_error()
            raise TranslationError(f"保存 Windows 凭据失败：{ctypes.WinError(error)}")

    def clear(self) -> None:
        if platform.system() != "Windows":
            return
        advapi32 = self._advapi32()
        advapi32.CredDeleteW.argtypes = [
            ctypes.wintypes.LPCWSTR,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD,
        ]
        advapi32.CredDeleteW.restype = ctypes.wintypes.BOOL
        ctypes.set_last_error(0)
        if not advapi32.CredDeleteW(
            self.TARGET_NAME,
            self.CRED_TYPE_GENERIC,
            0,
        ):
            error = ctypes.get_last_error()
            if error not in {0, self.ERROR_NOT_FOUND}:
                raise TranslationError(f"清除 Windows 凭据失败：{ctypes.WinError(error)}")

    def status_text(self) -> str:
        credentials = self.load()
        return f"已配置（{credentials.masked_id}）" if credentials else "未配置"

    @staticmethod
    def _from_environment() -> Optional[TranslationCredentials]:
        credentials = TranslationCredentials(
            os.environ.get("TENCENTCLOUD_SECRET_ID", ""),
            os.environ.get("TENCENTCLOUD_SECRET_KEY", ""),
        )
        return credentials if credentials.complete else None

    @staticmethod
    def _advapi32() -> ctypes.WinDLL:
        return ctypes.WinDLL("Advapi32.dll", use_last_error=True)
