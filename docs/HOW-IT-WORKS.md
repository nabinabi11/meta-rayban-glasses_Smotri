# How Smotri works

A technical walkthrough: what the system is made of, how one request flows through it, why each
piece was chosen, and how long every step takes. **All numbers are real measurements** (August 2026,
server in Singapore, DeepSeek brain, Alibaba speech).

## 1. Three physical layers

```
┌─ RAY-BAN META ─────────────────────────────────────────────────────────┐
│  mic + speaker ─────── Bluetooth HFP/SCO (plain headset) ───┐          │
│  camera ────────────── Bluetooth via Meta DAT SDK ──────────┼──┐       │
└─────────────────────────────────────────────────────────────┼──┼───────┘
                                                              ▼  ▼
┌─ ANDROID PHONE: Smotri app (Kotlin) ───────────────────────────────────┐
│  wake word "Hey look" (Vosk, offline)  1.2 s pre-roll audio buffer     │
│  VAD (end of utterance by silence)     ExoPlayer (radio / music)       │
│  location                              Compose UI                      │
└──────────────────────────┬─────────────────────────────────────────────┘
                           │  one WebSocket  (ws://host:8000/ws?token=…)
                           ▼
┌─ SERVER (Python / FastAPI, Docker) ────────────────────────────────────┐
│  audio in → STT → BRAIN (+ tools) → TTS → audio out                    │
└───────┬──────────────────┬───────────────────┬─────────────────────────┘
        ▼                  ▼                   ▼
   Alibaba DashScope   any OpenAI-compatible   the internet
   (ears, voice,       LLM (brain, and         (search, maps,
    vision fallback)    optionally vision)      radio, music)
```

**Key fact: the glasses have two independent channels to the phone.** Audio goes through the OS as
an ordinary Bluetooth headset. The camera goes through Meta's SDK. **The DAT SDK has no audio module.**
So mic and camera can't be synchronized at the SDK level, and they compete for the same Bluetooth radio.

## 2. One voice turn, end to end

The example is a turn without the camera: *"where am I?"* The timings were measured on the
author's Russian-language setup; the pipeline is identical in English.

```
1. GLASSES → PHONE     speech over BT SCO, PCM16
                        the phone always keeps the last 1200 ms in a pre-roll buffer
2. TRIGGER              Vosk (offline) hears the wake word, OR inside the follow-up window
                        the VAD sees 200 ms of speech → send pre-roll, start streaming
3. PHONE → SERVER       100 ms chunks, base64 in JSON over the WebSocket
4. END OF UTTERANCE     VAD: 1000 ms of silence after ≥300 ms of speech → {"type":"commit"}
                        safety net: hard limit of 12 s
5. EARS (STT)           qwen3-asr-flash                                      0.45 s
                        the wake word is stripped from the transcript start
6. BRAIN                LLM, stream=true, system prompt + history + tools    1.6–2.5 s
7. SENTENCE SPLITTER    the token stream is cut on [.!?…]; the first sentence
                        goes to TTS before the rest is generated
8. VOICE (TTS)          qwen3-tts-flash-realtime, ONE WebSocket session per  0.3 s
                        turn, sentences pushed in as they arrive → seamless speech
9. SERVER → PHONE → GLASSES   PCM16/24 kHz chunks → AudioTrack → BT SCO
                        the mic is muted meanwhile (half-duplex, no echo)

TOTAL: 2–6 s per turn, first sound after ~2–3 s
```

**Why it feels fast despite many hops:** no step waits for the previous one to finish. The brain
streams tokens. As soon as a sentence is complete, it's being synthesized. As soon as the first
audio chunk arrives, it's playing in the glasses. You hear the beginning of the answer while the
model is still writing the end.

## 3. Vision: handing the turn to a seeing model

### The old way: double hop (abandoned)

```
brain: "need to look" → look → frame → VL model describes it IN TEXT
                                     → brain retells the description
```

The problem wasn't speed. It was **information loss**. The VL model squeezed the scene into two
sentences, and anything not in them didn't exist for the brain. "Read the third line" or "how
many cans are there" got made-up answers.

