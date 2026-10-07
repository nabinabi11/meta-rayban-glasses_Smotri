/*
 * AudioBridge — captures the glasses' mic (BT SCO) and plays answers in their speaker.
 *
 * Key decisions:
 *  • Two-way BT audio = HFP/SCO, 16k. Capturing with VOICE_COMMUNICATION gives platform AEC.
 *  • Routed to the glasses' BT headset via setCommunicationDevice (API 31+).
 *  • half-duplex: the mic is muted (setMicMuted) while TTS plays → no echo.
 *
 * Needs RECORD_AUDIO, BLUETOOTH_CONNECT, MODIFY_AUDIO_SETTINGS.
 */
package com.meta.wearable.dat.externalsampleapps.cameraaccess.agent

import android.annotation.SuppressLint
import android.content.Context
import android.media.AudioAttributes
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.util.Log
import kotlin.concurrent.thread

class AudioBridge(private val context: Context) {

    fun interface OnPcm { fun onPcm(pcm: ByteArray) }

    private val am = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager

    private var record: AudioRecord? = null
    private var track: AudioTrack? = null
    private var captureThread: Thread? = null
    @Volatile private var capturing = false
    @Volatile private var micMuted = false
    private var commDevice: AudioDeviceInfo? = null

    /** The device audio is routed to (the glasses), so sound cues go there too. */
    fun communicationDevice(): AudioDeviceInfo? = commDevice

    /** Half-duplex: mute the mic while the agent speaks, so it doesn't hear itself. */
    fun setMicMuted(muted: Boolean) { micMuted = muted }

    // ── route to the glasses ─────────────────────────────────────────────────
    @SuppressLint("MissingPermission")
    fun routeToGlasses() {
        am.mode = AudioManager.MODE_IN_COMMUNICATION
        val dev = am.availableCommunicationDevices.firstOrNull {
            it.type == AudioDeviceInfo.TYPE_BLE_HEADSET ||
                it.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO
        }
        if (dev != null) {
            val ok = am.setCommunicationDevice(dev)
            commDevice = dev
            Log.i(TAG, "route to ${dev.productName} (type=${dev.type}) ok=$ok")
        } else {
            Log.w(TAG, "no BT/BLE headset among communication devices: audio will bypass the glasses")
        }
    }

    fun clearRoute() {
        am.clearCommunicationDevice()
        am.mode = AudioManager.MODE_NORMAL
    }

    // ── mic capture ──────────────────────────────────────────────────────────
    @SuppressLint("MissingPermission")
    fun startCapture(onPcm: OnPcm) {
        if (capturing) return
        val minBuf = AudioRecord.getMinBufferSize(
            Config.MIC_SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        val buf = maxOf(minBuf, Config.MIC_SAMPLE_RATE / 1000 * 2 * Config.AUDIO_BATCH_MS) // ~AUDIO_BATCH_MS
        record = AudioRecord(
            MediaRecorder.AudioSource.VOICE_COMMUNICATION, // platform AEC/NS/AGC
            Config.MIC_SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            buf,
        )
        capturing = true
        record?.startRecording()
        captureThread = thread(name = "glasses-mic") {
            val chunk = ByteArray(buf)
            while (capturing) {
                val n = record?.read(chunk, 0, chunk.size) ?: -1
                if (n > 0 && !micMuted) {
                    onPcm.onPcm(chunk.copyOf(n))
                }
            }
        }
        Log.i(TAG, "capture started @${Config.MIC_SAMPLE_RATE}Hz")
    }

    fun stopCapture() {
        capturing = false
        try { captureThread?.join(300) } catch (_: InterruptedException) {}
        captureThread = null
        record?.let { try { it.stop() } catch (_: Exception) {}; it.release() }
        record = null
    }

    // ── answer playback ──────────────────────────────────────────────────────
    fun startPlayback() {
        if (track != null) return
        val minBuf = AudioTrack.getMinBufferSize(
            Config.PLAYBACK_SAMPLE_RATE,
            AudioFormat.CHANNEL_OUT_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        track = AudioTrack.Builder()
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build()
            )
            .setAudioFormat(
                AudioFormat.Builder()
                    .setSampleRate(Config.PLAYBACK_SAMPLE_RATE)
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                    .build()
            )
            .setBufferSizeInBytes(maxOf(minBuf, Config.PLAYBACK_SAMPLE_RATE)) // ~0.5 s buffer
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()
        track?.play()
    }

    /** Write a speech chunk (PCM16) to the player. Blocking: call from your own thread. */
    fun writePlayback(pcm: ByteArray) {
        track?.write(pcm, 0, pcm.size, AudioTrack.WRITE_BLOCKING)
    }

    /** Cut playback instantly (barge-in / abort). */
    fun flushPlayback() {
        track?.let { try { it.pause(); it.flush(); it.play() } catch (_: Exception) {} }
    }

    fun stopPlayback() {
        track?.let { try { it.stop() } catch (_: Exception) {}; it.release() }
        track = null
    }

    fun release() {
        stopCapture(); stopPlayback(); clearRoute()
    }

    companion object { private const val TAG = "glasses:audio" }
}
