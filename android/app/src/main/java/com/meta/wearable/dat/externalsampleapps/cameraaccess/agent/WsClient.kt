/*
 * WsClient — WebSocket client for the Smotri server (/ws). See docs/PROTOCOL.md.
 *
 *   client → server: frame{data} | audio{data} | commit | cancel | abort | reset | ping | location | config
 *   server → client: ready | status{state} | user_text | text{delta} | audio{data} | capture | look | tool | media | done | error{message} | pong
 *
 * Audio and frames travel as base64 strings inside JSON.
 */
package com.meta.wearable.dat.externalsampleapps.cameraaccess.agent

import android.util.Base64
import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/** Server callbacks → UI/logic. Called on OkHttp's network thread. */
interface WsCallbacks {
    fun onReady() {}
    fun onStatus(state: String) {}
    fun onUserText(text: String) {}
    fun onText(delta: String) {}
    fun onTool(name: String) {}
    fun onAudio(pcm: ByteArray) {}
    fun onLook() {}
    fun onCapture() {}   // the server wants a fresh frame for look
    fun onMedia(action: String, url: String, title: String) {}   // play/stop media
    fun onDone() {}
    fun onError(message: String) {}
    fun onClosed(reason: String) {}
}

class WsClient(private val url: String, private val cb: WsCallbacks) {

    private val client = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)        // keep-alive
        .readTimeout(0, TimeUnit.MILLISECONDS)     // long-lived
        .build()

    @Volatile private var ws: WebSocket? = null
    @Volatile var isOpen = false; private set

    @Volatile private var closedByUs = false
    private var attempts = 0
    private val reconnector = Executors.newSingleThreadScheduledExecutor()

    fun connect() {
        closedByUs = false
        val req = Request.Builder().url(url).build()
        ws = client.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                isOpen = true
                attempts = 0
                Log.i(TAG, "ws open")
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                try {
                    val m = JSONObject(text)
                    when (m.optString("type")) {
                        "ready" -> cb.onReady()
                        "status" -> cb.onStatus(m.optString("state"))
                        "user_text" -> cb.onUserText(m.optString("text"))
                        "text" -> cb.onText(m.optString("delta"))
                        "tool" -> cb.onTool(m.optString("name"))
                        "audio" -> {
                            val pcm = Base64.decode(m.optString("data"), Base64.DEFAULT)
                            cb.onAudio(pcm)
                        }
                        "look" -> cb.onLook()
                        "capture" -> cb.onCapture()
                        "media" -> cb.onMedia(
                            m.optString("action"), m.optString("url"), m.optString("title"))
                        "done" -> cb.onDone()
                        "error" -> cb.onError(m.optString("message"))
                        "pong" -> {}
                    }
                } catch (e: Exception) {
                    Log.w(TAG, "bad message: ${e.message}")
                }
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                isOpen = false
                webSocket.close(1000, null)
                cb.onClosed(reason)
                scheduleReconnect()
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                isOpen = false
                Log.e(TAG, "ws failure: ${t.message} (http ${response?.code})")
                // the server rejects a wrong token during the handshake → HTTP 403
                cb.onError(if (response?.code == 403) "server rejected the token — re-paste the address"
                           else t.message ?: "ws failure")
                scheduleReconnect()
            }
        })
    }

    /** Reconnect with exponential backoff (recovers from Doze / timeouts). */
    private fun scheduleReconnect() {
        if (closedByUs) return
        attempts++
        val delay = minOf(
            Config.RECONNECT_MAX_MS,
            Config.RECONNECT_MIN_MS * (1L shl minOf(attempts - 1, 4)),
        )
        Log.i(TAG, "reconnect in ${delay}ms (attempt $attempts)")
        reconnector.schedule({ if (!closedByUs) connect() }, delay, TimeUnit.MILLISECONDS)
    }

    // ── client → server ──────────────────────────────────────────────────────
    fun sendFrame(jpeg: ByteArray) = sendData("frame", jpeg)
    fun sendAudio(pcm: ByteArray) = sendData("audio", pcm)
    fun commit() = sendType("commit")
    fun cancel() = sendType("cancel")
    fun abort() = sendType("abort")
    fun reset() = sendType("reset")
    fun ping() = sendType("ping")

    /** Switch the voice on the fly (the server overrides it for the session). */
    fun sendVoice(voice: String) {
        ws?.send(JSONObject().put("type", "config").put("voice", voice).toString())
    }

    /** Phone location → agent context. */
    fun sendLocation(lat: Double, lon: Double) {
        ws?.send(JSONObject().put("type", "location").put("lat", lat).put("lon", lon).toString())
    }

    private fun sendType(type: String) {
        ws?.send(JSONObject().put("type", type).toString())
    }

    private fun sendData(type: String, bytes: ByteArray) {
        val b64 = Base64.encodeToString(bytes, Base64.NO_WRAP)
        ws?.send(JSONObject().put("type", type).put("data", b64).toString())
    }

    fun close() {
        closedByUs = true
        isOpen = false
        ws?.close(1000, "bye")
        ws = null
        reconnector.shutdownNow()
    }

    companion object { private const val TAG = "glasses:ws" }
}
