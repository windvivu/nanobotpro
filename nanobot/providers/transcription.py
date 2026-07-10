"""Voice transcription providers (Groq and OpenAI Whisper)."""

import asyncio
from collections.abc import Callable
import mimetypes
import os
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
    for attempt in range(_MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(**build_request())
        except _RETRYABLE_EXCEPTIONS as e:
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
            return ""
        except Exception as e:
            logger.exception("{} transcription error: {}", provider_label, e)
            return ""

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
        except httpx.HTTPStatusError:
            body = response.text.strip().replace("\n", " ")[:500]
            logger.error(
                "{} transcription HTTP {}{}{}",
                provider_label,
                response.status_code,
                f" {response.reason_phrase}" if response.reason_phrase else "",
                f": {body}" if body else "",
            )
            return ""
        except Exception as e:
            logger.exception("{} transcription error: {}", provider_label, e)
            return ""

        try:
            payload = response.json()
        except Exception as e:
            logger.exception("{} transcription error: malformed response body: {}", provider_label, e)
            return ""
        if not isinstance(payload, dict):
            logger.error(
                "{} transcription error: unexpected response shape: {!r}",
                provider_label,
                type(payload).__name__,
            )
            return ""
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
        return ""
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

    async def transcribe(self, file_path: str | Path) -> str:
        """Transcribe an audio file using OpenAI Whisper."""
        if not self.api_key:
            logger.warning("OpenAI API key not configured for transcription")
            return ""

        path = Path(file_path)
        if not path.exists():
            logger.error("Audio file not found: {}", file_path)
            return ""
        return await _post_transcription_with_retry(
            self.api_url,
            api_key=self.api_key,
            path=path,
            model=self.model,
            provider_label="OpenAI",
            language=self.language,
        )


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

    async def transcribe(self, file_path: str | Path) -> str:
        """
        Transcribe an audio file using Groq.

        Args:
            file_path: Path to the audio file.

        Returns:
            Transcribed text.
        """
        if not self.api_key:
            logger.warning("Groq API key not configured for transcription")
            return ""

        path = Path(file_path)
        if not path.exists():
            logger.error("Audio file not found: {}", file_path)
            return ""
        return await _post_transcription_with_retry(
            self.api_url,
            api_key=self.api_key,
            path=path,
            model=self.model,
            provider_label="Groq",
            language=self.language,
        )
