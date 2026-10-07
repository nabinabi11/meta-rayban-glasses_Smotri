"""Server config: .env loading, settings, access token."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    """Tiny .env loader (no python-dotenv dependency)."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Settings:
    def __init__(self) -> None:
        self.host = os.getenv("HOST", "127.0.0.1")
        self.port = int(os.getenv("PORT", "8000"))
        # legacy settings (the effective switches live in runtime.py / config.yaml)
        self.agent_mode = os.getenv("AGENT_MODE", "omni").lower()
        self.voice = os.getenv("OMNI_VOICE", "Aiden")
        # speech tempo for the omni narrator: <1 slower (ffmpeg atempo, 0.5..2.0), 1.0 = as is.
        # atempo noticeably damages the timbre, so 1.0 is the default.
        self.speech_tempo = float(os.getenv("SPEECH_TEMPO", "1.0"))
        # dialogue memory
        self.remember = os.getenv("REMEMBER", "1") not in ("0", "false", "False", "")
        self.max_history_turns = int(os.getenv("MAX_HISTORY_TURNS", "6"))
        # voice engine: qwen (qwen3-tts) | omni (narrator)
        self.tts_engine = os.getenv("TTS_ENGINE", "omni").lower()
        # ears: qwen (qwen3-asr-flash, fast) | omni (multimodal)
        self.stt_engine = os.getenv("STT_ENGINE", "omni").lower()
        # output audio rate, reported to the client (player)
        self.audio_sample_rate = 24000


TOKEN_FILE = ROOT / "data" / "access_token"


def access_token() -> str:
    """Access secret for /ws. Without it, anyone who knows the IP spends your API keys.

    Taken from SMOTRI_TOKEN; if unset, generated once and stored in data/access_token
    (survives restarts and container rebuilds via the volume).
    """
    tok = os.getenv("SMOTRI_TOKEN", "").strip()
    if tok:
        return tok
    if TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    import secrets

    tok = secrets.token_urlsafe(24)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(tok, encoding="utf-8")
    return tok


def connect_url(token: str, port: int) -> str:
    """Connection string for the app: pasted into it as a whole."""
    base = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
    if not base:
        base = f"ws://<SERVER_IP>:{port}"
    if not base.endswith("/ws"):
        base += "/ws"
    return f"{base}?token={token}"
