"""Vision for look in describe mode: a VL model captions the frame (OpenAI-compatible).

Default is Qwen3-VL on Alibaba DashScope with the same DASHSCOPE_API_KEY as speech,
so no extra key is needed. Any OpenAI-compatible VL endpoint works.
Without a key we return None and the orchestrator falls back.

Env:
    VISION_API_KEY     optional; defaults to DASHSCOPE_API_KEY
    VISION_BASE_URL    default https://dashscope-intl.aliyuncs.com/compatible-mode/v1
    VISION_MODEL       default qwen3-vl-plus (qwen-vl-max for quality)
    VISION_MAX_TOKENS  default 400
    VISION_TIMEOUT     default 30
"""

from __future__ import annotations

import base64
import os

import httpx


def _key() -> str:
    return os.getenv("VISION_API_KEY") or os.getenv("DASHSCOPE_API_KEY", "")


def is_configured() -> bool:
    return bool(_key())


async def describe(frame_jpeg: bytes, prompt: str) -> str | None:
    """Frame (JPEG) + request → short caption. None if the provider isn't configured or failed."""
    key = _key()
    if not key:
        return None
    base = os.getenv(
        "VISION_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    ).rstrip("/")
    model = os.getenv("VISION_MODEL", "qwen3-vl-plus")
    max_tokens = int(os.getenv("VISION_MAX_TOKENS", "400"))
    timeout = float(os.getenv("VISION_TIMEOUT", "30"))
    data_uri = "data:image/jpeg;base64," + base64.b64encode(frame_jpeg).decode()
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }],
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
            )
            if r.status_code != 200:
                print(f"[vision] {r.status_code}: {r.text[:200]}", flush=True)
                return None
            data = r.json()
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
            if isinstance(content, list):
                content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
            return (content or "").strip() or None
    except Exception as e:  # noqa: BLE001
        print(f"[vision] exception: {e}", flush=True)
        return None
