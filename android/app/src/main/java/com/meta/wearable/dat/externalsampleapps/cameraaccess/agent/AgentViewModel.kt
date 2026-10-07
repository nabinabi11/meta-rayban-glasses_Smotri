/*
 * AgentViewModel — the client's orchestration.
 *
 * Ties together: glasses connection (WearablesViewModel) + camera frames (StreamViewModel,
 * video stream, no shutter) + Bluetooth audio (AudioBridge) + server (WsClient) + endpointing (Vad).
 *
 * Turn flow:
 *   wake word / button → startTalk() → stream audio chunks + VAD
 *   silence (VAD) → stopTalk() → commit
 *   server: text → accumulate; audio → play in the glasses (mic muted = half-duplex);
 *           capture → raise the camera, send one fresh frame
 *   done → follow-up window (no wake word needed), then idle
 */
package com.meta.wearable.dat.externalsampleapps.cameraaccess.agent

import android.app.Application
import android.graphics.Bitmap
import android.util.Log
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.meta.wearable.dat.externalsampleapps.cameraaccess.stream.StreamViewModel
import java.io.ByteArrayOutputStream
import java.util.concurrent.Executors
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

enum class AgentState { DISCONNECTED, IDLE, LISTENING, THINKING, SPEAKING, FOLLOWUP }

/** One exchange in the feed: user utterance, agent answer, the frame (if it looked), turn time. */
data class Exchange(val user: String, val agent: String, val frame: Bitmap? = null,
                    val durationSec: Double? = null)

/** Player state for the UI. durationMs=0 → live stream (radio), no seeking. */
data class MediaUi(val title: String, val playing: Boolean,
                   val positionMs: Long, val durationMs: Long)

data class AgentUiState(
    val state: AgentState = AgentState.DISCONNECTED,
    val userText: String = "",   // what was heard from the user (transcript)
    val answer: String = "",     // the agent's answer (streamed)
    val voice: String = "",      // currently selected voice (live switching)
    val tools: List<String> = emptyList(),   // this turn's activity feed
    val lastFrame: Bitmap? = null,            // the frame taken in the CURRENT turn (look), a snapshot
    val history: List<Exchange> = emptyList(),  // previous exchanges, oldest first
    val warmingUp: Boolean = true,            // warming up at start: can't talk yet
    val lastTurnSec: Double? = null,          // turn time: from "thinking" to the end of speech
    val media: MediaUi? = null,               // current player (null = nothing playing)
    val lastError: String? = null,
)

/** Server tool name → human-readable label for the activity feed. */
private fun toolLabel(name: String): String = when (name) {
    "look" -> "👁 looking"
    "web_search" -> "🌐 searching the web"
    "get_weather" -> "🌤 checking the weather"
    "find_place" -> "🗺 searching the map"
    "play_media", "play_track" -> "🎵 starting music"
    "stop_media", "control_media" -> "⏯ player"
    "remember" -> "💾 remembering"
    "read_file" -> "📂 reading notes"
    "write_file", "append_file" -> "✍️ writing notes"
    else -> "⚙ $name"
}

class AgentViewModel(app: Application) : AndroidViewModel(app) {

    private val _ui = MutableStateFlow(AgentUiState())
    val ui: StateFlow<AgentUiState> = _ui.asStateFlow()

    private val audio = AudioBridge(app)
    private val sounds = Sounds(app)
    private val media = MediaBridge(app)   // radio / tracks (ExoPlayer)
    private var ws: WsClient? = null
    private val vad = Vad()
    private var wake: WakeWord? = null
    private var geo: Geo? = null

    // a separate thread for blocking writes to the player (never block the network callback)
    private val playbackExec = Executors.newSingleThreadExecutor()

    private var streamVM: StreamViewModel? = null

