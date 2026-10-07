"""Smotri server: FastAPI + WebSocket.

Flow: the phone sends audio (PCM16/24k) over the WS and frames (JPEG) on request. On
`commit` (the user finished speaking) we build a turn: audio (+ frame) → orchestrator →
stream the answer back: text deltas + PCM speech.

Access: ws://<host>:8000/ws?token=<SMOTRI_TOKEN>. Without the right token the handshake
is rejected (HTTP 403 / close 4401), see config.access_token.

WS protocol (JSON text; binary payloads are base64). Full table: docs/PROTOCOL.md
  client → server:
    {type:"frame", data:<b64 jpeg>}    camera frame (normally the answer to "capture")
    {type:"audio", data:<b64 pcm16>}   mic chunk (while the user speaks)
    {type:"commit"}                    utterance finished: process the turn
    {type:"cancel"}                    drop buffered audio
  server → client:
    {type:"ready"}
    {type:"status", state:"listening|thinking|speaking|idle"}
    {type:"user_text", text:"..."}     the recognized user utterance (what was heard)
    {type:"text", delta:"..."}         answer text deltas
    {type:"audio", data:<b64 pcm16>}   answer speech chunk (PCM16/24k)
    {type:"done"}
    {type:"error", message:"..."}
"""

from __future__ import annotations

import base64
import hmac
import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from backend import config

config.load_env()
from backend import logs  # noqa: E402
from backend.orchestrator import Orchestrator  # noqa: E402  (after load_env)
from backend import qwen_tts  # noqa: E402
from backend import runtime  # noqa: E402


def make_agent(rt: dict):
    """Build the agent from the runtime config (rt = runtime.get() + WS overrides)."""
    return Orchestrator(
        voice=rt.get("voice", "Ethan"), tempo=settings.speech_tempo,
        remember=settings.remember, max_history_turns=settings.max_history_turns,
        tts_engine=rt.get("tts_engine", "qwen"), stt_engine=rt.get("stt_engine", "qwen"),
        prompt_extra=rt.get("prompt_extra", ""),
        vision_mode=rt.get("vision_mode", "direct"),
    )

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("smotri")

settings = config.Settings()
MINIAPP_DIR = config.ROOT / "miniapp"
ACCESS_TOKEN = config.access_token()

app = FastAPI(title="smotri")


@app.on_event("startup")
async def _print_connect_url() -> None:
    url = config.connect_url(ACCESS_TOKEN, settings.port)
    bar = "=" * 72
    print(
        f"\n{bar}\n"
        "  Smotri server is up. Paste this into the app (Server address):\n\n"
        f"  {url}\n\n"
        "  (<SERVER_IP> = public IP of this machine; set PUBLIC_URL in .env\n"
        "   to print it ready-made)\n"
        f"{bar}\n",
        flush=True,
    )


