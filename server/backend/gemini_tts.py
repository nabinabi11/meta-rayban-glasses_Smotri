"""gemini_tts.py: TTS via Gemini (generativelanguage API). Returns PCM16/24k/mono,
exactly the client player's format, no conversion. Natural intonation.

The model is request/response (not streaming), so we synthesize sentence by sentence:
better intonation, slower first sound than qwen realtime. If the API is blocked in
your server's region, set GEMINI_PROXY (or AUDIO_PROXY).

Env:
    GEMINI_API_KEY      AI Studio key
    GEMINI_PROXY        http://user:pass@host:port (or AUDIO_PROXY)
    GEMINI_TTS_MODEL    default gemini-2.5-flash-preview-tts
    GEMINI_TTS_VOICE    default Charon. Others: Orus, Puck, Fenrir, Kore…
"""

from __future__ import annotations

import base64
import os

import httpx

_BASE = "https://generativelanguage.googleapis.com/v1beta"


def is_configured() -> bool:
    return bool(os.getenv("GEMINI_API_KEY"))


def _proxy() -> str | None:
    return os.getenv("GEMINI_PROXY") or os.getenv("AUDIO_PROXY") or None


async def synth(text: str, voice: str | None = None) -> bytes:
    """Synthesize one sentence → raw PCM16/24k/mono. Error/empty → RuntimeError (caller catches)."""
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    model = os.getenv("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts")
    v = voice or os.getenv("GEMINI_TTS_VOICE", "Charon")
    body = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": v}}},
        },
    }
    url = f"{_BASE}/models/{model}:generateContent?key={key}"
    async with httpx.AsyncClient(timeout=30.0, proxy=_proxy()) as c:
        r = await c.post(url, json=body)
    if r.status_code != 200:
        raise RuntimeError(f"gemini tts {r.status_code}: {r.text[:200]}")
    parts = (((r.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
    for p in parts:
        d = (p.get("inlineData") or {}).get("data")
        if d:
            return base64.b64decode(d)   # mimeType audio/L16;pcm;rate=24000, no header
    raise RuntimeError("gemini tts: no audio in the response")
