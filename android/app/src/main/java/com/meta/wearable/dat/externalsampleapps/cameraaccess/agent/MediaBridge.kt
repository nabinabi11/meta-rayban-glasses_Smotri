/*
 * MediaBridge — plays media (radio / podcasts / tracks) by URL with ExoPlayer (media3).
 * Plays as MEDIA (A2DP to the glasses/phone). Paused during the agent's turn, resumed after.
 * Exposes position/duration/status for the UI player. All calls go to the main thread.
 */
package com.meta.wearable.dat.externalsampleapps.cameraaccess.agent

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.util.Log
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.exoplayer.ExoPlayer

class MediaBridge(context: Context) {
    private val app = context.applicationContext
    private var player: ExoPlayer? = null

    @Volatile var title: String = ""; private set

    // ExoPlayer MUST be called from the main thread only (or it crashes). Wake/network
    // callbacks come from other threads → every operation is posted to main.
    private val main = Handler(Looper.getMainLooper())
    private fun onMain(block: () -> Unit) {
        if (Looper.myLooper() == Looper.getMainLooper()) block() else main.post(block)
    }

    private fun ensure() {
        if (player != null) return
        player = ExoPlayer.Builder(app).build().apply {
            setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(C.USAGE_MEDIA)
                    .setContentType(C.AUDIO_CONTENT_TYPE_MUSIC)
                    .build(),
                /* handleAudioFocus = */ true,
            )
        }
    }

    /** Play a stream/track by URL. */
    fun play(url: String, title: String) = onMain {
        ensure()
        this.title = title
        player?.apply {
            setMediaItem(MediaItem.fromUri(url))
            prepare()
            playWhenReady = true
        }
        Log.i(TAG, "media play: $title ($url)")
    }

    fun stop() = onMain {
        player?.apply { stop(); clearMediaItems() }
        title = ""
        Log.i(TAG, "media stop")
    }

    fun pause() = onMain { player?.pause() }
    fun resume() = onMain { val p = player ?: return@onMain; if (p.currentMediaItem != null && !p.isPlaying) p.play() }
    fun togglePlay() = onMain { player?.let { if (it.isPlaying) it.pause() else it.play() } }
    fun seekTo(ms: Long) = onMain { player?.seekTo(ms.coerceAtLeast(0)) }

    val hasMedia: Boolean get() = player?.currentMediaItem != null
    val isPlaying: Boolean get() = player?.isPlaying == true
    val positionMs: Long get() = player?.currentPosition ?: 0L
    /** Duration in ms, or 0 if unknown / live (radio). */
    val durationMs: Long get() = (player?.duration ?: 0L).let { if (it > 0) it else 0L }

    fun release() {
        player?.release()
        player = null
    }

    companion object { private const val TAG = "glasses:media" }
}
