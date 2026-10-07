/*
 * WakeWord — offline wake-word spotting with Vosk.
 *
 * The small model (~40–45 MB) lives in assets/<Config.WAKE_MODEL_ASSET> (bundled by CI,
 * see .github/workflows/release-apk.yml). The mic is fed only while idle (by AgentViewModel);
 * on a match → onWake(). The recognizer runs at the mic rate (Vosk resamples to the model's 16k).
 */
package com.meta.wearable.dat.externalsampleapps.cameraaccess.agent

import android.content.Context
import android.util.Log
import org.json.JSONObject
import org.vosk.Model
import org.vosk.Recognizer
import org.vosk.android.StorageService

class WakeWord(
    private val context: Context,
    private val words: List<String> = Config.WAKE_WORDS,
    private val onWake: () -> Unit,
) {
    private var model: Model? = null
    private var rec: Recognizer? = null
    @Volatile private var ready = false

    /** Unpack the model from assets into filesDir and start the recognizer (async). */
    fun init() {
        StorageService.unpack(
            context, Config.WAKE_MODEL_ASSET, "vosk-model",
            { m ->
                model = m
                rec = Recognizer(m, Config.MIC_SAMPLE_RATE.toFloat())
                ready = true
                Log.i(TAG, "vosk ready (wake=$words)")
            },
            { e -> Log.e(TAG, "vosk unpack failed: ${e.message}", e) },
        )
    }

    fun reset() { runCatching { rec?.reset() } }

    /** Feed a PCM16 chunk (called by AgentViewModel only while idle). */
    fun feed(pcm: ByteArray) {
        val r = rec ?: return
        if (!ready) return
        val text = if (r.acceptWaveForm(pcm, pcm.size))
            JSONObject(r.result).optString("text")
        else
            JSONObject(r.partialResult).optString("partial")
        if (words.any { text.contains(it, ignoreCase = true) }) {
            r.reset()
            Log.i(TAG, "wake! ($text)")
            onWake()
        }
    }

    fun release() {
        runCatching { rec?.close() }
        runCatching { model?.close() }
        rec = null; model = null; ready = false
    }

    companion object { private const val TAG = "glasses:wake" }
}
