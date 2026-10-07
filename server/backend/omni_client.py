"""Client for Qwen3-Omni over an OpenAI-compatible endpoint.

Optional last-resort fallback for the senses (STT / captioning / speech) when the
primary engines fail. Provider-swappable: Novita serverless or a self-hosted
vLLM-Omni, switched via OMNI_BASE_URL / OMNI_MODEL / OMNI_API_KEY.

Minimal dependencies: httpx only. No heavy SDK: we want to see the raw protocol
(how frames + audio are sent, how text + speech come back).

Multimodal input format (OpenAI-compatible, as in Qwen-Omni):
    content = [
        {"type": "image_url",    "image_url": {"url": "<url | data:image/jpeg;base64,...>"}},
        {"type": "input_audio",  "input_audio": {"data": "<url | base64>", "format": "wav"}},
        {"type": "text",         "text": "user question"},   # text goes last
    ]

Requesting speech output:
    modalities=["text", "audio"], audio={"voice": "...", "format": "wav"}, stream=True

FIELD NOTES (Novita, 2026-06):
  - audio output is returned ONLY with stream=True; without streaming the speech is
    silently lost (audio_tokens are billed but absent from the response).
  - when streaming: text comes in delta.content, speech in delta.audio.data (base64).
  - the AUDIO is RAW PCM16, 24 kHz, mono (not a WAV container, no RIFF header!)
    despite format="wav". That is exactly our player's format, no re-encoding.
  - the final SSE chunk carries usage and an empty choices=[].
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx


# ── provider config ──────────────────────────────────────────────────────────

@dataclass
class OmniConfig:
    base_url: str = field(
        default_factory=lambda: os.getenv("OMNI_BASE_URL", "https://api.novita.ai/openai")
    )
    api_key: str = field(default_factory=lambda: os.getenv("OMNI_API_KEY", ""))
    model: str = field(
        default_factory=lambda: os.getenv("OMNI_MODEL", "qwen/qwen3-omni-30b-a3b-instruct")
    )
    timeout: float = field(default_factory=lambda: float(os.getenv("OMNI_TIMEOUT", "120")))

    def require_key(self) -> None:
        if not self.api_key:
            raise RuntimeError(
                "OMNI_API_KEY is not set. Put the key in .env (see .env.example) "
                "or export the environment variable."
            )


# ── helpers for multimodal content ───────────────────────────────────────────

def _is_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://") or s.startswith("data:")


def image_part(src: str, mime: str = "image/jpeg") -> dict[str, Any]:
    """Frame: URL, data URI or a local file path (encoded to base64)."""
    if not _is_url(src):
        raw = Path(src).read_bytes()
        src = f"data:{mime};base64," + base64.b64encode(raw).decode()
    return {"type": "image_url", "image_url": {"url": src}}


def audio_part(src: str, fmt: str = "wav") -> dict[str, Any]:
    """Speech: URL, data URI or a local file path.

    IMPORTANT (Novita): audio input is accepted only as a URL or data URI
    (`data:audio/wav;base64,...`). Bare base64 → HTTP 400, so a local file is
    wrapped into a data URI rather than plain base64.
    """
    if not _is_url(src):
        raw = Path(src).read_bytes()
        src = f"data:audio/{fmt};base64," + base64.b64encode(raw).decode()
    return {"type": "input_audio", "input_audio": {"data": src, "format": fmt}}


def video_part(src: str, mime: str = "video/mp4") -> dict[str, Any]:
    """Video (frames + an audio track in one): URL, data URI or a file path.

    The way to send "frame + speech" in one call: video is a single modality that
    carries both picture and sound, sidestepping the "one extra modality" limit.
    """
    if not _is_url(src):
        raw = Path(src).read_bytes()
        src = f"data:{mime};base64," + base64.b64encode(raw).decode()
    return {"type": "video_url", "video_url": {"url": src}}


def text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


# ── response ─────────────────────────────────────────────────────────────────

# Omni output audio rate/format (PCM16 mono): for the player and resampling
OMNI_AUDIO_SAMPLE_RATE = 24000
OMNI_AUDIO_SAMPLE_WIDTH = 2  # bytes (int16)
OMNI_AUDIO_CHANNELS = 1


@dataclass
class OmniDelta:
    """One stream increment: a piece of text and/or a piece of speech (PCM16/24k/mono)."""
    text: str = ""
    audio_pcm: bytes = b""
    usage: dict[str, Any] | None = None  # arrives at the very end
    event: str = ""  # control signal for the client, e.g. "look" (shutter sound)


@dataclass
class OmniReply:
    text: str
    audio: bytes | None  # raw PCM16/24k/mono (concatenated chunks) if speech was requested
    raw: dict[str, Any]  # for debugging ({} in streaming mode; see chat_stream)
    usage: dict[str, Any] | None = None


# ── client ───────────────────────────────────────────────────────────────────

class OmniClient:
    def __init__(self, config: OmniConfig | None = None) -> None:
        self.cfg = config or OmniConfig()

    def _build_payload(
        self,
        content: list[dict[str, Any]],
        *,
        system: str | None,
        history: list[dict[str, Any]] | None,
        want_audio: bool,
        voice: str,
        audio_format: str,
        max_tokens: int,
        temperature: float | None,
        extra_body: dict[str, Any] | None,
        stream: bool,
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": [text_part(system)]})
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": content})

        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if want_audio:
            # IMPORTANT: speech arrives only with stream=True (see the module header)
            payload["modalities"] = ["text", "audio"]
            payload["audio"] = {"voice": voice, "format": audio_format}
        if stream:
            payload["stream_options"] = {"include_usage": True}
        if extra_body:
            payload.update(extra_body)
        return payload

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
        }

    async def chat_stream(
        self,
        content: list[dict[str, Any]],
        *,
        system: str | None = None,
        history: list[dict[str, Any]] | None = None,
        want_audio: bool = False,
        voice: str = "Ethan",
        audio_format: str = "wav",
        max_tokens: int = 1024,
        temperature: float | None = None,
        extra_body: dict[str, Any] | None = None,
    ):
        """Core: streams OmniDelta (text and PCM speech increments) as they are generated.

        `history` holds previous dialogue turns (text messages), inserted between
        system and the current user message.
        """
        self.cfg.require_key()
        payload = self._build_payload(
            content, system=system, history=history, want_audio=want_audio, voice=voice,
            audio_format=audio_format, max_tokens=max_tokens, temperature=temperature,
            extra_body=extra_body, stream=True,
        )
        url = f"{self.cfg.base_url.rstrip('/')}/v1/chat/completions"

        async with httpx.AsyncClient(timeout=self.cfg.timeout) as client:
            async with client.stream("POST", url, headers=self._headers, json=payload) as r:
                if r.status_code != 200:
                    body = (await r.aread()).decode("utf-8", "replace")
                    raise httpx.HTTPStatusError(
                        f"Omni {r.status_code}: {body[:800]}", request=r.request, response=r
                    )
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    obj = _json_loads(data)
                    if obj is None:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        if obj.get("usage"):
                            yield OmniDelta(usage=obj["usage"])
                        continue
                    delta = choices[0].get("delta") or {}
                    text = delta.get("content") or ""
                    if isinstance(text, list):  # in case of the block format
                        text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
                    pcm = b""
                    au = delta.get("audio")
                    if isinstance(au, dict) and au.get("data"):
                        pcm = base64.b64decode(au["data"])
                    if text or pcm:
                        yield OmniDelta(text=text, audio_pcm=pcm)

    async def message(
        self,
        content: list[dict[str, Any]],
        *,
        system: str | None = None,
        history: list[dict[str, Any]] | None = None,
        tools: list[dict] | None = None,
        max_tokens: int = 512,
        temperature: float | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Non-streaming call with tools. Returns (message, usage).
        With modalities=audio Omni stops calling tools, so decisions use a text call."""
        self.cfg.require_key()
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": [text_part(system)]})
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": content})
        payload: dict[str, Any] = {
            "model": self.cfg.model, "messages": messages, "max_tokens": max_tokens,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = tools
        async with httpx.AsyncClient(timeout=self.cfg.timeout) as client:
            r = await client.post(
                f"{self.cfg.base_url.rstrip('/')}/v1/chat/completions",
                headers=self._headers, json=payload,
            )
            if r.status_code != 200:
                raise httpx.HTTPStatusError(
                    f"Omni {r.status_code}: {r.text[:400]}", request=r.request, response=r
                )
            data = r.json()
        return data["choices"][0]["message"], data.get("usage")

    async def chat(
        self,
        content: list[dict[str, Any]],
        *,
        system: str | None = None,
        history: list[dict[str, Any]] | None = None,
        want_audio: bool = False,
        voice: str = "Ethan",
        audio_format: str = "wav",
        max_tokens: int = 1024,
        temperature: float | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> OmniReply:
        """Convenience aggregator over chat_stream: collects all text and speech."""
        parts: list[str] = []
        pcm_chunks: list[bytes] = []
        usage: dict[str, Any] | None = None
        async for d in self.chat_stream(
            content, system=system, history=history, want_audio=want_audio, voice=voice,
            audio_format=audio_format, max_tokens=max_tokens, temperature=temperature,
            extra_body=extra_body,
        ):
            if d.text:
                parts.append(d.text)
            if d.audio_pcm:
                pcm_chunks.append(d.audio_pcm)
            if d.usage:
                usage = d.usage
        audio = b"".join(pcm_chunks) if pcm_chunks else None
        return OmniReply(text="".join(parts), audio=audio, raw={}, usage=usage)


def _json_loads(s: str) -> dict[str, Any] | None:
    import json
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        return None
