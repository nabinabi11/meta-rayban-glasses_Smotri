"""STT via Alibaba DashScope qwen3-asr-flash.

OpenAI-compatible chat/completions with input_audio, served by a dedicated fast ASR
model. Returns the utterance text.

Docs: https://www.alibabacloud.com/help/en/model-studio/qwen-asr-api-reference
Region intl/Singapore: base = https://dashscope-intl.aliyuncs.com/compatible-mode/v1

Env:
    DASHSCOPE_API_KEY     Model Studio key (same as for qwen_tts)
    DASHSCOPE_COMPAT_BASE default https://dashscope-intl.aliyuncs.com/compatible-mode/v1
    QWEN_ASR_MODEL        default qwen3-asr-flash
    QWEN_ASR_LANGUAGE     empty = auto-detect (default); or en / ru / ... to force
"""

from __future__ import annotations

import base64
import os

import httpx


def _cfg() -> dict:
    return {
        "key": os.getenv("DASHSCOPE_API_KEY", ""),
        "base": os.getenv(
            "DASHSCOPE_COMPAT_BASE", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
        ).rstrip("/"),
        "model": os.getenv("QWEN_ASR_MODEL", "qwen3-asr-flash"),
        "language": os.getenv("QWEN_ASR_LANGUAGE", ""),
        "timeout": float(os.getenv("QWEN_ASR_TIMEOUT", "30")),
    }


def is_configured() -> bool:
    return bool(os.getenv("DASHSCOPE_API_KEY"))


async def transcribe(wav_bytes: bytes) -> str:
    """WAV (PCM16) → utterance text. Raises on error (the caller catches it)."""
    c = _cfg()
    if not c["key"]:
        raise RuntimeError("DASHSCOPE_API_KEY is not set")
    data_uri = "data:audio/wav;base64," + base64.b64encode(wav_bytes).decode()
    asr_options: dict = {"enable_itn": False}
    if c["language"]:
        asr_options["language"] = c["language"]
    payload = {
        "model": c["model"],
        "messages": [
            {"role": "user", "content": [
                {"type": "input_audio", "input_audio": {"data": data_uri}},
            ]}
        ],
        "asr_options": asr_options,
    }
    headers = {"Authorization": f"Bearer {c['key']}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=c["timeout"]) as client:
        r = await client.post(f"{c['base']}/chat/completions", headers=headers, json=payload)
        if r.status_code != 200:
            raise RuntimeError(f"qwen-asr {r.status_code}: {r.text[:300]}")
        data = r.json()
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        # content may be a string or a list of blocks
        if isinstance(content, list):
            content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
        return (content or "").strip()