    @Volatile private var capturing = false
    @Volatile private var captureStartMs = 0L   // capture start (for the start-cue grace)
    @Volatile private var cueGrace = true       // apply the grace (only when the start cue played)
    @Volatile private var warmingUp = true      // warming up at start: no utterances yet
    @Volatile private var started = false
    private var micChunks = 0
    // pre-roll: ring buffer of the last PREROLL_MS of mic audio (see Config.PREROLL_MS)
    private val preRoll = ArrayDeque<ByteArray>()
    private var preRollBytes = 0
    private val preRollMax = Config.MIC_SAMPLE_RATE * 2 * Config.PREROLL_MS / 1000
    @Volatile private var playGen = 0   // playback generation: chunks of an old answer are dropped
    private var followupJob: Job? = null
    private var turnWatchdog: Job? = null   // safety net for a stuck turn (no done)
    private var listenWatchdog: Job? = null // safety net for a stuck capture (VAD never got enough speech)
    private var followupSpeechMs = 0.0
    // Is a frame capture in flight? Then the safety stopCamera() in onDone must NOT turn the
    // camera off: the server gives up before it reaches STREAMING (~5 s) and used to kill it midway.
    @Volatile private var captureInFlight = false
    @Volatile private var thinkStartMs = 0L          // when the turn started processing ("thinking")
    @Volatile private var playbackEndMs = 0L         // estimated end of the spoken answer

    /** Start the agent: audio route to the glasses, player, capture, WS, frames.
     *  Call AFTER the glasses are connected and camera permission is granted (stream running). */
    fun start(streamVM: StreamViewModel) {
        if (started) return
        started = true
        this.streamVM = streamVM

        audio.routeToGlasses()
        sounds.preferredDevice = audio.communicationDevice()   // sound cues go to the glasses
        audio.startPlayback()
        audio.startCapture { pcm -> onMicPcm(pcm) }

        ws = WsClient(ServerPrefs.get(getApplication()), callbacks).also { it.connect() }
        // offline wake word (Vosk); only listened to while idle
        wake = WakeWord(getApplication()) { startTalk() }.also { it.init() }
        // phone location → server (for "nearby" / maps)
        geo = Geo(getApplication()).also { it.start { lat, lon -> ws?.sendLocation(lat, lon) } }

        // warm-up: don't allow talking for a couple of seconds while the audio route,
        // WS, wake word and location come up
        viewModelScope.launch {
            delay(Config.WARMUP_MS)
            warmingUp = false
            _ui.update { it.copy(warmingUp = false) }
            Log.i(TAG, "warmup done → ready")
        }

        // player ticker: refresh position/duration/status for the UI
        viewModelScope.launch {
            while (true) {
                val m = if (media.hasMedia)
                    MediaUi(media.title, media.isPlaying, media.positionMs, media.durationMs) else null
                if (_ui.value.media != m) _ui.update { it.copy(media = m) }
                delay(500)
            }
        }
    }

    // ── player controls from the UI ─────────────────────────────────────────
    fun mediaToggle() { media.togglePlay() }
    fun mediaSeekTo(ms: Long) { media.seekTo(ms) }
    fun mediaStop() { media.stop() }

    // ── microphone ──────────────────────────────────────────────────────────
    private fun onMicPcm(pcm: ByteArray) {
        if (capturing) {
            ws?.sendAudio(pcm)
            // while the "listening" cue plays, don't feed the VAD: the cue itself looks like
            // speech → then silence → a premature commit before you even started talking
            if (cueGrace && android.os.SystemClock.elapsedRealtime() - captureStartMs < Config.CUE_GRACE_MS) return
            val ended = vad.feed(pcm)
            if (++micChunks % 20 == 0) Log.i(TAG, "cap ${vad.debug}")
            if (ended) { Log.i(TAG, "VAD end → commit"); stopTalk() }   // end of utterance by silence
            return
        }
        // not capturing: keep the audio in the pre-roll so the start of a sentence isn't lost
        synchronized(preRoll) {
            preRoll.addLast(pcm); preRollBytes += pcm.size
            while (preRollBytes > preRollMax && preRoll.isNotEmpty()) {
                preRollBytes -= preRoll.removeFirst().size
            }
        }
        when (_ui.value.state) {
            // follow-up window: a new utterance without the wake word, catch the start of speech
            AgentState.FOLLOWUP -> if (detectFollowupStart(pcm)) startTalk(cue = false)
            // idle: listen for the wake word
            AgentState.IDLE -> wake?.feed(pcm)
            else -> {}
        }
    }

    /** FOLLOWUP_START_MS of continuous speech → start a new utterance. */
    private fun detectFollowupStart(pcm: ByteArray): Boolean {
        val ms = (pcm.size / 2).toDouble() / Config.MIC_SAMPLE_RATE * 1000.0
        if (vad.isSpeech(pcm)) {
            followupSpeechMs += ms
            if (followupSpeechMs >= Config.FOLLOWUP_START_MS) { followupSpeechMs = 0.0; return true }
        } else {
            followupSpeechMs = 0.0
        }
        return false
    }

