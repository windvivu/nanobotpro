"""Image generation provider helpers."""

from __future__ import annotations

import base64
import binascii
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from nanobot.providers.registry import find_by_name
from nanobot.utils.helpers import detect_image_mime

_OPENROUTER_ATTRIBUTION_HEADERS = {
    "HTTP-Referer": "https://github.com/HKUDS/nanobot",
    "X-OpenRouter-Title": "nanobot",
    "X-OpenRouter-Categories": "cli-agent,personal-agent",
}
_DEFAULT_TIMEOUT_S = 120.0
_AIHUBMIX_TIMEOUT_S = 300.0
_AIHUBMIX_ASPECT_RATIO_SIZES = {
    "1:1": "1024x1024",
    "3:4": "1024x1536",
    "9:16": "1024x1536",
    "4:3": "1536x1024",
    "16:9": "1536x1024",
}
_OLLAMA_DEFAULT_SIDE = 1024
_OLLAMA_SIZE_PRESETS = {
    "1K": 1024,
    "2K": 2048,
    "4K": 4096,
}
_OLLAMA_EXPLICIT_SIZE_RE = re.compile(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$")
_OLLAMA_ASPECT_RATIO_RE = re.compile(r"^\s*(\d+)\s*:\s*(\d+)\s*$")


class ImageGenerationError(RuntimeError):
    """Raised when the image generation provider cannot return images."""


@dataclass(frozen=True)
class GeneratedImageResponse:
    """Images and optional text returned by the provider."""

    images: list[str]
    content: str
    raw: dict[str, Any]


def _read_image_b64(path: str | Path) -> tuple[str, str]:
    p = Path(path).expanduser()
    raw = p.read_bytes()
    mime = detect_image_mime(raw)
    if mime is None:
        raise ImageGenerationError(f"unsupported reference image: {p}")
    return mime, base64.b64encode(raw).decode("ascii")


def image_path_to_data_url(path: str | Path) -> str:
    mime, encoded = _read_image_b64(path)
    return f"data:{mime};base64,{encoded}"


def image_path_to_inline_data(path: str | Path) -> dict[str, str]:
    mime, encoded = _read_image_b64(path)
    return {"mimeType": mime, "data": encoded}


def _b64_image_data_url(value: str) -> str:
    encoded = "".join(value.split())
    try:
        raw = base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise ImageGenerationError("generated image payload was not valid base64") from exc
    mime = detect_image_mime(raw)
    if mime is None:
        raise ImageGenerationError("generated image payload was not a supported image")
    return f"data:{mime};base64,{encoded}"


def _aihubmix_size(aspect_ratio: str | None, image_size: str | None) -> str:
    if image_size and "x" in image_size.lower():
        return image_size
    if aspect_ratio in _AIHUBMIX_ASPECT_RATIO_SIZES:
        return _AIHUBMIX_ASPECT_RATIO_SIZES[aspect_ratio]
    return "auto"


def _aihubmix_model_path(model: str) -> str:
    if "/" in model:
        return model
    if model.startswith(("gpt-image-", "dall-e-")):
        return f"openai/{model}"
    return model


async def _download_image_data_url(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(url)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = response.text[:500]
        raise ImageGenerationError(f"failed to download generated image: {detail}") from exc
    raw = response.content
    mime = detect_image_mime(raw)
    if mime is None:
        raise ImageGenerationError("generated image URL did not return a supported image")
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


_IMAGE_GEN_PROVIDERS: dict[str, type["ImageGenerationProvider"]] = {}


def register_image_gen_provider(cls: type["ImageGenerationProvider"]) -> None:
    name = cls.provider_name
    if not name:
        raise ValueError(f"{cls.__name__} must set provider_name")
    _IMAGE_GEN_PROVIDERS[name] = cls


def get_image_gen_provider(name: str) -> type["ImageGenerationProvider"] | None:
    return _IMAGE_GEN_PROVIDERS.get(name)


def image_gen_provider_names() -> tuple[str, ...]:
    return tuple(_IMAGE_GEN_PROVIDERS)


def image_gen_provider_configs(config: Any) -> dict[str, Any]:
    providers_cfg = config.providers
    return {
        name: pc
        for name in _IMAGE_GEN_PROVIDERS
        if (pc := getattr(providers_cfg, name, None)) is not None
    }


class ImageGenerationProvider(ABC):
    """Base class for image generation provider clients."""

    provider_name: str = ""
    missing_key_message: str = ""
    default_timeout: float = _DEFAULT_TIMEOUT_S

    def __init__(
        self,
        *,
        api_key: str | None,
        api_base: str | None = None,
        extra_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
        timeout: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.api_base = self._resolve_base_url(api_base)
        self.extra_headers = extra_headers or {}
        self.extra_body = extra_body or {}
        self.timeout = timeout if timeout is not None else self.default_timeout
        self._client = client

    def _resolve_base_url(self, api_base: str | None) -> str:
        if api_base:
            return api_base.rstrip("/")
        spec = find_by_name(self.provider_name)
        if spec and spec.default_api_base:
            return spec.default_api_base.rstrip("/")
        return self._default_base_url()

    def _default_base_url(self) -> str:
        return ""

    @abstractmethod
    async def generate(
        self,
        *,
        prompt: str,
        model: str,
        reference_images: list[str] | None = None,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
    ) -> GeneratedImageResponse: ...

    def _require_images(self, images: list[str], data: dict[str, Any]) -> None:
        if images:
            return
        provider_error = data.get("error") if isinstance(data, dict) else None
        label = self.provider_name
        if provider_error:
            raise ImageGenerationError(f"{label} returned no images: {provider_error}")
        raise ImageGenerationError(f"{label} returned no images for this request")

    async def _http_post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        body: dict[str, Any],
        client: httpx.AsyncClient | None = None,
    ) -> httpx.Response:
        if client is not None:
            return await client.post(url, headers=headers, json=body)
        if self._client is not None:
            return await self._client.post(url, headers=headers, json=body)
        async with httpx.AsyncClient(timeout=self.timeout) as local_client:
            return await local_client.post(url, headers=headers, json=body)

    async def _download(self, url: str) -> str:
        if self._client is not None:
            return await _download_image_data_url(self._client, url)
        async with httpx.AsyncClient(timeout=self.timeout) as local_client:
            return await _download_image_data_url(local_client, url)


class OpenRouterImageGenerationClient(ImageGenerationProvider):
    provider_name = "openrouter"
    missing_key_message = (
        "OpenRouter API key is not configured. Set providers.openrouter.apiKey."
    )

    async def generate(
        self,
        *,
        prompt: str,
        model: str,
        reference_images: list[str] | None = None,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
    ) -> GeneratedImageResponse:
        if not self.api_key:
            raise ImageGenerationError(self.missing_key_message)

        content_blocks: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for ref in reference_images or []:
            content_blocks.append({"type": "image_url", "image_url": {"url": image_path_to_data_url(ref)}})

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **_OPENROUTER_ATTRIBUTION_HEADERS,
            **self.extra_headers,
        }
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": content_blocks}],
            "modalities": ["image", "text"],
            "image_config": {
                "aspect_ratio": aspect_ratio or "1:1",
                "image_size": image_size or "1K",
            },
        }
        body.update(self.extra_body)

        response = await self._http_post(
            f"{self.api_base}/chat/completions",
            headers=headers,
            body=body,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text[:500]
            raise ImageGenerationError(f"OpenRouter image generation failed: {detail}") from exc

        payload = response.json()
        message = ((payload.get("choices") or [{}])[0] or {}).get("message") or {}
        images = await _images_from_message(message, downloader=self._download)
        self._require_images(images, payload)
        content = message.get("content")
        text = content if isinstance(content, str) else ""
        return GeneratedImageResponse(images=images, content=text, raw=payload)


class OpenAIImageGenerationClient(ImageGenerationProvider):
    provider_name = "openai"
    missing_key_message = "OpenAI API key is not configured. Set providers.openai.apiKey."

    async def generate(
        self,
        *,
        prompt: str,
        model: str,
        reference_images: list[str] | None = None,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
    ) -> GeneratedImageResponse:
        if not self.api_key:
            raise ImageGenerationError(self.missing_key_message)
        if reference_images:
            raise ImageGenerationError("reference images are not supported in the first OpenAI slice")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }
        body: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "response_format": "b64_json",
        }
        if image_size:
            body["size"] = image_size
        body.update(self.extra_body)

        response = await self._http_post(
            f"{self.api_base}/images/generations",
            headers=headers,
            body=body,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text[:500]
            raise ImageGenerationError(f"OpenAI image generation failed: {detail}") from exc

        payload = response.json()
        images = await _openai_images_from_payload(payload, downloader=self._download)
        self._require_images(images, payload)
        return GeneratedImageResponse(images=images, content="", raw=payload)


class AIHubMixImageGenerationClient(ImageGenerationProvider):
    provider_name = "aihubmix"
    default_timeout = _AIHUBMIX_TIMEOUT_S
    missing_key_message = "AiHubMix API key is not configured. Set providers.aihubmix.apiKey."

    async def generate(
        self,
        *,
        prompt: str,
        model: str,
        reference_images: list[str] | None = None,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
    ) -> GeneratedImageResponse:
        if not self.api_key:
            raise ImageGenerationError(self.missing_key_message)
        if reference_images:
            raise ImageGenerationError("reference images are not supported in the first AiHubMix slice")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }
        body: dict[str, Any] = {
            "input": {
                "prompt": prompt,
                "n": 1,
                "size": _aihubmix_size(aspect_ratio, image_size),
                **self.extra_body,
            }
        }
        response = await self._http_post(
            f"{self.api_base}/models/{_aihubmix_model_path(model)}/predictions",
            headers=headers,
            body=body,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text[:500]
            raise ImageGenerationError(f"AiHubMix image generation failed: {detail}") from exc

        payload = response.json()
        images = _aihubmix_images_from_payload(payload)
        self._require_images(images, payload)
        return GeneratedImageResponse(images=images, content="", raw=payload)


class OllamaImageGenerationClient(ImageGenerationProvider):
    provider_name = "ollama"

    async def generate(
        self,
        *,
        prompt: str,
        model: str,
        reference_images: list[str] | None = None,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
    ) -> GeneratedImageResponse:
        if reference_images:
            raise ImageGenerationError("reference images are not supported by the Ollama first slice")

        width, height = _ollama_dimensions(aspect_ratio, image_size)
        headers = {
            "Content-Type": "application/json",
            **self.extra_headers,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "width": width,
            "height": height,
            "steps": 0,
            "stream": False,
            **self.extra_body,
        }
        response = await self._http_post(
            f"{self.api_base.rstrip('/v1')}/api/generate",
            headers=headers,
            body=body,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text[:500]
            raise ImageGenerationError(f"Ollama image generation failed: {detail}") from exc

        payload = response.json()
        image = payload.get("image")
        images = [_b64_image_data_url(image)] if isinstance(image, str) and image else []
        self._require_images(images, payload)
        return GeneratedImageResponse(images=images, content="", raw=payload)


async def _images_from_message(
    message: dict[str, Any],
    *,
    downloader: Any,
) -> list[str]:
    images: list[str] = []
    for item in message.get("images") or []:
        if not isinstance(item, dict):
            continue
        image_url = ((item.get("image_url") or {}).get("url"))
        if not isinstance(image_url, str) or not image_url:
            continue
        if image_url.startswith("data:image/"):
            images.append(image_url)
        elif image_url.startswith("http://") or image_url.startswith("https://"):
            images.append(await downloader(image_url))
        else:
            images.append(_b64_image_data_url(image_url))
    return images


async def _openai_images_from_payload(payload: dict[str, Any], *, downloader: Any) -> list[str]:
    images: list[str] = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        b64 = item.get("b64_json")
        url = item.get("url")
        if isinstance(b64, str) and b64:
            images.append(_b64_image_data_url(b64))
        elif isinstance(url, str) and url:
            images.append(await downloader(url))
    return images


def _aihubmix_images_from_payload(payload: dict[str, Any]) -> list[str]:
    images: list[str] = []
    items = (((payload.get("output") or {}).get("b64_json")) or [])
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get("bytesBase64")
        if isinstance(value, str) and value:
            images.append(_b64_image_data_url(value))
    return images


def _ollama_dimensions(aspect_ratio: str | None, image_size: str | None) -> tuple[int, int]:
    side = _OLLAMA_SIZE_PRESETS.get((image_size or "1K").upper(), _OLLAMA_DEFAULT_SIDE)
    if image_size:
        match = _OLLAMA_EXPLICIT_SIZE_RE.match(image_size)
        if match:
            return int(match.group(1)), int(match.group(2))
    if aspect_ratio:
        match = _OLLAMA_ASPECT_RATIO_RE.match(aspect_ratio)
        if match:
            w = int(match.group(1))
            h = int(match.group(2))
            if w > 0 and h > 0:
                if w >= h:
                    return side, max(1, round(side * h / w))
                return max(1, round(side * w / h)), side
    return side, side


register_image_gen_provider(AIHubMixImageGenerationClient)
register_image_gen_provider(OllamaImageGenerationClient)
register_image_gen_provider(OpenAIImageGenerationClient)
register_image_gen_provider(OpenRouterImageGenerationClient)
