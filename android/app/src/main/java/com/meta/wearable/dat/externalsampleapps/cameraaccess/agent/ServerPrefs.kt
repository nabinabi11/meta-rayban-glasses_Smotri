/*
 * ServerPrefs — the server address the user pasted into the app.
 *
 * On start the server prints a connection line like
 *   ws://203.0.113.7:8000/ws?token=AbC...
 * It's pasted as a whole into the "Server address" field; the token travels as a query param.
 */
package com.meta.wearable.dat.externalsampleapps.cameraaccess.agent

import android.content.Context

object ServerPrefs {
    private const val FILE = "smotri"
    private const val KEY_URL = "server_url"

    fun get(ctx: Context): String =
        ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).getString(KEY_URL, "") ?: ""

    fun set(ctx: Context, url: String) {
        ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).edit()
            .putString(KEY_URL, normalize(url)).apply()
    }

    /** null = the line is fine; otherwise an error message for the UI. */
    fun validate(raw: String): String? {
        val u = normalize(raw)
        if (!u.startsWith("ws://") && !u.startsWith("wss://")) return "Address must start with ws:// or wss://"
        if (!u.contains("token=")) return "Missing ?token=… — copy the whole line from the server log"
        if (u.contains("<SERVER_IP>")) return "Replace <SERVER_IP> with your server's IP address"
        return null
    }

    private fun normalize(raw: String): String = raw.trim().replace(Regex("\\s+"), "")
}