    /** Start capturing an utterance. cue=true (button / wake word): with the "listening" sound and
     *  grace. cue=false (continuing in the follow-up window): NO sound and no grace, the
     *  conversation is already going and a cue would only distract. */
    fun startTalk(cue: Boolean = true) {
        if (capturing || warmingUp || _ui.value.state == AgentState.DISCONNECTED) return
        // if the agent is still speaking, interrupt it (thread-safe: flag + flush on the playback thread)
        if (_ui.value.state == AgentState.SPEAKING) {
            ws?.abort()
            resetPlayback()                 // cut the speech without a race
            audio.setMicMuted(false)
        }
        followupJob?.cancel()
        followupSpeechMs = 0.0
        vad.reset()
        wake?.reset()
        micChunks = 0
        if (cue) sounds.playStart()   // "listening" cue: only for a new call, not a continuation
        // send the pre-roll: it holds the start of the sentence (and the wake word itself),
        // which would otherwise be lost while Vosk recognizes it
        synchronized(preRoll) {
            var sent = 0
            for (chunk in preRoll) { ws?.sendAudio(chunk); sent += chunk.size }
            if (sent > 0) Log.i(TAG, "pre-roll: sent ${sent * 1000 / (Config.MIC_SAMPLE_RATE * 2)}ms of audio")
            preRoll.clear(); preRollBytes = 0
        }
        captureStartMs = android.os.SystemClock.elapsedRealtime()
        cueGrace = cue                // the grace only exists so the start cue doesn't confuse the VAD
        capturing = true
        Log.i(TAG, "startTalk → LISTENING")
        // safety net: quiet speech may never reach VAD_MIN_SPEECH_MS and hang forever
        listenWatchdog?.cancel()
        listenWatchdog = viewModelScope.launch {
            delay(Config.LISTEN_MAX_MS)
            if (capturing) {
                Log.w(TAG, "DIAG listen watchdog ${Config.LISTEN_MAX_MS}ms → forcing commit")
                stopTalk()
            }
        }
        media.pause()   // duck music/radio during the utterance
        _ui.update {
            // the previous exchange (with its frame and turn time) goes to history
            val hist = if (it.userText.isNotBlank() || it.answer.isNotBlank())
                (it.history + Exchange(it.userText, it.answer, it.lastFrame, it.lastTurnSec)).takeLast(8)
                else it.history
            it.copy(state = AgentState.LISTENING, userText = "", answer = "", tools = emptyList(),
                    lastFrame = null, lastTurnSec = null, history = hist)
        }
    }

    /** Finish the utterance → commit. */
    fun stopTalk() {
        if (!capturing) return
        capturing = false
        listenWatchdog?.cancel()
        Log.i(TAG, "stopTalk → commit")
        resetPlayback()   // drop leftovers of the previous answer so they don't overlap the new one
        // no frame on commit: look requests a fresh one on demand
        ws?.commit()
        thinkStartMs = android.os.SystemClock.elapsedRealtime()   // start of the turn timer
        playbackEndMs = 0L
        sounds.startWaiting()   // "waiting" sound while the agent thinks
        _ui.update { it.copy(state = AgentState.THINKING) }
        // watchdog: if the turn hangs (no done after a drop/server error), recover on our own
        turnWatchdog?.cancel()
        turnWatchdog = viewModelScope.launch {
            delay(Config.TURN_WATCHDOG_MS)
            val st = _ui.value.state
            if (st == AgentState.THINKING || st == AgentState.SPEAKING) {
                Log.w(TAG, "watchdog: turn stuck → reset to IDLE")
                capturing = false
                sounds.stopWaiting()
                audio.setMicMuted(false)
                resetPlayback()
                _ui.update { it.copy(state = AgentState.IDLE, lastError = "turn interrupted") }
            }
        }
    }

    /** Switch the voice on the fly (to preview it). */
    fun setVoice(name: String) {
        ws?.sendVoice(name)
        _ui.update { it.copy(voice = name) }
    }