### Now: turn handoff (`vision_mode: direct`)

```
text model  = ORCHESTRATOR   runs the dialogue, decides whether to answer or to look
vision model = EXECUTOR      gets THE SAME context and answers from the pixels itself
```

When the `look` handler receives a frame, it doesn't describe it. It puts a data URI into
`vision_ctx`. `brain.run_stream` then appends the image as a **separate user message** after the
tool result, switches the model to `BRAIN_VISION_MODEL`, and continues the same loop with the
same system prompt, history and tools. No retelling between models.

Two details that matter:
- **`look` is removed from the tool list** once the image is attached. Otherwise the model sees the
  image, calls `look` again anyway, and ignores the attached frame (verified experimentally).
- **Invariant:** if any message contains an image, the model is forced to the vision model.
  Otherwise text-only models return `400 model does not support image`.

**Fast path.** If the transcript matches "what … see / looking at", the server grabs the frame
*before* calling the brain and starts the turn directly on the vision model. This saves one round
(~0.7 s). It is a regex, not substring matching, so "what do I *now* see" still hits.
The same anchor exists for Russian («что я вижу»).

`vision_mode: describe` keeps the old caption-based path. It works with any VL model plus any
text-only brain, and it's the default in the one-key Alibaba profile.

### What vision costs

```
camera added to session          ~15 ms
stream reaches STREAMING         1.7–10 s   ← unstable
first video frame                +4.4–8 s
frame on the server              total 6.6–14 s
vision model answer              1.2–2.4 s
```

**This is the bottleneck of the whole system**, and it's outside our control (see §6).

## 4. Agent tools

The brain gets a list of functions and decides what to call. Everything except `look` runs on the
server. `look` needs the phone.

| Tool | Does | How |
|---|---|---|
| `look` | grab a frame from the glasses | server sends `{"type":"capture"}`, the phone raises the camera and returns a JPEG |
| `web_search` | internet search | DuckDuckGo HTML, own parser, no key |
| `find_place` | places nearby | Yandex Places if keyed → **fallback: web search near the known address** |
| `play_media` | radio by genre/country | radio-browser.info, regional first, each stream checked for liveness |
| `play_track` | a specific song | Yandex Music (your token) → Audius |
| `control_media` / `stop_media` | pause / resume / seek / stop | `{"type":"media"}` event to the phone |
| `remember` | store a fact about the user | appended to `profile.md` |
| `read_file` / `write_file` / `append_file` | file memory | sandboxed to `MEMORY_DIR` (path traversal blocked) |

**Memory** is a folder of markdown files outside the code. Each turn, the system prompt gets the
full user profile plus a tree of the memory files with their titles. The agent reads specific
files on demand. This holds a lot of memory without bloating every request.

**Injected into the system prompt every turn:** the local date/time and weekday, the profile and
memory map, the user's **street address** (not raw coordinates: with numbers, the model
hallucinated streets), what's currently playing, and `prompt_extra` from the config.

**Radio note.** The *phone* fetches the stream, not the server, so a stream reachable from the
server can still be dead on the phone. Search goes region → language → global, takes batches of
8 candidates, probes each for the first byte, and remembers already-served URLs so that
"play another one" really gives another one.

## 5. Phone client: non-obvious decisions

Each item fixes a specific bug found in real use.

- **Pre-roll buffer (1200 ms).** Capture starts only after Vosk recognizes the wake word, so
  everything said before that moment was lost and STT got sentences without their start. The
  phone keeps the last 1.2 s and sends it before the live stream.
- **VAD threshold 0.008 RMS.** The glasses' mic over SCO is quiet: speech 0.011–0.028, silence
  0.000–0.003. The usual 0.018 classified half of the speech as silence.
- **800 ms grace after the "listening" chime.** The chime leaks into the mic as fake "speech". The
  VAD isn't fed during it, but the audio **is still sent** so STT hears the whole sentence.
- **Hard 12 s listen limit.** If you speak too quietly, the speech threshold is never reached and the
  capture would hang forever. After the limit, the turn is committed as is.
