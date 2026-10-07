"""Streaming TTS via Alibaba DashScope (PCM chunks straight over a WebSocket).

This fixes the main lag. The old non-realtime API rendered each sentence into a .wav that we
then downloaded (POST+GET ≈ 2.5–3 s per sentence). Now we use DashScope's streaming protocol:
audio arrives as PCM chunks over the socket, with the first sound after tens of ms and no file.

Region intl/Singapore: wss://dashscope-intl.aliyuncs.com/api-ws/v1/...

Env:
    DASHSCOPE_API_KEY        Model Studio key
    QWEN_TTS_VOICE           default Ethan
    QWEN_TTS_LANGUAGE        force a language (English/Russian/...). Empty = pick per turn
                             from the user's transcript (see language_for)
    QWEN_TTS_SAMPLE_RATE     default 24000 (= the client player's rate)
    QWEN_TTS_REALTIME_MODEL  default qwen3-tts-flash-realtime
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import uuid
from typing import AsyncIterator

import websockets


def _cfg() -> dict:
    return {
        "key": os.getenv("DASHSCOPE_API_KEY", ""),
        "url": os.getenv(
            "DASHSCOPE_WS_URL", "wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference/"
        ),
        "model": os.getenv("QWEN_TTS_MODEL", "qwen3-tts-flash"),
        "voice": os.getenv("QWEN_TTS_VOICE", "Ethan"),
        "sample_rate": int(os.getenv("QWEN_TTS_SAMPLE_RATE", "24000")),
        # realtime API (the good qwen3-tts lives here; the inference endpoint says ModelNotFound)
        "rt_url": os.getenv(
            "QWEN_TTS_REALTIME_URL",
            "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime",
        ),
        "rt_model": os.getenv("QWEN_TTS_REALTIME_MODEL", "qwen3-tts-flash-realtime"),
        # language_type is documented and defaults to "Auto": the model guesses the language
        # of every fragment separately and gets confused on short sentences. Allowed: Auto/
        # Chinese/English/German/Italian/Portuguese/Spanish/Japanese/Korean/French/Russian.
        "language": os.getenv("QWEN_TTS_LANGUAGE", "").strip(),
    }


_CYRILLIC = re.compile(r"[а-яё]", re.I)


def language_for(text: str) -> str:
    """language_type for this turn: forced by QWEN_TTS_LANGUAGE, otherwise matched to the
    script the user spoke in (the agent answers in the user's language)."""
    forced = _cfg()["language"]
    if forced:
        return forced
    return "Russian" if _CYRILLIC.search(text or "") else "English"


def is_configured() -> bool:
    return bool(os.getenv("DASHSCOPE_API_KEY"))


async def stream(text: str, voice: str | None = None) -> AsyncIterator[bytes]:
    """Speak one sentence as a stream: yields PCM16 mono chunks as they are synthesized.
    voice: the session voice (from runtime config); None means the env default.
    Raises RuntimeError on task-failed (the caller catches it and falls back)."""
    c = _cfg()
    if not c["key"]:
        raise RuntimeError("DASHSCOPE_API_KEY is not set")

    task_id = uuid.uuid4().hex[:32]
    run_task = {
        "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
        "payload": {
            "task_group": "audio", "task": "tts", "function": "SpeechSynthesizer",
            "model": c["model"],
            "parameters": {
                "text_type": "PlainText",
                "voice": (voice or c["voice"]),
                "format": "pcm",
                "sample_rate": c["sample_rate"],
            },
            "input": {},
        },
    }
    continue_task = {
        "header": {"action": "continue-task", "task_id": task_id, "streaming": "duplex"},
        "payload": {"input": {"text": text}},
    }
    finish_task = {
        "header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
        "payload": {"input": {}},
    }

    async with websockets.connect(
        c["url"],
        additional_headers={"Authorization": f"Bearer {c['key']}"},
        max_size=None, ping_interval=20, ping_timeout=20,
    ) as ws:
        await ws.send(json.dumps(run_task))
        async for msg in ws:
            if isinstance(msg, (bytes, bytearray)):
                if msg:
                    yield bytes(msg)
                continue
            data = json.loads(msg)
            event = (data.get("header") or {}).get("event")
            if event == "task-started":
                await ws.send(json.dumps(continue_task))
                await ws.send(json.dumps(finish_task))
            elif event == "task-finished":
                break
            elif event == "task-failed":
                payload = data.get("payload") or {}
                raise RuntimeError(
                    f"dashscope tts failed: {payload.get('message') or msg[:200]}"
                )


async def stream_session(
    text_iter: AsyncIterator[str], voice: str | None = None, language: str | None = None
) -> AsyncIterator[bytes]:
    """Speak the WHOLE turn in one session of DashScope's realtime API (qwen3-tts-flash-realtime).
    One socket: session.update → sentences from text_iter go in via append as they become
    ready → finish at the end. Audio comes back in response.audio.delta (base64 PCM16 mono),
    with no gaps between sentences. Raises RuntimeError on an error event; the caller
    catches it and falls back."""
    c = _cfg()
    if not c["key"]:
        raise RuntimeError("DASHSCOPE_API_KEY is not set")

    url = f"{c['rt_url']}?model={c['rt_model']}"
    session_update = {
        "type": "session.update",
        "session": {
            "voice": (voice or c["voice"]),
            "response_format": "pcm",          # allowed: mp3/wav/pcm/opus
            "sample_rate": c["sample_rate"],   # 24k = the client player's rate
            "mode": "server_commit",
            # without this, language_type=Auto guesses per fragment, and we feed short
            # sentences → broken pronunciation
            "language_type": language or c["language"] or "English",
        },
    }

    # close_timeout: DashScope never answers the close frame, and the default 10 s close
    # handshake held "done" back by 10 s after the last audio chunk (the follow-up window
    # and music resume waited for it). By then all audio is received, so don't wait.
    async with websockets.connect(
        url,
        additional_headers={"Authorization": f"bearer {c['key']}"},
        max_size=None, ping_interval=20, ping_timeout=20, close_timeout=0.2,
    ) as ws:
        await ws.send(json.dumps(session_update))

        async def feed() -> None:
            # send sentences as they become ready (server_commit decides when to
            # synthesize), then close the session
            async for text in text_iter:
                if text:
                    await ws.send(json.dumps(
                        {"type": "input_text_buffer.append", "text": text}))
            await ws.send(json.dumps({"type": "session.finish"}))

        feeder: asyncio.Task = asyncio.create_task(feed())
        try:
            async for msg in ws:
                if isinstance(msg, (bytes, bytearray)):
                    # realtime sends audio as base64 inside JSON, but pass raw binary through too
                    if msg:
                        yield bytes(msg)
                    continue
                data = json.loads(msg)
                etype = data.get("type") or ""
                if etype == "response.audio.delta":
                    b64 = data.get("delta") or data.get("audio") or ""
                    if b64:
                        yield base64.b64decode(b64)
                elif etype == "session.finished":
                    # NOT response.done: with server_commit a long answer can be split into
                    # several responses, and stopping at the first one cut the rest off.
                    # session.finished comes once everything sent before session.finish is spoken.
                    break
                elif etype == "error" or etype.endswith(".failed"):
                    err = data.get("error") or data
                    raise RuntimeError(f"qwen realtime tts error: {str(err)[:200]}")
        finally:
            if not feeder.done():
                feeder.cancel()
                try:
                    await feeder
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