    /** Follow-up window after an answer: wait for a new utterance without the wake word; silence → idle. */
    private fun enterFollowup() {
        followupSpeechMs = 0.0
        _ui.update { it.copy(state = AgentState.FOLLOWUP) }
        followupJob?.cancel()
        followupJob = viewModelScope.launch {
            delay(Config.FOLLOWUP_TIMEOUT_MS)
            if (_ui.value.state == AgentState.FOLLOWUP) {
                sounds.playEvent()   // window closed: the wake word is needed again
                _ui.update { it.copy(state = AgentState.IDLE) }
            }
        }
    }

    /** Wait for the camera to warm up (STREAMING) and a fresh frame → an immutable snapshot. */
    private suspend fun awaitFreshFrame(timeoutMs: Long): Bitmap? {
        val deadline = android.os.SystemClock.elapsedRealtime() + timeoutMs
        while (android.os.SystemClock.elapsedRealtime() < deadline) {
            val st = streamVM?.uiState?.value?.streamState
            val f = streamVM?.uiState?.value?.videoFrame
            if (st == com.meta.wearable.dat.camera.types.StreamState.STREAMING && f != null) {
                delay(Config.CAPTURE_DELAY_MS)   // let a fresher frame arrive (stream buffer lag)
                val ff = streamVM?.uiState?.value?.videoFrame ?: f
                // snapshot copy: the stream reuses its bitmap, otherwise the preview "breathes"
                return runCatching { ff.copy(ff.config ?: Bitmap.Config.ARGB_8888, false) }.getOrNull() ?: ff
            }
            delay(80)
        }
        // DIAG: where exactly we got stuck: stream state and whether any frame arrived
        Log.w(TAG, "DIAG awaitFreshFrame TIMEOUT ${timeoutMs}ms: " +
            "streamState=${streamVM?.uiState?.value?.streamState} " +
            "videoFrame=${streamVM?.uiState?.value?.videoFrame != null} " +
            "frameCount=${streamVM?.uiState?.value?.videoFrameCount}")
        return null
    }

    // ── frame (video stream → JPEG), on request only ────────────────────────
    private fun encodeJpeg(src: Bitmap): ByteArray {
        val scaled = if (src.width > Config.FRAME_MAX_WIDTH) {
            val h = src.height * Config.FRAME_MAX_WIDTH / src.width
            Bitmap.createScaledBitmap(src, Config.FRAME_MAX_WIDTH, h, true)
        } else src
        val out = ByteArrayOutputStream()
        scaled.compress(Bitmap.CompressFormat.JPEG, Config.FRAME_JPEG_QUALITY, out)
        return out.toByteArray()
    }

