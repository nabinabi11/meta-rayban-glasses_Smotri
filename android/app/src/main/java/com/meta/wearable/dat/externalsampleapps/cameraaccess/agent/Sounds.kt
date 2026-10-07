/*
 * Sounds — the agent's sound cues (played in the glasses via the communication route).
 *  • waiting — looped while the agent thinks (THINKING)
 *  • event   — once, when look fires
 * Files: res/raw/waiting.wav, res/raw/event.wav
 */
package com.meta.wearable.dat.externalsampleapps.cameraaccess.agent

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioDeviceInfo
import android.media.MediaPlayer
import com.meta.wearable.dat.externalsampleapps.cameraaccess.R

class Sounds(private val context: Context) {

    private val attrs = AudioAttributes.Builder()
        .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)   // follow the route to the glasses
        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
        .build()

    /** Where to play cues (the glasses). Set by AgentViewModel after routing. */
    var preferredDevice: AudioDeviceInfo? = null

    private var waiting: MediaPlayer? = null

    fun startWaiting() {
        stopWaiting()
        waiting = MediaPlayer.create(context, R.raw.waiting)?.apply {
            setAudioAttributes(attrs)
            setVolume(1f, 1f)
            preferredDevice?.let { setPreferredDevice(it) }
            isLooping = true
            start()
        }
    }

    fun stopWaiting() {
        waiting?.let { runCatching { if (it.isPlaying) it.stop() }; it.release() }
        waiting = null
    }

    fun playStart() = playOnce(R.raw.start)

    fun playEvent() = playOnce(R.raw.event)

    private fun playOnce(resId: Int) {
        val mp = MediaPlayer.create(context, resId) ?: return
        mp.setAudioAttributes(attrs)
        mp.setVolume(1f, 1f)
        preferredDevice?.let { mp.setPreferredDevice(it) }
        mp.setOnCompletionListener { it.release() }
        mp.start()
    }

    fun release() { stopWaiting() }
}
