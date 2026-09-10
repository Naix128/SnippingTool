"""Image translation support for ScreenshotTool."""

from .credentials import TencentCredentialVault
from .local import LocalCTranslateProvider, LocalModelManager
from .models import (
    LANGUAGE_LABELS,
    TranslationCancelled,
    TranslationCredentials,
    TranslationDocument,
    TranslationError,
    TranslationRegion,
    language_code_for_label,
    language_label,
)
from .providers import TencentTranslationProvider
from .service import ImageTranslationService

__all__ = [
    "ImageTranslationService",
    "LANGUAGE_LABELS",
    "LocalCTranslateProvider",
    "LocalModelManager",
    "TencentCredentialVault",
    "TencentTranslationProvider",
    "TranslationCancelled",
    "TranslationCredentials",
    "TranslationDocument",
    "TranslationError",
    "TranslationRegion",
    "language_code_for_label",
    "language_label",
]
