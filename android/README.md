# Smotri: Android app

The phone side of Smotri. The glasses connect as a Bluetooth headset (voice) and through Meta's
Wearables Device Access Toolkit (camera on demand). The app streams voice to your Smotri server
and plays the answers in the glasses.

Based on Meta's **CameraAccess** sample from
[meta-wearables-dat-android](https://github.com/facebook/meta-wearables-dat-android). Our code is in
`app/src/main/java/.../cameraaccess/agent/`. The `stream/` and `wearables/` packages come from the sample.

| File | Role |
|---|---|
| `agent/AgentViewModel.kt` | the brain of the client: states, wake word → capture → commit, playback, follow-up window |
| `agent/AudioBridge.kt` | routes mic/speaker to the glasses over Bluetooth SCO |
| `agent/WakeWord.kt` | offline wake word "Smotri" (Vosk), matched only at the start of a phrase |
| `agent/Vad.kt` | end-of-utterance detection |
| `agent/WsClient.kt` | WebSocket client, see [the protocol](../docs/PROTOCOL.md) |
| `agent/ServerPrefs.kt` | the server address the user pasted |
| `agent/Config.kt` | all timing and VAD constants, with the reason behind each one |
| `stream/StreamViewModel.kt` | camera: raise the stream on `capture`, return a fresh frame, stop |

## Build

Requirements: Android Studio (or JDK 17 + Android SDK 36), and a GitHub PAT (classic) with
`read:packages`. Meta's SDK is distributed through GitHub Packages.

```bash
echo "github_token=ghp_..." > local.properties
# wake word model (≈40 MB, not in git)
curl -L -o /tmp/vosk.zip https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
unzip -q /tmp/vosk.zip -d app/src/main/assets && mv app/src/main/assets/vosk-model-small-en-us-0.15 app/src/main/assets/vosk-model-en
uuidgen > app/src/main/assets/vosk-model-en/uuid
./gradlew installDebug
# Russian accent: vosk-model-small-ru-0.22 → assets/vosk-model-ru, then
# ./gradlew installDebug -Psmotri.wake=ru
```

## Run

1. Meta AI app → Settings → App info → tap the version 5 times → **Developer Mode** on.
2. Open Smotri → paste the connection line from your server → **Save**.
3. **Connect glasses** → confirm in Meta AI → **Start agent** → grant permissions.
4. Say "Smotri".

Debug tips are in [How it works §9](../docs/HOW-IT-WORKS.md#9-debugging).