- **Half-duplex.** The mic is muted while the agent speaks, otherwise it hears itself.
- **Camera only on `look`.** The video stream starts only when the agent actually looks, and stops
  right after the frame. A warm camera would be faster, but the privacy LED would be on all the time.
- **Playback generation counter.** On interruption, chunks of the old answer are dropped.
  Otherwise leftovers of the previous reply overlapped the new one.
- **45 s turn watchdog.** If `done` never arrives, the client unmutes and returns to idle
  instead of hanging without sound and hearing.

## 6. What doesn't work, and why (DAT SDK field notes)

Useful if you build on the Wearables Device Access Toolkit yourself.

- **`capturePhoto()` is unusable for this.** Without a running stream it fails instantly with
  `DeviceDisconnected`. After the stream reaches `STREAMING`, it hangs 10 s and fails with
  `CaptureFailed` (an internal SDK timeout). The fallback then raises the camera again, slowly: 27 s
  total instead of 6.6 s. The `if (!isStreaming) return` guard in Meta's own sample reflects a real
  SDK requirement. The one working path: start the video stream and take a fresh frame from it.
- **Frames are portrait 504×896.** That's the video preview, not a full photo, so small text reads
  worse than it could.
- **Version skew looks like your bug.** The SDK version in the app, the Meta AI app, and the DAT
  component on the glasses must match. A mismatch produces symptoms that look exactly like
  application bugs.
- **The wake word can't hear through music.** A2DP output and the SCO mic compete for one radio.
- **The image is not kept in dialogue history.** A follow-up about the same scene re-looks
  (another 6–14 s).

## 7. TTS lessons (Qwen3-TTS realtime)

- **Set `language_type` explicitly.** It's formally optional and defaults to `Auto`, which
  guesses the language *per fragment*. With short streamed sentences that produces broken
  pronunciation. Exposed as `QWEN_TTS_LANGUAGE`.
- **The session silently accepts any field.** Send `"nonsense": "Russian"` and synthesis still
  succeeds. You can't find parameter names by trial and error, only by reading the docs.
- **Feeding whole text vs. sentence by sentence:** whole text starts *faster* (0.34 s vs 1.13 s) and
  sounds more natural. The model waits for a meaningful fragment before it starts speaking.

## 8. Where to put the server

The deciding factor is **latency to the speech provider**. The server calls it several times per
turn (STT upload, vision frame, TTS chunks). The phone keeps only one persistent WebSocket.

| | server in Moscow | server in Singapore |
|---|---|---|
| ping to DashScope (intl) | 200 ms | **2 ms** |
| STT, real | 1.65–6.86 s | **0.42–0.51 s** |
| TTS to first sound | 1.27–6.21 s | **0.26–0.34 s** |
| whole turn | 11–37 s | **2–6 s** |

The phone-to-server leg got worse (281 ms vs 85 ms from the author's city), but it's one connection. The trade is worth it.

**A bonus lesson about DNS:** an unreachable public resolver listed *first* in `resolv.conf` adds
a silent **5 s** to every uncached name lookup. There are no errors in the logs, just waiting. It
caused "sometimes fast, sometimes 37 s" turns. Keep public resolvers as fallback only.

## 9. Debugging

```bash
# phone logs: silence Meta telemetry first, or it floods the buffer within minutes
adb shell setprop log.tag.SupernovaDeviceTelemetryLogger S
adb logcat -G 32M
adb logcat -d -v time | grep -aE "DIAG|glasses:|CameraAccess:"
# note: `adb logcat -s glasses:agent` doesn't work, because the colon is parsed as tag:priority

# server logs (Docker)
docker compose logs -f smotri

# SDK API without docs: what's actually in your version
unzip -oq mwdat-camera-0.9.0.aar && unzip -oq classes.jar -d cls
javap -classpath cls com.meta.wearable.dat.camera.Camera
```

The server log prints a timing line per turn: time to first text, time to first audio, and the length of the spoken answer. A long answer makes a turn feel slow even when the model replied in 1.6 s.
