/*
 * Smotri client config. Every number here was tuned on real glasses; the comment says why.
 */
package com.meta.wearable.dat.externalsampleapps.cameraaccess.agent

import com.meta.wearable.dat.externalsampleapps.cameraaccess.BuildConfig

object Config {
    // The server address is NOT hardcoded: the user pastes the connection line the
    // server prints (ws://<ip>:8000/ws?token=…), see ServerPrefs.
    // usesCleartextTraffic is enabled in the manifest for ws:// on a raw IP.

    // The server speaks 24 kHz (health: audio_sample_rate=24000). BT SCO is physically 16 kHz:
    // we ask AudioRecord for 24 kHz and the OS resamples from SCO. The server stays untouched.
    const val MIC_SAMPLE_RATE = 24000
    const val PLAYBACK_SAMPLE_RATE = 24000

    // frames: JPEG from the video stream (no shutter sound)
    const val FRAME_INTERVAL_MS = 800L
    const val FRAME_MAX_WIDTH = 960     // more detail for vision (look)
    const val FRAME_JPEG_QUALITY = 85
    // delay before taking the frame on look: compensates for the video stream's buffer lag,
    // so the agent gets a frame closer to "the moment of the question"
    const val CAPTURE_DELAY_MS = 400L

    // VAD / capture (ms)
    const val AUDIO_BATCH_MS = 100
    // RMS threshold: the glasses' mic over BT SCO is quiet: speech ≈0.011–0.028, silence ≈0.000–0.003.
    // 0.018 cut half the speech as silence → endpointing drifted. 0.008 separates them cleanly.
    const val VAD_RMS_THRESHOLD = 0.008
    const val VAD_SILENCE_MS = 1000
    const val VAD_MIN_SPEECH_MS = 300
    // while the "listening" cue plays, DON'T feed the VAD (the cue looked like speech).
    // start.wav = 0.71 s; slightly more (800 ms) but NOT longer, or the grace swallows the start
    // of the first sentence, the VAD never sees its end by silence and never commits.
    const val CUE_GRACE_MS = 800L

    // PRE-ROLL. Capture starts ONLY after Vosk recognized the wake word (or after
    // FOLLOWUP_START_MS of speech in the follow-up window): everything said before that was
    // lost and STT got sentences without their start. We keep the last PREROLL_MS of audio
    // and send it to the server before the live stream.
    const val PREROLL_MS = 1200

    // Hard listen limit. If less than VAD_MIN_SPEECH_MS of speech was collected (spoken quietly),
    // the commit condition `sil>=SILENCE && sp>=MIN_SPEECH` is NEVER met and capture hangs
    // forever. After the limit we commit as is: the audio was sent anyway, STT will cope, and
    // the agent asks again if it's nonsense.
    const val LISTEN_MAX_MS = 12000L

    // follow-up: after an answer, keep a conversation window open without the wake word
    const val FOLLOWUP_TIMEOUT_MS = 4000L   // how long to wait for a new utterance
    const val FOLLOWUP_START_MS = 200       // this much continuous speech → start capturing

    // turn watchdog: if no done arrives in this time (drop / server error), the client
    // recovers on its own (unmutes, IDLE) instead of hanging without sound and hearing
    const val TURN_WATCHDOG_MS = 45000L

    // warm-up at start: let the audio route, WS, wake word and location come up before
    // allowing speech, so an utterance doesn't start before the system is ready
    const val WARMUP_MS = 2500L

    // WS auto-reconnect (ms)
    const val RECONNECT_MIN_MS = 1000L
    const val RECONNECT_MAX_MS = 15000L

    // Wake word "Smotri". The English build matches how the English model hears it
    // ("small tree", "smoke tree", ...), the Russian build matches «смотри»; see build.gradle.kts.
    // Only at the START of a phrase (optionally after "hey"/"ok"), so "give me a summary"
    // or «посмотри» mid-sentence don't wake it.
    val WAKE_WORDS: List<String> = BuildConfig.WAKE_WORDS.split("|")
    val WAKE_MODEL_ASSET: String = BuildConfig.WAKE_MODEL_ASSET
    const val WAKE_HINT = "\"Smotri\""
}