@app.get("/health")
async def health() -> dict:
    rt = runtime.get()   # effective config (config.yaml > env > default)
    return {
        "ok": True,
        "agent_mode": rt["agent_mode"],
        "tts_engine": rt["tts_engine"],
        "stt_engine": rt["stt_engine"],
        "vision_mode": rt["vision_mode"],
        "audio_sample_rate": settings.audio_sample_rate,
        "qwen_tts_configured": qwen_tts.is_configured(),
    }


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    given = ws.query_params.get("token", "")
    if not hmac.compare_digest(given.encode(), ACCESS_TOKEN.encode()):
        await ws.close(code=4401)   # wrong/missing token: rejected before accept, no keys spent
        logs.event("rejected: wrong token", logs.DIM)
        return
    await ws.accept()
    rt = dict(runtime.get())   # effective session config (the client may override it)
    orch = make_agent(rt)
    import asyncio

    latest_frame: bytes | None = None
    audio_buf = bytearray()
    send_lock = asyncio.Lock()
    turn_task: asyncio.Task | None = None
    pending_frame: asyncio.Future | None = None   # waiting for a fresh frame requested by look

    async def safe_send(obj: dict) -> None:
        # one serialized send channel (the turn and the main loop send concurrently)
        async with send_lock:
            await ws.send_json(obj)

    async def request_frame() -> bytes | None:
        """Ask the client for a FRESH frame and wait for it.
        Used by the look tool, so the agent sees "now" and not a stale frame.
        Timeout → None (never a stale frame, see below)."""
        nonlocal pending_frame, latest_frame
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        pending_frame = fut
        # Freshness beats availability: drop the previous frame so it can't stand in for the new one.
        latest_frame = None
        await safe_send({"type": "capture"})   # client: raises the camera and sends a fresh frame
        try:
            # On DAT 0.9 the glasses' camera reaches STREAMING in ~5 s, first frame ~13 s (measured).
            # The timeout must be LONGER than the client's window or the server gives up first.
            return await asyncio.wait_for(fut, timeout=16.0)
        except asyncio.TimeoutError:
            # Do NOT return a stale frame: that used to cause an "off by one" bug where the
            # turn described the previous question's scene. Better to admit there's no frame.
            print("[capture] timeout: no fresh frame, NOT substituting a stale one", flush=True)
            return None
        finally:
            pending_frame = None

    async def run_turn(frame: bytes, pcm: bytes) -> None:
        try:
            await safe_send({"type": "status", "state": "thinking"})
            spoke = False
            async for d in orch.perceive_stream(frame, pcm, want_audio=True,
                                                request_frame=request_frame):
                if d.event == "look":
                    await safe_send({"type": "look"})  # client plays the shutter sound
                    continue
                if d.event == "user_text":
                    await safe_send({"type": "user_text", "text": d.text})  # what was heard from the user
                    continue
                if d.event == "tool":
                    await safe_send({"type": "tool", "name": d.text})  # activity feed
                    continue
                if d.event == "media":
                    # d.text = JSON {action, url?, title?} → client plays/stops
                    import json as _json
                    try:
                        payload = _json.loads(d.text)
                    except Exception:  # noqa: BLE001
                        payload = {}
                    await safe_send({"type": "media", **payload})
                    continue
                if d.text:
                    await safe_send({"type": "text", "delta": d.text})
                if d.audio_pcm:
                    if not spoke:
                        spoke = True
                        await safe_send({"type": "status", "state": "speaking"})
                    await safe_send({"type": "audio", "data": base64.b64encode(d.audio_pcm).decode()})
            await safe_send({"type": "done"})
        except asyncio.CancelledError:
            # interrupted (barge-in/abort): the client already stopped playback, send nothing
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("turn failed")
            await safe_send({"type": "error", "message": f"{type(exc).__name__}: {exc}"})

    async def cancel_turn() -> None:
        nonlocal turn_task
        if turn_task and not turn_task.done():
            turn_task.cancel()
            try:
                await turn_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        turn_task = None

    await safe_send({"type": "ready"})
    await safe_send({"type": "status", "state": "idle"})
    logs.event("client connected", logs.GREEN)

    try:
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")

            if mtype == "frame":
                try:
                    latest_frame = base64.b64decode(msg["data"])
                    # if a turn is waiting for a fresh frame (look), hand it over
                    if pending_frame is not None and not pending_frame.done():
                        pending_frame.set_result(latest_frame)
                except Exception:  # noqa: BLE001
                    pass

            elif mtype == "audio":
                try:
                    audio_buf.extend(base64.b64decode(msg["data"]))
                except Exception:  # noqa: BLE001
                    pass

            elif mtype in ("cancel", "abort"):
                # abort = interrupt the current answer
                await cancel_turn()
                audio_buf.clear()

            elif mtype == "reset":
                await cancel_turn()
                orch.reset_history()
                audio_buf.clear()

            elif mtype == "config":
                # runtime-config override for this session (no server restart)
                await cancel_turn()
                for k in runtime.FIELDS:
                    if k in msg and msg[k] is not None:
                        rt[k] = msg[k]
                orch = make_agent(rt)
                audio_buf.clear()
                await safe_send({
                    "type": "config_applied",
                    "agent_mode": rt["agent_mode"], "tts_engine": rt["tts_engine"],
                    "stt_engine": rt["stt_engine"], "voice": rt["voice"],
                })

            elif mtype == "commit":
                # a frame is NOT required on commit: look requests a fresh one itself
                if len(audio_buf) < 4800:  # < ~0.1 s: no speech
                    await safe_send({"type": "error", "message": "utterance too short"})
                    audio_buf.clear()
                    continue

                await cancel_turn()  # interrupt the current turn if it's still running
                frame = latest_frame or b""
                pcm = bytes(audio_buf)
                audio_buf.clear()
                turn_task = asyncio.create_task(run_turn(frame, pcm))

            elif mtype == "location":
                # phone location → agent context (for "nearby" / maps)
                try:
                    if hasattr(orch, "set_location"):
                        orch.set_location(float(msg["lat"]), float(msg["lon"]))
                except (KeyError, ValueError, TypeError):
                    pass

            elif mtype == "ping":
                await safe_send({"type": "pong"})

    except WebSocketDisconnect:
        logs.event("client disconnected", logs.DIM)
    except Exception:  # noqa: BLE001
        log.exception("ws error")
    finally:
        await cancel_turn()


# optional static web client (mounted last so it doesn't shadow /ws and /health)
if MINIAPP_DIR.exists():
    app.mount("/", StaticFiles(directory=str(MINIAPP_DIR), html=True), name="miniapp")
