"""Voice transcription providers (Groq and OpenAI Whisper)."""

import asyncio
from collections.abc import Callable
import mimetypes
import os
import ssl
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

_TRANSCRIPTIONS_PATH = "audio/transcriptions"
_MAX_RETRIES = 3
_REQUEST_TIMEOUT_S = 60.0
_BACKOFF_S = (1.0, 2.0, 4.0)
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
_RETRYABLE_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)
_AUDIO_MIME_OVERRIDES = {
    ".m4a": "audio/mp4",
    ".mpga": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".weba": "audio/webm",
    ".webm": "audio/webm",
}


class TranscriptionError(Exception):
    """Why a transcription failed (custom). transcribe() returns "" instead unless it is asked to raise
    this: the dashboard's voice route shows the reason, the channels only log it."""


def is_certificate_error(exc: BaseException | None) -> bool:
    """Whether a request failed because the server's certificate did not verify (custom). Trying again
    cannot help: an antivirus that inspects HTTPS (Kaspersky, for one) signs sites with its own root,
    which Python does not trust. httpx keeps the ssl error a few links down the exception chain."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, ssl.SSLCertVerificationError):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


def _resolve_transcription_url(api_base: str | None, default_url: str) -> str:
    """Resolve transcription endpoint without double-appending the path."""
    if not api_base:
        return default_url
    base = api_base.rstrip("/")
    if base.endswith(_TRANSCRIPTIONS_PATH) or base.endswith("/transcriptions"):
        return base
    return f"{base}/{_TRANSCRIPTIONS_PATH}"


def _audio_mime_type(path: Path) -> str:
    return (
        _AUDIO_MIME_OVERRIDES.get(path.suffix.lower())
        or mimetypes.guess_type(path.name)[0]
        or "application/octet-stream"
    )


def _text_from_transcription_payload(payload: dict[str, Any]) -> str:
    text = payload.get("text")
    return text if isinstance(text, str) else ""


async def _post_with_retry(
    build_request: Callable[[], dict[str, Any]],
    provider_label: str,
    extract_text: Callable[[dict[str, Any]], str],
) -> str:
    """Post, retrying what may pass on another try. A failure is logged here, then raised as
    TranscriptionError saying why (custom: it returned "" before)."""
    for attempt in range(_MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(**build_request())
        except _RETRYABLE_EXCEPTIONS as e:
            if is_certificate_error(e):  # fails the same way on every try: no retry (custom)
                logger.error("{} transcription error: the server's certificate does not verify: {}",
                             provider_label, e)
                raise TranscriptionError(str(e)) from e
            if attempt < _MAX_RETRIES:
                logger.warning(
                    "{} transcription transient error (attempt {}/{}): {}",
                    provider_label,
                    attempt + 1,
                    _MAX_RETRIES + 1,
                    e,
                )
                await asyncio.sleep(_BACKOFF_S[attempt])
                continue
            logger.exception(
                "{} transcription error after {} attempts: {}",
                provider_label,
                _MAX_RETRIES + 1,
                e,
            )
            raise TranscriptionError(str(e) or type(e).__name__) from e
        except Exception as e:
            logger.exception("{} transcription error: {}", provider_label, e)
            raise TranscriptionError(str(e) or type(e).__name__) from e

        if response.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
            logger.warning(
                "{} transcription transient HTTP {} (attempt {}/{})",
                provider_label,
                response.status_code,
                attempt + 1,
                _MAX_RETRIES + 1,
            )
            await asyncio.sleep(_BACKOFF_S[attempt])
            continue

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            body = response.text.strip().replace("\n", " ")[:500]
            status = f"HTTP {response.status_code} {response.reason_phrase or ''}".strip()
            logger.error("{} transcription {}{}", provider_label, status, f": {body}" if body else "")
            raise TranscriptionError(status) from e
        except Exception as e:
            logger.exception("{} transcription error: {}", provider_label, e)
            raise TranscriptionError(str(e) or type(e).__name__) from e

        try:
            payload = response.json()
        except Exception as e:
            logger.exception("{} transcription error: malformed response body: {}", provider_label, e)
            raise TranscriptionError("malformed response body") from e
        if not isinstance(payload, dict):
            logger.error(
                "{} transcription error: unexpected response shape: {!r}",
                provider_label,
                type(payload).__name__,
            )
            raise TranscriptionError("unexpected response shape")
        return extract_text(payload)
    return ""


async def _post_transcription_with_retry(
    url: str,
    *,
    api_key: str | None,
    path: Path,
    model: str,
    provider_label: str,
    language: str | None = None,
) -> str:
    try:
        data = path.read_bytes()
    except OSError as e:
        logger.exception("{} transcription error: cannot read audio file: {}", provider_label, e)
        raise TranscriptionError(f"cannot read audio file: {e}") from e
    headers = {"Authorization": f"Bearer {api_key}"}

    def build_request() -> dict[str, Any]:
        files = {
            "file": (path.name, data, _audio_mime_type(path)),
            "model": (None, model),
        }
        if language:
            files["language"] = (None, language)
        return {"url": url, "headers": headers, "files": files, "timeout": _REQUEST_TIMEOUT_S}

    return await _post_with_retry(build_request, provider_label, _text_from_transcription_payload)


class OpenAITranscriptionProvider:
    """Voice transcription provider using OpenAI's Whisper API."""

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        model: str | None = None,
        language: str | None = None,
    ):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.api_url = _resolve_transcription_url(
            api_base or os.environ.get("OPENAI_TRANSCRIPTION_BASE_URL"),
            "https://api.openai.com/v1/audio/transcriptions",
        )
        self.model = model or "whisper-1"
        self.language = language or None

    async def transcribe(self, file_path: str | Path, *, raise_errors: bool = False) -> str:
        """Transcribe an audio file using OpenAI Whisper. "" when that fails (logged); with raise_errors,
        the TranscriptionError saying why (custom)."""
        try:
            if not self.api_key:
                logger.warning("OpenAI API key not configured for transcription")
                raise TranscriptionError("OpenAI API key not configured")

            path = Path(file_path)
            if not path.exists():
                logger.error("Audio file not found: {}", file_path)
                raise TranscriptionError("audio file not found")
            return await _post_transcription_with_retry(
                self.api_url,
                api_key=self.api_key,
                path=path,
                model=self.model,
                provider_label="OpenAI",
                language=self.language,
            )
        except TranscriptionError:
            if raise_errors:
                raise
            return ""


