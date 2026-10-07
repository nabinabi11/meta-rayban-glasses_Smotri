/*
 * Vad — a simple energy-based end-of-utterance detector (endpointing).
 * Speech accumulated → 1 s of silence → end of sentence.
 */
package com.meta.wearable.dat.externalsampleapps.cameraaccess.agent

class Vad(
    private val sampleRate: Int = Config.MIC_SAMPLE_RATE,
    private val rmsThreshold: Double = Config.VAD_RMS_THRESHOLD,
    private val silenceMs: Int = Config.VAD_SILENCE_MS,
    private val minSpeechMs: Int = Config.VAD_MIN_SPEECH_MS,
) {
    private var speechMs = 0.0
    private var silMs = 0.0
    private var speechStarted = false
    var lastRms = 0.0; private set

    val debug: String get() = "rms=%.3f sp=%.0f sil=%.0f thr=%.3f".format(lastRms, speechMs, silMs, rmsThreshold)

    fun reset() { speechMs = 0.0; silMs = 0.0; speechStarted = false }

    /** Louder than the threshold? (detects speech start in the follow-up window). */
    fun isSpeech(pcm: ByteArray): Boolean {
        val samples = pcm.size / 2
        return samples > 0 && rms(pcm, samples) > rmsThreshold
    }

    /** Feed a PCM16 chunk. true → the utterance ended (time to commit). */
    fun feed(pcm: ByteArray): Boolean {
        val samples = pcm.size / 2
        if (samples == 0) return false
        val chunkMs = samples.toDouble() / sampleRate * 1000.0
        val level = rms(pcm, samples)
        lastRms = level
        if (level > rmsThreshold) {
            speechStarted = true
            speechMs += chunkMs
            silMs = 0.0
        } else if (speechStarted) {
            silMs += chunkMs
            if (silMs >= silenceMs && speechMs >= minSpeechMs) return true
        }
        return false
    }

    private fun rms(pcm: ByteArray, samples: Int): Double {
        var sum = 0.0
        var i = 0
        while (i < samples * 2) {
            val s = ((pcm[i].toInt() and 0xFF) or (pcm[i + 1].toInt() shl 8)).toShort()
            val v = s / 32768.0
            sum += v * v
            i += 2
        }
        return Math.sqrt(sum / samples)
    }
}