    // ── server → client ─────────────────────────────────────────────────────
    private val callbacks = object : WsCallbacks {
        override fun onReady() {
            // recover after a (re)connect: if a turn was cut midway, unmute the mic,
            // stop the "waiting" sound, reset capture/player → clean IDLE
            turnWatchdog?.cancel()
            capturing = false
            sounds.stopWaiting()
            audio.setMicMuted(false)
            resetPlayback()
            _ui.update { it.copy(state = AgentState.IDLE, lastError = null) }
        }

        override fun onStatus(state: String) {
            if (state == "speaking") beginSpeaking()
        }

        override fun onUserText(text: String) {
            _ui.update { it.copy(userText = text) }
        }

        override fun onText(delta: String) {
            _ui.update { it.copy(answer = it.answer + delta) }
        }

        override fun onAudio(pcm: ByteArray) {
            beginSpeaking()
            val g = playGen
            playbackExec.execute { if (g == playGen) audio.writePlayback(pcm) }
            // estimate the end of speech: extend by the chunk duration (PCM16 mono)
            val chunkMs = (pcm.size / 2).toLong() * 1000 / Config.PLAYBACK_SAMPLE_RATE
            val now = android.os.SystemClock.elapsedRealtime()
            playbackEndMs = maxOf(playbackEndMs, now) + chunkMs
        }

        override fun onTool(name: String) {
            _ui.update { it.copy(tools = (it.tools + toolLabel(name)).takeLast(8)) }
        }

        override fun onLook() {
            sounds.playEvent()
            // pin the frame the agent "saw"
            streamVM?.uiState?.value?.videoFrame?.let { f -> _ui.update { it.copy(lastFrame = f) } }
        }

        // The server asks for a frame for look → turn the camera on, wait for a frame, send it.
        // Camera on demand: between looks it is off (the glasses' privacy LED is off too).
        override fun onCapture() {
            sounds.playEvent()   // a soft "looking" sound (ours, not the hardware's loud shutter)
            viewModelScope.launch {
                val t0 = android.os.SystemClock.elapsedRealtime()
                Log.i(TAG, "DIAG onCapture: server asked for a frame")
                captureInFlight = true
                // capturePhoto DOES NOT WORK here (measured): without a stream it fails instantly
                // with DeviceDisconnected; after STREAMING it hangs 10 s and fails with
                // CaptureFailed, and the fallback then raises the camera again slowly (27 s
                // instead of 6.6 s). The one working path: start the stream, take a fresh frame.
                streamVM?.ensureCameraOn()
                val snap = awaitFreshFrame(15000)
                streamVM?.stopCamera()          // camera only for look, never kept warm
                Log.i(TAG, "DIAG onCapture: frame=${snap != null} in " +
                    "${android.os.SystemClock.elapsedRealtime() - t0}ms")
                captureInFlight = false
                if (snap == null) { Log.w(TAG, "capture: no frame"); return@launch }
                _ui.update { it.copy(lastFrame = snap) }
                runCatching { ws?.sendFrame(encodeJpeg(snap)); Log.i(TAG, "capture → frame sent") }
            }
        }

        // media: play / stop / pause / resume / seek (radio, tracks)
        override fun onMedia(action: String, url: String, title: String) {
            viewModelScope.launch {
                when (action) {
                    "play" -> if (url.isNotBlank()) media.play(url, title.ifBlank { "Playing" })
                    "stop" -> media.stop()
                    "pause" -> media.pause()
                    "resume" -> media.resume()
                    "seek" -> media.seekTo(title.toLongOrNull()?.times(1000) ?: 0L)  // title = seconds
                }
            }
        }

        override fun onDone() {
            turnWatchdog?.cancel()
            sounds.stopWaiting()
            // safety: turn the camera off if it's on but no look is in flight (on the main thread).
            // But DON'T touch it while a capture is running: the camera reaches STREAMING only
            // after ~5 s and this stopCamera() used to kill it halfway.
            if (!captureInFlight) viewModelScope.launch { streamVM?.stopCamera() }
            else Log.i(TAG, "DIAG onDone: capture in flight, keeping the camera on")
            // turn time: from "thinking" (commit) to the end of speech (estimated from chunks)
            val now = android.os.SystemClock.elapsedRealtime()
            val endMs = maxOf(playbackEndMs, now)
            if (thinkStartMs > 0) {
                val sec = (endMs - thinkStartMs) / 1000.0
                _ui.update { it.copy(lastTurnSec = sec) }
            }
            // resume music/radio after the answer finishes speaking
            val wait = (endMs - now).coerceIn(0, 8000)
            viewModelScope.launch { delay(wait); media.resume() }
            // let the player drain, unmute the mic and open the follow-up window
            playbackExec.execute {
                try { Thread.sleep(300) } catch (_: InterruptedException) {}
                audio.setMicMuted(false)
                enterFollowup()
            }
        }

        override fun onError(message: String) {
            turnWatchdog?.cancel()
            capturing = false
            sounds.stopWaiting()
            audio.setMicMuted(false)
            _ui.update { it.copy(state = AgentState.IDLE, lastError = message) }
        }

        override fun onClosed(reason: String) {
            // the connection dropped mid-turn: don't get stuck with a muted mic
            turnWatchdog?.cancel()
            capturing = false
            sounds.stopWaiting()
            audio.setMicMuted(false)
            _ui.update { it.copy(state = AgentState.DISCONNECTED) }
        }
    }

    /** New playback generation: old chunks are dropped, the player is flushed (no race). */
    private fun resetPlayback() {
        playGen++
        playbackExec.execute { audio.flushPlayback() }
    }

    private fun beginSpeaking() {
        if (_ui.value.state == AgentState.SPEAKING) return
        sounds.stopWaiting()                // the answer started: stop the "waiting" sound
        audio.setMicMuted(true)             // half-duplex: don't hear our own answer
        media.pause()                       // duck music/radio under the answer (resumed on onDone)
        _ui.update { it.copy(state = AgentState.SPEAKING) }
    }

    companion object { private const val TAG = "glasses:agent" }

    override fun onCleared() {
        super.onCleared()
        followupJob?.cancel()
        listenWatchdog?.cancel()
        ws?.close()
        wake?.release()
        geo?.stop()
        media.release()
        sounds.release()
        audio.release()
        playbackExec.shutdownNow()
    }
}