class GroqTranscriptionProvider:
    """
    Voice transcription provider using Groq's Whisper API.

    Groq offers extremely fast transcription with a generous free tier.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        model: str | None = None,
        language: str | None = None,
    ):
        self.api_key = api_key or os.environ.get("GROQ_API_KEY")
        self.api_url = _resolve_transcription_url(
            api_base or os.environ.get("GROQ_BASE_URL"),
            "https://api.groq.com/openai/v1/audio/transcriptions",
        )
        self.model = model or "whisper-large-v3"
        self.language = language or None

    async def transcribe(self, file_path: str | Path, *, raise_errors: bool = False) -> str:
        """
        Transcribe an audio file using Groq.

        Args:
            file_path: Path to the audio file.
            raise_errors: Raise the TranscriptionError saying why it failed instead of returning ""
                (custom: the dashboard's voice route shows the reason).

        Returns:
            Transcribed text; "" when it fails, which is logged.
        """
        try:
            if not self.api_key:
                logger.warning("Groq API key not configured for transcription")
                raise TranscriptionError("Groq API key not configured")

            path = Path(file_path)
            if not path.exists():
                logger.error("Audio file not found: {}", file_path)
                raise TranscriptionError("audio file not found")
            return await _post_transcription_with_retry(
                self.api_url,
                api_key=self.api_key,
                path=path,
                model=self.model,
                provider_label="Groq",
                language=self.language,
            )
        except TranscriptionError:
            if raise_errors:
                raise
            return ""
