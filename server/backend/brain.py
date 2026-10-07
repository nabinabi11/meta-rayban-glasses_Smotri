"""The brain: a text LLM with tool calling.

Runs the dialogue, holds memory (via messages), and decides on its OWN when to "look" at
the frame by calling the look tool.

Provider-agnostic: any OpenAI-compatible /chat/completions endpoint (DeepSeek, Qwen on
DashScope, OpenRouter, vLLM, Ollama...). Switched via .env, no code changes.

Minimal dependencies: httpx only.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

# tool executor: (name, args) -> text result
ToolHandler = Callable[[str, dict], Awaitable[str]]


def _brain_key() -> str:
    """BRAIN_API_KEY > DEEPSEEK_API_KEY > DASHSCOPE_API_KEY (when the brain is on DashScope
    too, the whole server runs on a single Alibaba key)."""
    key = os.getenv("BRAIN_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    if not key and "dashscope" in os.getenv("BRAIN_BASE_URL", ""):
        key = os.getenv("DASHSCOPE_API_KEY")
    return key or ""


@dataclass
class BrainConfig:
    base_url: str = field(default_factory=lambda: os.getenv("BRAIN_BASE_URL", "https://api.deepseek.com"))
    api_key: str = field(default_factory=lambda: _brain_key())
    model: str = field(default_factory=lambda: os.getenv("BRAIN_MODEL", "deepseek-v4-flash"))
    provider: str = field(default_factory=lambda: os.getenv("BRAIN_PROVIDER", "deepseek"))
    timeout: float = field(default_factory=lambda: float(os.getenv("BRAIN_TIMEOUT", "60")))

    def require_key(self) -> None:
        if not self.api_key:
            raise RuntimeError("No brain API key: set BRAIN_API_KEY (or DEEPSEEK_API_KEY) in .env")


@dataclass
class BrainResult:
    text: str
    tool_calls: list[str]          # for logs, e.g. ["look({'query': 'what is on the table'})"]
    usage: dict[str, Any] | None = None


def _safe_json(s: str | None) -> dict:
    try:
        return json.loads(s) if s else {}
    except Exception:  # noqa: BLE001
        return {}


def _safe_loads(s: str) -> dict | None:
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        return None


def _has_image(messages: list[dict[str, Any]]) -> bool:
    """Is there at least one image in the messages (multimodal content)?"""
    for m in messages:
        c = m.get("content")
        if isinstance(c, list) and any(
            isinstance(b, dict) and b.get("type") == "image_url" for b in c
        ):
            return True
    return False


class Brain:
    def __init__(self, config: BrainConfig | None = None) -> None:
        self.cfg = config or BrainConfig()

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.cfg.api_key}", "Content-Type": "application/json"}

    async def run(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict] | None = None,
        tool_handler: ToolHandler | None = None,
        max_rounds: int = 4,
        max_tokens: int = 1024,
        temperature: float = 0.6,
    ) -> BrainResult:
        """Run one dialogue turn. If the model calls tools, execute them and continue.
        Returns the final text answer."""
        self.cfg.require_key()
        msgs = list(messages)
        made: list[str] = []
        usage: dict | None = None
        url = f"{self.cfg.base_url.rstrip('/')}/chat/completions"

        async with httpx.AsyncClient(timeout=self.cfg.timeout) as client:
            for _ in range(max_rounds):
                payload: dict[str, Any] = {
                    "model": self.cfg.model,
                    "messages": msgs,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                }
                if tools:
                    payload["tools"] = tools
                r = await client.post(url, headers=self._headers, json=payload)
                if r.status_code != 200:
                    raise RuntimeError(f"Brain {r.status_code}: {r.text[:400]}")
                data = r.json()
                usage = data.get("usage") or usage
                msg = data["choices"][0]["message"]
                tool_calls = msg.get("tool_calls")

                if tool_calls and tool_handler:
                    # the model decided to use a tool (e.g. look)
                    msgs.append({
                        "role": "assistant",
                        "content": msg.get("content") or "",
                        "tool_calls": tool_calls,
                    })
                    for tc in tool_calls:
                        fn = tc["function"]["name"]
                        args = _safe_json(tc["function"].get("arguments"))
                        made.append(f"{fn}({args})")
                        try:
                            result = await tool_handler(fn, args)
                        except Exception as exc:  # noqa: BLE001
                            result = f"ERROR: {exc}"
                        msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                    continue

                return BrainResult(text=(msg.get("content") or "").strip(),
                                   tool_calls=made, usage=usage)

        return BrainResult(text="Sorry, I couldn't come up with an answer.", tool_calls=made, usage=usage)

    async def run_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict] | None = None,
        tool_handler: ToolHandler | None = None,
        stats: dict | None = None,
        max_rounds: int = 4,
        max_tokens: int = 1024,
        temperature: float = 0.6,
        vision_model: str | None = None,
        vision_ctx: dict | None = None,
        model_override: str | None = None,
    ):
        """Streaming turn: yields the final answer text as it is generated.

        Tool rounds (look etc.) run without text output; as soon as the model starts
        writing the answer, it is streamed out token by token. `stats` is filled with
        usage and the list of called tools (for logs).

        HANDING THE TURN TO A VISION MODEL (vision_model + vision_ctx):
        the text model is the orchestrator: it runs the dialogue and decides whether
        to look. If the look handler put a frame into `vision_ctx["image_uri"]`, we
        attach the picture as a separate user message and FROM THEN ON the vision model
        runs the turn: same system prompt, same history, same tools. It answers the
        user from the pixels itself, with no retelling between models. `look` is
        removed from the tools, otherwise the model calls it again and ignores the
        attached frame (verified).
        """
        self.cfg.require_key()
        if stats is None:
            stats = {}
        stats.setdefault("tool_calls", [])
        stats.setdefault("usage", None)
        msgs = list(messages)
        url = f"{self.cfg.base_url.rstrip('/')}/chat/completions"
        cur_model = model_override or self.cfg.model
        # INVARIANT: if the messages contain an image, the model MUST be a vision model,
        # otherwise text-only providers return 400 "model does not support image". This
        # used to depend on the caller remembering to pass model_override.
        if vision_model and _has_image(msgs) and cur_model != vision_model:
            print(f"[brain] image in messages → model {vision_model}", flush=True)
            cur_model = vision_model
        cur_tools = list(tools) if tools else None
        stats["model"] = cur_model

        async with httpx.AsyncClient(timeout=self.cfg.timeout) as client:
            for _ in range(max_rounds):
                payload: dict[str, Any] = {
                    "model": cur_model,
                    "messages": msgs,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                }
                if cur_tools:
                    payload["tools"] = cur_tools

                acc: dict[int, dict] = {}   # index -> {id,name,args}
                round_content = ""
                async with client.stream("POST", url, headers=self._headers, json=payload) as r:
                    if r.status_code != 200:
                        body = (await r.aread()).decode("utf-8", "replace")
                        raise RuntimeError(f"Brain {r.status_code}: {body[:400]}")
                    async for line in r.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        obj = _safe_loads(data)
                        if obj is None:
                            continue
                        if obj.get("usage"):
                            stats["usage"] = obj["usage"]
                        choices = obj.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        content = delta.get("content")
                        if content:
                            round_content += content
                            yield content  # filler/answer text goes out immediately
                        for tcd in (delta.get("tool_calls") or []):
                            i = tcd.get("index", 0)
                            slot = acc.setdefault(i, {"id": "", "name": "", "args": ""})
                            if tcd.get("id"):
                                slot["id"] = tcd["id"]
                            fn = tcd.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["args"] += fn["arguments"]

                # tool calls present: execute and continue (even if there was filler text)
                if acc and tool_handler:
                    tcs = [
                        {"id": s["id"] or f"call_{i}", "type": "function",
                         "function": {"name": s["name"], "arguments": s["args"]}}
                        for i, s in sorted(acc.items())
                    ]
                    msgs.append({"role": "assistant", "content": round_content, "tool_calls": tcs})
                    for tc in tcs:
                        name = tc["function"]["name"]
                        args = _safe_json(tc["function"]["arguments"])
                        stats["tool_calls"].append(f"{name}({args})")
                        try:
                            result = await tool_handler(name, args)
                        except Exception as exc:  # noqa: BLE001
                            result = f"ERROR: {exc}"
                        msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

                    # ── HAND THE TURN TO THE VISION MODEL ────────────────────
                    # look put a frame into vision_ctx → attach the picture and
                    # the vision model runs the rest of the turn, answering from
                    # the pixels in the same context. No retelling.
                    if (vision_ctx and vision_ctx.get("image_uri")
                            and not vision_ctx.get("attached")):
                        msgs.append({"role": "user", "content": [
                            {"type": "image_url",
                             "image_url": {"url": vision_ctx["image_uri"]}},
                            {"type": "text", "text": vision_ctx.get("hint") or (
                                "This is a frame from the glasses' camera: what the user is "
                                "looking at right now. Answer their question from this frame: one "
                                "or two short sentences in the user's language, only what was "
                                "asked, without listing the whole frame: this is spoken aloud.")},
                        ]})
                        vision_ctx["attached"] = True
                        if vision_model:
                            cur_model = vision_model
                            stats["model"] = vision_model
                        # drop look: with the frame attached the model still called
                        # it again and ignored the picture
                        if cur_tools:
                            cur_tools = [t for t in cur_tools
                                         if (t.get("function") or {}).get("name") != "look"]
                    continue
                return
