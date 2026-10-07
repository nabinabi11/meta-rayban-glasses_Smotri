# Smotri — an open-source voice AI agent for Ray-Ban Meta glasses

*Say "Smotri" (Russian for "look!", pronounced SMOH-tree), ask anything. The agent answers in your ear, in your language, and sees through the glasses' camera when it needs to.*

> 🎥 **Demo video:** _coming soon_

**Smotri** is a full working stack: an Android app, a server, and the agent itself. Bring your own
LLM. The glasses do no computing: they are a microphone, a speaker and a camera. The
phone handles the wake word and audio. The server does listening → thinking (+ tools) → speaking.

```
RAY-BAN META                 ANDROID PHONE (this app)                  YOUR SERVER (Docker)
mic + speaker ──BT audio──►  wake word "Smotri" (offline, Vosk)   ─WS─►  speech-to-text
camera ────────DAT SDK────►  VAD · pre-roll buffer · player             LLM brain + tools
                             location                                   text-to-speech
```

## What it can do

- 🗣 **Hands-free conversation**: wake word, then you talk. Follow-up questions don't need the wake word. You can interrupt it.
- 🌍 **Answers in your language**: speech recognition auto-detects the language and the agent replies in it.
- 👁 **Sees on demand**: the brain decides when to `look`. The camera turns on only for that one frame (the privacy LED is on only then).
- 🌐 **Web search**: news, facts, prices (DuckDuckGo, no key needed).
- 📍 **Knows where you are**: "what pharmacies are nearby", "where am I".
- 🧠 **Remembers you**: file-based memory (profile + notes) that the agent reads and writes itself.
- 🎵 **Plays radio and music** through the glasses, with pause, seek and stop by voice.
- ⚡ **Fast**: everything streams. Speech starts **~2–3 s** after you stop talking (measured with the DeepSeek profile, server in Singapore).

## Any model? Mostly yes

| Part | What it uses | Swappable? |
|---|---|---|
| **Brain** (dialogue + tool calling) | any **OpenAI-compatible** endpoint: Qwen, DeepSeek, Claude/GPT/Gemini via OpenRouter, local vLLM/Ollama | ✅ 3 env vars |
| **Vision** | `describe`: any VL model captions the frame for the brain · `direct`: the brain's own vision model answers from the pixels | ✅ |
| **Speech-to-text / text-to-speech** | Alibaba **Qwen3-ASR / Qwen3-TTS** (streaming) · Gemini TTS optional | ⚠️ Alibaba today, pluggable modules |

**Quickest setup: one Alibaba Model Studio key runs everything.** See the profiles in [`server/.env.example`](server/.env.example).

---

## Quick start (~15 minutes)

You need: **Ray-Ban Meta** glasses, an **Android** phone (tested on Pixel 8 Pro), any **Linux VPS**
(1 vCPU / 2 GB, ~$12/month), and an **[Alibaba Model Studio](https://modelstudio.console.alibabacloud.com) API key**.

### 1. Server: one command

```bash
git clone https://github.com/nabinabi11/meta-rayban-glasses_Smotri.git
cd meta-rayban-glasses_Smotri/server
./setup.sh          # installs Docker if needed, asks for your key, starts everything
```

At the end it prints your connection line:

```
  ws://203.0.113.7:8000/ws?token=Xq3...
```

Copy it. That's all for the server. (Open port `8000/tcp` in your cloud firewall if needed.)

> 💡 Put the server **close to your model provider**. Each turn calls the speech APIs several
> times, so the server-to-provider latency matters more than the phone-to-server latency.
> The author's server is in Singapore, next to Alibaba's international region.

### 2. App: install the APK

1. Download **`smotri.apk`** from [**Releases**](../../releases/latest) on your phone and install it
   (allow "install unknown apps" when asked). `smotri-ru.apk` is the same app tuned for a Russian accent.
2. Open it → paste the connection line → **Save**.

### 3. Glasses: enable Developer Mode (one time)

Third-party camera access on Ray-Ban Meta currently goes through Meta's
[Wearables Device Access Toolkit](https://wearables.developer.meta.com/) preview, which needs Developer Mode:

1. **Meta AI** app → Settings → App info → tap the **version number 5 times** → turn on **Developer Mode**.
2. Back in Smotri: **Connect glasses** → confirm in Meta AI → **Start agent** → grant
   microphone, Bluetooth, camera and location.
3. Say **"Smotri"** and ask something. Try *"Smotri, what am I looking at?"* Start the phrase with it:
   the wake word only counts at the beginning, so "give me a summary" mid-sentence won't wake it.

> ⚠️ If the camera behaves strangely, check that three versions match: the SDK in the app, the
> Meta AI app, and the DAT component on the glasses (Meta AI → App Connections). A mismatch looks
> exactly like a bug in our code.

---

## Status and honest limits

- **Languages**: the agent understands and answers in any language Qwen supports. The wake word
  "Smotri" ships for English and Russian accents (two APKs). Other accents need a
  [Vosk model](https://alphacephei.com/vosk/models) and one line in `android/app/build.gradle.kts`.
  Voice output is tuned for English and Russian; set `QWEN_TTS_LANGUAGE` for others.
- **The wake word was tuned offline on synthetic voices** (details in [How it works](docs/HOW-IT-WORKS.md#wake-word)).
  Real voices and accents may need more aliases; issues with "it didn't wake" logs are very welcome.
- **Android only.** There is no iOS client.
- **The camera is the bottleneck**: the glasses' camera takes **6–14 s** to deliver the first frame
  through the SDK. This is outside our control. Details and measurements are in [How it works](docs/HOW-IT-WORKS.md).
- The wake word can't hear you **while music plays** (Bluetooth A2DP and SCO compete for one radio).
- It's a personal project that grew up. It works every day for the author, but it is not a polished product.

## Docs

- [**How it works**](docs/HOW-IT-WORKS.md): the full request path with real timings, the vision handoff
  between models, client tricks, and what doesn't work and why.
- [**WebSocket protocol**](docs/PROTOCOL.md): the contract between server and client. Write your own client (web, iOS, another device).
- [Build the app from source](android/README.md).

## Build the app yourself

```bash
cd android
echo "github_token=ghp_your_read_packages_PAT" > local.properties   # Meta's SDK lives on GitHub Packages
./gradlew installDebug
```

You need JDK 17 and the Android SDK 36. For the wake word, put
[`vosk-model-small-en-us-0.15`](https://alphacephei.com/vosk/models) into
`app/src/main/assets/vosk-model-en/` (Russian accent: `vosk-model-small-ru-0.22` → `vosk-model-ru/`,
build with `-Psmotri.wake=ru`). CI does this automatically, see [`.github/workflows/release-apk.yml`](.github/workflows/release-apk.yml).

## Licenses and credits

- Code in this repository: [MIT](LICENSE), except where noted below.
- `android/` is based on Meta's **CameraAccess** sample from
  [meta-wearables-dat-android](https://github.com/facebook/meta-wearables-dat-android). Files carrying
  Meta's copyright header stay under Meta's license. The **Wearables Device Access Toolkit** is used
  under the [Meta Wearables Developer Terms](https://wearables.developer.meta.com/terms).
- Wake word: [Vosk](https://alphacephei.com/vosk/) (Apache 2.0).

**Not affiliated with or endorsed by Meta Platforms or EssilorLuxottica.** Ray-Ban is a trademark of its owner.

Author: **Nikolai Nabiev**. Issues and PRs are welcome.
