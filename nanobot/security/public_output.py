"""Small output filters for the public customer runtime."""

from pathlib import Path
from uuid import uuid4

from loguru import logger


def public_error(exc: BaseException) -> str:
    reference = uuid4().hex[:8]
    logger.opt(exception=(type(exc), exc, exc.__traceback__)).error(
        "Public customer failure [{}]: {}", reference, exc,
    )
    return f"Xin lỗi, hệ thống đang gặp sự cố, anh/chị thử lại sau ít phút. Mã: {reference}"


def redact_workspace(value, workspace: Path):
    """Redact native, slash-normalized and JSON-escaped workspace paths."""
    if isinstance(value, str):
        root = str(workspace)
        for variant in sorted({root, root.replace("\\", "/"), root.replace("\\", "\\\\")}, key=len, reverse=True):
            if variant:
                value = value.replace(variant, "<workspace>")
        return value
    if isinstance(value, list):
        return [redact_workspace(item, workspace) for item in value]
    if isinstance(value, dict):
        return {key: redact_workspace(item, workspace) for key, item in value.items()}
    return value
