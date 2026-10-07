/*
 * Geo — phone location for the agent (LocationManager, no extra dependencies).
 * Delivers the last known fix right away + subscribes to updates. Needs ACCESS_FINE_LOCATION.
 */
package com.meta.wearable.dat.externalsampleapps.cameraaccess.agent

import android.annotation.SuppressLint
import android.content.Context
import android.location.LocationListener
import android.location.LocationManager
import android.os.Looper
import android.util.Log

class Geo(context: Context) {
    private val lm = context.getSystemService(Context.LOCATION_SERVICE) as LocationManager
    private var listener: LocationListener? = null

    @SuppressLint("MissingPermission")  // the permission is requested in MainActivity
    fun start(onLoc: (Double, Double) -> Unit) {
        val l = LocationListener { loc ->
            Log.i(TAG, "loc ${"%.5f".format(loc.latitude)},${"%.5f".format(loc.longitude)} " +
                "acc=${loc.accuracy.toInt()}m via=${loc.provider}")
            onLoc(loc.latitude, loc.longitude)
        }
        listener = l
        runCatching {
            // GPS first (more precise), frequent updates to stay current on the move
            for (p in listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER)) {
                if (lm.isProviderEnabled(p)) {
                    lm.getLastKnownLocation(p)?.let {
                        Log.i(TAG, "last-known via $p acc=${it.accuracy.toInt()}m")
                        onLoc(it.latitude, it.longitude)
                    }
                    lm.requestLocationUpdates(p, 15_000L, 20f, l, Looper.getMainLooper())
                }
            }
            Log.i(TAG, "geo started")
        }.onFailure { Log.w(TAG, "geo start failed: ${it.message}") }
    }

    fun stop() {
        listener?.let { runCatching { lm.removeUpdates(it) } }
        listener = null
    }

    companion object { private const val TAG = "glasses:geo" }
}
