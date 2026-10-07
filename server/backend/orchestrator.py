"""Orchestrator: the brain plus the senses.

Architecture:
  • BRAIN = a text LLM (any OpenAI-compatible endpoint). It runs the dialogue, holds memory,
    and decides on its own when to "look" (the look tool).
  • SENSES:
      - ears: STT (user speech → text), Qwen3-ASR,
      - eyes: frame → straight to the brain's vision model (direct) or a VL caption (describe),
      - voice: answer text → speech (streaming TTS), Qwen3-TTS realtime.

Turn flow: STT → brain (stream; optionally look → vision) → TTS sentence by sentence, overlapped.
External interface: perceive_stream(frame, pcm) → a stream of OmniDelta
(answer text deltas, then speech chunks), so the server doesn't care which engines are used.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
from typing import AsyncIterator

from backend import logs, media, qwen_tts, gemini_tts, qwen_asr, vision, web, maps, memory
from backend.brain import Brain, BrainConfig
from backend.omni_client import (
    OMNI_AUDIO_SAMPLE_RATE,
    OmniClient,
    OmniConfig,
    OmniDelta,
    audio_part,
    image_part,
    text_part,
)

BRAIN_SYSTEM = (
    "You are the user's smart personal assistant (you can call yourself Smotri). Sharp, warm and "
    "genuinely useful: you think about the substance, get things done, have opinions and take "
    "initiative, like a strong personal agent rather than a help desk.\n"
    "You live in smart glasses (camera, microphone, speaker) and talk by voice. ALWAYS reply in the "
    "language the user speaks to you in. The glasses are just the medium. Do NOT talk about your "
    "nature: never say things like \"I'm on your face\", \"I see through your eyes\", \"I'm in your "
    "glasses\". The user knows. Just help.\n\n"
    "CHARACTER:\n"
    "- Smart and genuinely helpful. Think, don't brush off. If something needs a calculation or "
    "analysis, do it and give a concrete result, numbers, a conclusion.\n"
    "- Tone: CALM and POLITE. Respectful and to the point. No over-familiarity, no slang, no "
    "unnecessary jokes. But no servility or boilerplate either (\"how can I help\", \"happy to help\").\n"
    "- Have an opinion and recommend things when it fits, not just dry facts.\n\n"
    "HOW YOU SPEAK (this is live speech, not text):\n"
    "- LENGTH IS A HARD RULE: at most TWO short sentences. People listen, they don't read, and "
    "they won't sit through a long answer. Give the point with no preamble or wrap-up. Expand "
    "ONLY when explicitly asked for more detail.\n"
    "- When there's a lot of data (places, search results, options), name AT MOST TWO, the best "
    "ones. Never read out lists or retell search results.\n"
    "- Do NOT talk about your internal failures, tools or how you search (\"maps don't work\", "
    "\"let me search the web\", \"the service is down\"). Answer with what you have, and if the "
    "data may be imprecise, add one short caveat.\n"
    "- No lists, markdown, headings, emoji or asterisks: everything is spoken aloud.\n"
    "- Understand casual speech, slang and broken phrases by meaning, not literally. Build on the "
    "previous turns and don't repeat yourself. Don't end every reply with a question.\n\n"
    "VISION (look tool):\n"
    "- Call look when the question is about what the person sees, shows you, or asks you to look "
    "at, read, count or identify (what is this, how many, what color, what does it say).\n"
    "- If it's just conversation and there's nothing to look at, do NOT call look.\n"
    "- Do NOT announce looking (\"let me see\", \"one sec\"). Call look silently and answer. A "
    "sound cue plays by itself.\n\n"
    "INTERNET (web_search tool):\n"
    "- Call web_search for anything fresh or precise: news, events, prices, schedules, facts, "
    "anything after your knowledge cutoff. Retell the gist briefly, never read links aloud.\n\n"
    "MAPS AND PLACES (find_place tool):\n"
    "- You know the user's location when it's available. For \"what's nearby\", \"the nearest...\", "
    "addresses and where to go, call find_place. Name a couple of options with distance, briefly.\n\n"
    "MUSIC AND RADIO (play_track / play_media / control_media / stop_media):\n"
    "- A specific song or artist → play_track. A genre, mood or \"something\" → play_media (radio, "
    "English genre tag). Pause / resume / seek → control_media. Turn off → stop_media.\n"
    "- After starting music, reply in AT MOST 2–3 WORDS (\"Playing.\", \"Done.\"). The music is "
    "already playing and your words go over it. Don't name the track, don't explain, don't ask "
    "anything. Before calling play_track / play_media / stop_media, don't announce it either.\n\n"
    "IF YOU DIDN'T CATCH IT: if the utterance is a fragment or nonsense, briefly ask again. "
    "Never make up an answer.\n\n"
    "MEMORY AND FILES:\n"
    "- The MEMORY section gives you the user's profile and a map of their files. Rely on the profile.\n"
    "- When you learn something important about the user (name, how to address them, preferences, "
    "address, goals, habits), save it with remember. Don't ask again what's already in the profile.\n"
    "- For ongoing topics (say, learning a language) keep files: read_file the right one from the "
    "map, write_file / append_file to update progress and notes. Work from the file, not from "
    "memory on the fly.\n"
    "- ONBOARDING: if the profile is empty or thin, gently learn the user's name, how to address "
    "them, their goals and interests, one question at a time, and save them with remember. "
    "Don't ask everything at once.\n"
)

LOOK_TOOL = {
    "type": "function",
    "function": {
        "name": "look",
        "description": (
            "Look at what the user's camera sees right now. Call it when the question is about "
            "something visible: what is this, how many, what color, read the text, estimate "
            "from the picture, etc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "what exactly to examine or measure in the frame",
                }
            },
            "required": ["query"],
        },
    },
}

WEB_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the internet: fresh news, events, prices, schedules, facts, anything you "
            "don't know or that changes over time. Call it when you need current or precise "
            "information."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "search query"}},
            "required": ["query"],
        },
    },
}

FIND_PLACE_TOOL = {
    "type": "function",
    "function": {
        "name": "find_place",
        "description": (
            "Find places or businesses near the user (cafe, pharmacy, metro, shop, an address, "
            "etc.). Uses the user's current location and computes distances."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "what to find, e.g. \"coffee shop\", \"nearest pharmacy\""}},
            "required": ["query"],
        },
    },
}

PLAY_MEDIA_TOOL = {
    "type": "function",
    "function": {
        "name": "play_media",
        "description": (
            "Play internet radio by genre or mood. Call it when asked for music, radio or "
            "\"something\" without a specific song. The catalog is international: pass an ENGLISH "
            "genre tag: jazz, rap, hip hop, rock, pop, dance, lofi, chillout, ambient, news, "
            "electronic. Map moods to genres: upbeat → dance or pop; calm → chillout."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "English genre tag, e.g. jazz, rap, dance, lofi"}},
            "required": ["query"],
        },
    },
}

STOP_MEDIA_TOOL = {
    "type": "function",
    "function": {
        "name": "stop_media",
        "description": "Stop playback (when asked to turn off or stop the music or radio).",
        "parameters": {"type": "object", "properties": {}},
    },
}

PLAY_TRACK_TOOL = {
    "type": "function",
    "function": {
        "name": "play_track",
        "description": (
            "Play a SPECIFIC song by title or artist (\"play <song>\", \"put on <artist>\"). "
            "For a genre, mood or radio use play_media. Tracks can be seeked (radio can't)."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "song title and/or artist"}},
            "required": ["query"],
        },
    },
}

CONTROL_MEDIA_TOOL = {
    "type": "function",
    "function": {
        "name": "control_media",
        "description": (
            "Control current playback: pause, resume, seek to a position (tracks and podcasts). "
            "Call it on \"pause\", \"continue\", \"skip to 2 minutes\"."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["pause", "resume", "seek"]},
                "seconds": {"type": "integer", "description": "for seek: position in seconds from the start"},
            },
            "required": ["action"],
        },
    },
}

MEMORY_TOOLS = [
    {"type": "function", "function": {
        "name": "remember",
        "description": "Remember a fact about the user (name, preferences, address, goal, anything important). Keep it short.",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    }},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a memory file by its path from the file map (e.g. english/progress.md).",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Create or overwrite a memory file (path + full content). For topics, skills, progress.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
    }},
    {"type": "function", "function": {
        "name": "append_file",
        "description": "Append a line to a memory file (log, notes, progress).",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "text": {"type": "string"}}, "required": ["path", "text"]},
    }},
]

STT_INSTRUCTION = (
    "Transcribe the utterance in the audio verbatim. Output only the text, with no quotes "
    "or comments. If there is no speech, output an empty string."
)
SCENE_INSTRUCTION = (
    "This is a first-person frame from smart-glasses camera: what the user is looking at "
    "right now. Briefly describe exactly what is needed: \"{q}\". "
    "Facts from the frame only, one or two sentences, no filler."
)
TTS_DICTOR = (
    "You are a speech synthesizer (TTS). Read the user's text aloud VERBATIM, word for word, "
    "adding, removing or answering nothing. The text output must match the input exactly."
)
INAUDIBLE = "(inaudible)"


# Wake word at the start of the transcript. The client sends a pre-roll (the last 1200 ms of
# mic audio) so the start of the sentence isn't lost, and that window contains the wake word
# itself: Vosk fires only AFTER it was spoken. Shrinking the buffer would bring the clipping
# back, so the word is cut here instead. Only at the very start and only as a whole word, so
# "smotri" in the middle of a sentence is untouched. Words that also occur in real requests
# ("summary", "symmetry") are deliberately not stripped: a follow-up may start with them.
_WAKE_PREFIX = re.compile(
    r"^\s*(?:(?:hey|hi|ok(?:ay)?|эй)[,\s]+)?(?:smotr(?:i|ee|y)|smoke\s+tree|смотри(?:те)?)\b[\s,.!?:;-]*",
    re.I,
)

# Fast-path anchor: "what do I see" → grab the frame before asking the brain.
# A regex rather than exact substrings: "what do I *now* see" must match too.
_SEE_ANCHOR = re.compile(
    r"(что|чё|чего)[\w\s]{0,20}?(вижу|видно|видишь)"
    r"|what\b[\w\s']{0,20}?\b(see|seeing|looking at)\b",
    re.I,
)


def _strip_wake_word(text: str) -> str:
    """Remove the wake word from the start. If nothing else is left, return empty."""
    t = (text or "").strip()
    stripped = _WAKE_PREFIX.sub("", t, count=1).strip()
    return stripped if stripped else ""


def _format_for_speech(text: str) -> str:
    """One sentence per paragraph (an empty line = a pause in Omni speech)."""
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    return "\n\n".join(p for p in parts if p)


class Orchestrator:
    def __init__(
        self,
        *,
        voice: str = "Aiden",
        tempo: float = 1.0,
        remember: bool = True,
        max_history_turns: int = 8,
        tts_engine: str = "omni",
        stt_engine: str = "omni",
        prompt_extra: str = "",
        vision_mode: str = "direct",
        omni: OmniClient | None = None,
        brain: Brain | None = None,
    ) -> None:
        self.omni = omni or OmniClient(OmniConfig())
        self.brain = brain or Brain(BrainConfig())
        self.voice = voice
        self.tempo = tempo
        # voice engine: "qwen" (qwen3-tts realtime) | "google" (Gemini) | "omni" (Omni narrator)
        self.tts_engine = tts_engine
        # ears: "qwen" (qwen3-asr-flash, fast and accurate) | "omni" (Omni multimodal)
        self.stt_engine = stt_engine
        # extra system-prompt text (tuning for the user; from config.yaml or the client)
        self.prompt_extra = (prompt_extra or "").strip()
        # VISION:
        #   direct   — the frame goes STRAIGHT to the brain's vision model, which answers itself
        #              (text model = orchestrator, vision model = executor). No retelling.
        #   describe — a VL model captions the frame as text, the brain retells it.
        self.vision_mode = (vision_mode or "direct").lower()
        self.vision_model = os.getenv("BRAIN_VISION_MODEL", "deepseek-v4-flash-vision-exp")
        self.remember = remember
        self.max_history_turns = max_history_turns
        self.history: list[dict] = []  # text messages {role, content:str}
        # user location (from the phone) + a lazily resolved address
        self.location: tuple[float, float] | None = None
        self.location_addr: str | None = None
        self.now_playing: str | None = None   # what the agent started: 'radio "X"' / 'track "Y"'
        # stream URLs already served to the client: "play another one" must not return
        # the same station (limit=1 used to always give the top result → same silence)
        self._tried_streams: set[str] = set()

    def set_location(self, lat: float, lon: float) -> None:
        self.location = (lat, lon)
        self.location_addr = None
        # reverse geocoding IN THE BACKGROUND so it never blocks a turn
        try:
            asyncio.get_running_loop().create_task(self._resolve_addr(lat, lon))
        except RuntimeError:
            pass

    async def _resolve_addr(self, lat: float, lon: float) -> None:
        addr = await maps.reverse_geocode(lat, lon)
        print(f"[geo] {lat:.5f},{lon:.5f} → {addr!r}", flush=True)
        if addr and self.location == (lat, lon):
            self.location_addr = addr

    # ── dialogue memory ──────────────────────────────────────────────────────
    def reset_history(self) -> None:
        self.history.clear()

    def _append_history(self, user_text: str, answer_text: str) -> None:
        self.history.append({"role": "user", "content": user_text or INAUDIBLE})
        if answer_text:
            self.history.append({"role": "assistant", "content": answer_text})
        cap = self.max_history_turns * 2
        if len(self.history) > cap:
            self.history = self.history[-cap:]

    # ── senses ───────────────────────────────────────────────────────────────
    async def _transcribe(self, pcm: bytes, sample_rate: int) -> str:
        wav = media.pcm_to_wav_bytes(pcm, sample_rate=sample_rate)
        if self.stt_engine == "qwen":
            try:
                return await qwen_asr.transcribe(wav)
            except Exception as exc:  # noqa: BLE001 — fall back to omni: the agent must not go deaf
                print(f"[qwen_asr] fail, fallback omni: {exc}", flush=True)
        uri = "data:audio/wav;base64," + base64.b64encode(wav).decode()
        r = await self.omni.chat([audio_part(uri), text_part(STT_INSTRUCTION)], max_tokens=128)
        return r.text.strip()

    async def _describe_scene(self, frame_jpeg: bytes, query: str) -> str:
        prompt = SCENE_INSTRUCTION.format(q=query)
        desc = await vision.describe(frame_jpeg, prompt)
        if desc:
            return desc
        # fallback: omni vision
        uri = "data:image/jpeg;base64," + base64.b64encode(frame_jpeg).decode()
        r = await self.omni.chat([image_part(uri), text_part(prompt)], max_tokens=400)
        return r.text.strip()

    async def _speak_sentence(self, sentence: str) -> AsyncIterator[bytes]:
        """Sentence-by-sentence omni narrator: the last fallback (Gemini and the qwen session run in tts_worker)."""
        gen = self.omni.chat_stream(
            [text_part(sentence)], system=TTS_DICTOR, want_audio=True,
            voice=self.voice, temperature=0.0, max_tokens=1024,
        )
        if self.tempo == 1.0:
            async for d in gen:
                if d.audio_pcm:
                    yield d.audio_pcm
        else:
            async def src() -> AsyncIterator[bytes]:
                async for d in gen:
                    if d.audio_pcm:
                        yield d.audio_pcm
            async for pcm in media.atempo_pcm_stream(
                src(), tempo=self.tempo, sample_rate=OMNI_AUDIO_SAMPLE_RATE
            ):
                yield pcm

    # ── one turn (streaming pipeline) ────────────────────────────────────────
    async def perceive_stream(
        self,
        frame_jpeg: bytes,
        pcm: bytes,
        *,
        want_audio: bool = True,
        sample_rate: int = 24000,
        request_frame=None,
    ) -> AsyncIterator[OmniDelta]:
        """STT → brain (stream) → TTS per sentence, overlapped. The first sentence is spoken
        before the whole answer is ready: this is what cuts the time to first sound."""
        tl = logs.new_turn(len(frame_jpeg), len(pcm) / 2 / sample_rate)
        cancelled = False
        transcript = ""
        answer_parts: list[str] = []
        audio_bytes = 0

        try:
            # 1. EARS
            transcript = await self._transcribe(pcm, sample_rate)
            raw_transcript = transcript
            transcript = _strip_wake_word(transcript)
            if raw_transcript != transcript:
                print(f"[wake] stripped wake word: {raw_transcript!r} → {transcript!r}", flush=True)
            tl.stt(transcript)
            # tell the client what was heard (shown on screen)
            yield OmniDelta(event="user_text", text=transcript)
            # speak back in the user's language
            tts_language = qwen_tts.language_for(transcript)

            # vision state for THIS turn (the frame is taken once and reused by every look)
            look_state = {"frame": None, "n": 0}

            visual_context = ""      # describe mode only (caption from the VL model)
            # vision_ctx carries the frame to the vision model. The look handler puts
            # image_uri here; Brain attaches the picture and hands the turn to the vision model.
            vision_ctx: dict = {}
            direct = self.vision_mode == "direct"

            # FAST PATH on "what do I see": grab the frame right away instead of spending a
            # brain round on deciding to look.
            _t = (transcript or "").lower()
            if request_frame is not None and _SEE_ANCHOR.search(_t):
                yield OmniDelta(event="tool", text="look")   # "looking" chip on the client
                fresh = await request_frame()
                if fresh:
                    look_state["frame"] = fresh   # reused if the model still calls look
                    look_state["n"] = 1
                    if direct:
                        # the frame goes INTO the request: the vision model runs the turn in one pass
                        vision_ctx["image_uri"] = ("data:image/jpeg;base64,"
                                                   + base64.b64encode(fresh).decode())
                        tl.look(transcript, f"frame {len(fresh)/1024:.0f} KB → straight to {self.vision_model}")
                    else:
                        visual_context = await self._describe_scene(fresh, transcript)
                        tl.look(transcript, visual_context)

            system_prompt = BRAIN_SYSTEM
            # "now": local date/time (AGENT_TIMEZONE) so the agent knows what's open,
            # morning vs evening, the weekday. Not to be spoken, just taken into account.
            try:
                from datetime import datetime
                from zoneinfo import ZoneInfo
                _tz = os.getenv("AGENT_TIMEZONE", "UTC")
                _now = datetime.now(ZoneInfo(_tz))
                system_prompt += (
                    f"\n\n# Now\n{_now.strftime('%A, %d %B %Y, %H:%M')} ({_tz}). "
                    "Take the time of day and weekday into account (what's open, morning/evening)."
                )
            except Exception:  # noqa: BLE001
                pass
            # memory: user profile + file map (read the right one with read_file)
            try:
                system_prompt += "\n\n# MEMORY\n" + memory.build_map()
            except Exception:  # noqa: BLE001
                pass
            if self.location:
                # Raw coordinates are NOT given to the model: it hallucinates streets from them.
                # Only a human-readable address (reverse geocode, resolved in the background).
                # find_place takes the coordinates programmatically (self.location).
                if self.location_addr:
                    system_prompt += (
                        f"\n\n# Where the user is\nThe user is currently at: {self.location_addr}. "
                        "Use it for \"nearby\", addresses and directions. Find nearby places with find_place."
                    )
                else:
                    system_prompt += (
                        "\n\n# Where the user is\nThe exact address couldn't be determined right now. "
                        "Do NOT invent a street or district: you can't know it from coordinates. If asked "
                        "where we are, say honestly that the exact place isn't available yet. Still use "
                        "find_place for nearby places."
                    )
            if self.now_playing:
                system_prompt += (
                    f"\n\n# Now playing\n{self.now_playing} is playing. "
                    "If asked what's playing, say so. Pause/seek: control_media; change: play_media/play_track."
                )
            if self.prompt_extra:
                system_prompt += "\n\n# From the user\n" + self.prompt_extra
            if visual_context:
                system_prompt += (
                    "\n\n# WHAT IS IN FRONT OF THE USER NOW (fresh camera frame)\n"
                    + visual_context
                    + "\nAnswer from what is seen, briefly. No need to look again."
                )
            # The frame is already here (anchor fired) → the image goes INTO the user message
            # and the vision model runs the turn. Otherwise plain text; vision joins via look.
            _prefetched = bool(vision_ctx.get("image_uri"))
            if _prefetched:
                _user_content: object = [
                    {"type": "image_url", "image_url": {"url": vision_ctx["image_uri"]}},
                    {"type": "text", "text": (transcript or INAUDIBLE)},
                ]
                vision_ctx["attached"] = True   # Brain won't attach it again
            else:
                _user_content = transcript or INAUDIBLE
            messages = (
                [{"role": "system", "content": system_prompt}]
                + self.history
                + [{"role": "user", "content": _user_content}]
            )
            tl.prompt(system_prompt, self.history)

            async def tool_handler(name: str, args: dict) -> str:
                # activity feed on the client: which tool was called
                await out_q.put(OmniDelta(event="tool", text=name))
                if name == "look":
                    look_state["n"] += 1
                    if look_state["n"] > 3:
                        return ("You have already looked several times. Don't look again; "
                                "answer from what you've already seen.")
                    q = args.get("query") or "what is in the frame"
                    # the frame is taken ONCE per turn; later looks reuse it for other questions
                    if look_state["frame"] is None:
                        if request_frame is not None:
                            fresh = await request_frame()
                            if fresh:
                                look_state["frame"] = fresh
                        if look_state["frame"] is None:
                            look_state["frame"] = frame_jpeg  # fallback
                    frame = look_state["frame"]
                    if not frame:
                        return "Couldn't get a camera frame right now."
                    if direct:
                        # NO text caption: put the frame into vision_ctx and the vision model
                        # takes over the turn, answering from the pixels itself.
                        vision_ctx["image_uri"] = ("data:image/jpeg;base64,"
                                                   + base64.b64encode(frame).decode())
                        vision_ctx["hint"] = (
                            "This is a frame from the glasses' camera: what the user is looking at "
                            f"right now. Their request: \"{q}\". Answer FROM THIS FRAME and strictly "
                            "to the request: one or two short spoken sentences, in the user's "
                            "language. Don't list everything visible, don't describe the "
                            "background, don't start with \"in the frame\": this is said aloud."
                        )
                        tl.look(q, f"frame {len(frame)/1024:.0f} KB → straight to {self.vision_model}")
                        return "Frame received; the image is attached in the next message."
                    desc = await self._describe_scene(frame, q)
                    tl.look(q, desc)
                    return desc
                if name == "web_search":
                    return await web.search_text(args.get("query") or "")
                if name == "find_place":
                    # addr is the address already known from the geocoder: without a Places
                    # key, the fallback searches the web for businesses near that address
                    return await maps.find_place(args.get("query") or "", self.location,
                                                 addr=self.location_addr)
                if name == "play_media":
                    import json as _json
                    from backend import audio_sources
                    st = await audio_sources.find_radio(args.get("query") or "",
                                                        exclude=self._tried_streams)
                    if not st:
                        return "No matching station found. Try another query."
                    # event to the client: play the stream (url + title) with ExoPlayer
                    self._tried_streams.add(st["url"])
                    self.now_playing = f"radio \"{st['title']}\""
                    await out_q.put(OmniDelta(
                        event="media",
                        text=_json.dumps({"action": "play", "url": st["url"], "title": st["title"]}),
                    ))
                    return "Done, the radio is playing. Reply to the user in AT MOST 2–3 words."
                if name == "play_track":
                    import json as _json
                    from backend import audio_sources
                    q = args.get("query") or ""
                    # Yandex Music first (real catalog, needs a token), then Audius
                    tr = await audio_sources.find_yandex_track(q) or await audio_sources.find_track(q)
                    if not tr:
                        return "Track not found. Try a more precise title or artist."
                    self.now_playing = f"track \"{tr['title']}\""
                    await out_q.put(OmniDelta(
                        event="media",
                        text=_json.dumps({"action": "play", "url": tr["url"], "title": tr["title"]}),
                    ))
                    return "Done, the track is playing. Reply to the user in AT MOST 2–3 words."
                if name == "stop_media":
                    import json as _json
                    self.now_playing = None
                    await out_q.put(OmniDelta(event="media", text=_json.dumps({"action": "stop"})))
                    return "Playback stopped."
                if name == "control_media":
                    import json as _json
                    act = args.get("action") or ""
                    if act == "seek":
                        sec = int(args.get("seconds") or 0)
                        # the client reads the seconds from the title field
                        await out_q.put(OmniDelta(event="media",
                            text=_json.dumps({"action": "seek", "title": str(sec)})))
                        return f"Seeked to {sec} s."
                    if act in ("pause", "resume"):
                        await out_q.put(OmniDelta(event="media", text=_json.dumps({"action": act})))
                        return "Done."
                    return "Unknown player action."
                if name == "remember":
                    return memory.remember(args.get("text") or "")
                if name == "read_file":
                    return memory.read_file(args.get("path") or "")
                if name == "write_file":
                    return memory.write_file(args.get("path") or "", args.get("content") or "")
                if name == "append_file":
                    return memory.append_file(args.get("path") or "", args.get("text") or "")
                return f"ERROR: unknown tool {name}"

            stats: dict = {}
            out_q: asyncio.Queue = asyncio.Queue()
            tts_in_q: asyncio.Queue = asyncio.Queue()
            sentinel = object()

            _tools = [WEB_TOOL, FIND_PLACE_TOOL, PLAY_MEDIA_TOOL, PLAY_TRACK_TOOL,
                      STOP_MEDIA_TOOL, CONTROL_MEDIA_TOOL, *MEMORY_TOOLS]
            # no look if the frame is already attached (the model would call it again and
            # ignore the picture) or if the caption is already in the prompt (describe mode)
            if not visual_context and not _prefetched:
                _tools = [LOOK_TOOL, *_tools]

            # PRODUCER: streams the brain, cuts sentences → TTS queue, text → out
            async def producer() -> None:
                buf = ""
                try:
                    async for delta in self.brain.run_stream(
                        messages, tools=_tools,
                        tool_handler=tool_handler, stats=stats,
                        # vision: the text model orchestrates, the vision model executes
                        vision_model=self.vision_model if direct else None,
                        vision_ctx=vision_ctx if direct else None,
                        model_override=self.vision_model if _prefetched else None,
                    ):
                        answer_parts.append(delta)
                        await out_q.put(OmniDelta(text=delta))
                        buf += delta
                        while True:
                            m = re.search(r'[.!?…]+["»)\]]*(?=\s|$)', buf)
                            if not m:
                                break
                            sentence = buf[: m.end()].strip()
                            buf = buf[m.end():].lstrip()
                            if sentence:
                                await tts_in_q.put(sentence)
                    if buf.strip():
                        await tts_in_q.put(buf.strip())
                finally:
                    await tts_in_q.put(sentinel)

            # TTS WORKER: speaks sentences → audio out.
            # qwen: ONE session for the whole turn → no gaps between sentences.
            # Sentences are read into a BUFFER (reader) and the engine consumes the buffer. So if
            # the qwen session dies, the omni fallback can speak the WHOLE buffer instead of
            # hanging on an empty queue (that used to cause an endless "thinking").
            sbuf: list[str] = []
            reading_done = asyncio.Event()

            async def reader() -> None:
                while True:
                    s = await tts_in_q.get()
                    if s is sentinel:
                        reading_done.set()
                        return
                    sbuf.append(s)

            async def buffered_feed() -> AsyncIterator[str]:
                i = 0
                while True:
                    if i < len(sbuf):
                        yield sbuf[i]
                        i += 1
                    elif reading_done.is_set():
                        return
                    else:
                        await asyncio.sleep(0.02)

            async def tts_worker() -> None:
                rd = asyncio.create_task(reader())
                try:
                    if not want_audio:
                        await reading_done.wait()
                        return
                    # FALLBACK CHAIN: Gemini (best intonation) → qwen realtime → omni.
                    # 1. Gemini (google): sentence by sentence from the buffer. If the FIRST
                    #    sentence fails (no key / quota / proxy), fall back to qwen (no audio sent yet).
                    if self.tts_engine in ("google", "gemini") and gemini_tts.is_configured():
                        produced = False
                        try:
                            async for s in buffered_feed():
                                pcm = await gemini_tts.synth(s, voice=None)
                                if pcm:
                                    produced = True
                                    await out_q.put(OmniDelta(audio_pcm=pcm))
                            return
                        except Exception as exc:  # noqa: BLE001
                            print(f"[gemini_tts] fail: {exc}", flush=True)
                            if produced:
                                return   # part was already spoken by Gemini; don't duplicate
                    # 2. qwen realtime (one session per turn)
                    if qwen_tts.is_configured():
                        try:
                            async for chunk in qwen_tts.stream_session(
                                buffered_feed(), voice=self.voice, language=tts_language
                            ):
                                await out_q.put(OmniDelta(audio_pcm=chunk))
                            return
                        except Exception as exc:  # noqa: BLE001 — session died → omni speaks the buffer
                            print(f"[qwen_tts] session fail, fallback omni: {exc}", flush=True)
                    # 3. omni narrator: speak the whole buffer (after reading the rest)
                    await reading_done.wait()
                    for s in sbuf:
                        async for chunk in self._speak_sentence(s):
                            await out_q.put(OmniDelta(audio_pcm=chunk))
                finally:
                    if not rd.done():
                        rd.cancel()
                    try:
                        await rd
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass

            prod = asyncio.create_task(producer())
            tts = asyncio.create_task(tts_worker())

            # The turn ends when BOTH the producer and the worker are done. The end is NOT
            # tied to TTS alone (an instant TTS failure used to end the turn before the text).
            async def ender() -> None:
                try:
                    await prod
                finally:
                    try:
                        await tts
                    finally:
                        await out_q.put(sentinel)

            end = asyncio.create_task(ender())
            try:
                while True:
                    item = await out_q.get()
                    if item is sentinel:
                        break
                    if item.text:
                        tl.mark_text()
                    if item.audio_pcm:
                        tl.mark_audio()
                        audio_bytes += len(item.audio_pcm)
                    yield item
            finally:
                for t in (prod, tts, end):
                    if not t.done():
                        t.cancel()
                for t in (prod, tts, end):
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass

            answer = "".join(answer_parts).strip()
            tl.answer(answer)
            tl.usage(stats.get("usage"), label=f"brain:{stats.get('model', '?')}")
        except (asyncio.CancelledError, GeneratorExit):
            cancelled = True
            tl.interrupted()
            raise
        except Exception as exc:  # noqa: BLE001
            tl.error(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if not cancelled:
                tl.done(audio_bytes)
                answer = "".join(answer_parts).strip()
                if self.remember and (transcript or answer):
                    self._append_history(transcript, answer)
