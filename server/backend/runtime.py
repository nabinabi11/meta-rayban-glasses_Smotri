"""Runtime config: hot-reloaded from config.yaml (no restart or rebuild).

Only NON-secret switches (agent_mode, tts/stt_engine, voice, prompt_extra, vision_mode).
Secrets (API keys) live in .env and never go into git.

Priority: config.yaml  >  env  >  default.
Edit config.yaml → it's re-read on the fly (cached by mtime).
The client can also send a per-session override over the WS: {type:"config", ...}.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    import yaml
except Exception:  # noqa: BLE001
    yaml = None

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yaml"

# fields and their defaults (the .env name is UPPERCASE)
FIELDS = ("agent_mode", "tts_engine", "stt_engine", "voice", "prompt_extra", "vision_mode")
_DEFAULTS = {
    "agent_mode": "agent",   # reserved; only the tool-using agent exists
    "tts_engine": "qwen",
    "stt_engine": "qwen",
    "voice": "Ethan",
    "prompt_extra": "",
    "vision_mode": "direct",
}

_cache: dict = {}
_mtime: float = -1.0


def get() -> dict:
    """The current effective config (re-reads config.yaml when the file changes)."""
    global _cache, _mtime
    data = dict(_DEFAULTS)
    # env overrides the default
    for k in FIELDS:
        v = os.getenv(k.upper())
        if v is not None and v != "":
            data[k] = v
    # config.yaml is the main source for switches and overrides env
    try:
        if yaml is not None and CONFIG.exists():
            m = CONFIG.stat().st_mtime
            if m != _mtime:
                _mtime = m
                _cache = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
            for k in FIELDS:
                if isinstance(_cache, dict) and _cache.get(k) is not None:
                    data[k] = _cache[k]
    except Exception as e:  # noqa: BLE001
        print(f"[runtime] config.yaml read failed: {e}", flush=True)
    return data
