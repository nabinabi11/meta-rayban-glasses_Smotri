# WebSocket protocol

The server doesn't know which device is on the other end. **Any client that speaks this protocol
works with the agent**: the Android app for glasses, a web page, an iOS app, an ESP32 with a mic.
The source of truth is the docstring at the top of [`server/backend/server.py`](../server/backend/server.py).

## Connect

```
ws://<host>:8000/ws?token=<SMOTRI_TOKEN>
```

A wrong or missing token is rejected during the handshake (HTTP 403 / close code 4401), before
any model is called.

All messages are JSON text. Binary payloads are base64. **Audio everywhere is PCM16, mono, 24 kHz**,
one format from mic to speaker, never re-encoded.

## Client → server

| Message | Meaning |
|---|---|
| `{"type":"audio","data":"<b64 pcm16>"}` | mic chunk while the user speaks (the app sends 100 ms chunks) |
| `{"type":"commit"}` | the user finished speaking: process the turn |
| `{"type":"frame","data":"<b64 jpeg>"}` | a camera frame (normally as the answer to `capture`) |
| `{"type":"cancel"}` / `{"type":"abort"}` | drop buffered audio / interrupt the current answer |
| `{"type":"reset"}` | forget the dialogue history |
| `{"type":"location","lat":..,"lon":..}` | phone location → the agent's context ("nearby", address) |
| `{"type":"config","voice":"Kai",...}` | per-session override of `config.yaml` switches |
| `{"type":"ping"}` | keep-alive → `pong` |

## Server → client

| Message | Meaning |
|---|---|
| `{"type":"ready"}` | session is up |
| `{"type":"status","state":"idle\|thinking\|speaking"}` | agent state for the UI |
| `{"type":"user_text","text":"..."}` | what was heard (transcript) |
| `{"type":"text","delta":"..."}` | answer text, streamed |
| `{"type":"audio","data":"<b64 pcm16>"}` | answer speech, streamed: play it as it comes |
| `{"type":"capture"}` | **"give me a fresh frame"**: raise the camera and send one `frame` |
| `{"type":"look"}` | the agent is looking (UI hint / shutter sound) |
| `{"type":"tool","name":"web_search"}` | the agent called a tool (activity feed) |
| `{"type":"media","action":"play\|pause\|resume\|seek\|stop","url":"...","title":"..."}` | play radio/music on the client |
| `{"type":"done"}` | turn finished |
| `{"type":"error","message":"..."}` | something failed |

## `capture` is the only reverse request

This is the one place where the server asks the client to do something and waits for the
result. It waits **16 s** (the glasses' camera needs up to ~14 s). On timeout, the server
deliberately **does not** fall back to an older frame. Doing that caused an "off by one" bug:
the agent confidently described the scene from the *previous* question. It's better to say
honestly that there's no frame.

## Minimal client loop

```
connect → wait "ready"
loop:
  user speaks → send audio chunks → send commit
  on "capture"  → send one fresh frame
  on "audio"    → play
  on "media"    → play/pause the URL
  on "done"     → back to listening
```
