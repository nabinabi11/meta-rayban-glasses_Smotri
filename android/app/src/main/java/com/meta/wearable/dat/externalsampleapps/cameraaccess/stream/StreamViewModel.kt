/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 * All rights reserved.
 *
 * This source code is licensed under the license found in the
 * LICENSE file in the root directory of this source tree.
 */

// StreamViewModel - DAT Camera Streaming API Demo
//
// This ViewModel demonstrates the DAT Camera Streaming APIs for:
// - Creating and managing stream sessions with wearable devices
// - Receiving video frames from device cameras
// - Capturing photos during streaming sessions
// - Handling different video qualities and formats
// - Processing raw video data (I420 -> ARGB conversion)

package com.meta.wearable.dat.externalsampleapps.cameraaccess.stream

import android.annotation.SuppressLint
import android.app.Application
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import android.util.Log
import androidx.core.content.FileProvider
import androidx.exifinterface.media.ExifInterface
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.meta.wearable.dat.camera.Camera
import com.meta.wearable.dat.camera.Stream
import com.meta.wearable.dat.camera.addCamera
import com.meta.wearable.dat.camera.types.CameraState
import com.meta.wearable.dat.camera.types.PhotoData
import com.meta.wearable.dat.camera.types.StreamConfiguration
import com.meta.wearable.dat.camera.types.StreamError
import com.meta.wearable.dat.camera.types.StreamState
import com.meta.wearable.dat.camera.types.VideoFrame
import com.meta.wearable.dat.camera.types.VideoQuality
import com.meta.wearable.dat.core.Wearables
import com.meta.wearable.dat.core.selectors.DeviceSelector
import com.meta.wearable.dat.core.session.DeviceSession
import com.meta.wearable.dat.core.session.DeviceSessionState
import com.meta.wearable.dat.core.types.DeviceSessionError
import com.meta.wearable.dat.externalsampleapps.cameraaccess.wearables.WearablesViewModel
import java.io.ByteArrayInputStream
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

@SuppressLint("AutoCloseableUse")
class StreamViewModel(
    application: Application,
    private val wearablesViewModel: WearablesViewModel,
) : AndroidViewModel(application) {

  companion object {
    private const val TAG = "CameraAccess:StreamViewModel"
    private val INITIAL_STATE = StreamUiState()
    private val SESSION_TERMINAL_STATES = setOf(StreamState.CLOSED)
  }

  private val deviceSelector: DeviceSelector = wearablesViewModel.deviceSelector
  private var session: DeviceSession? = null

  private val _uiState = MutableStateFlow(INITIAL_STATE)
  val uiState: StateFlow<StreamUiState> = _uiState.asStateFlow()

  private var videoJob: Job? = null
  private var stateJob: Job? = null
  private var errorJob: Job? = null
  private var sessionErrorJob: Job? = null
  private var sessionStateJob: Job? = null
  private var stream: Stream? = null
  // SDK 0.9.0: Camera owns the camera, Stream is its child feature (addStream was removed).
  private var camera: Camera? = null
  @Volatile private var cameraStarting = false   // addCamera in progress (async): don't start a second one
  // Was the camera stopped while addCamera was still in flight? Then fold the result right away,
  // or an orphaned camera remains: LED on, resource busy, the next look can't start.
  @Volatile private var cameraCancelled = false

  // DIAG: time since ensureCameraOn(), to see how long a cold start really takes
  @Volatile private var cameraOnAtMs = 0L
  @Volatile private var diagFramesSinceOn = 0
  private fun elapsedSinceCameraOn(): Long =
      if (cameraOnAtMs == 0L) -1 else android.os.SystemClock.elapsedRealtime() - cameraOnAtMs
  private var previousDeviceSessionState: DeviceSessionState? = null

  // Presentation queue for buffering frames after color conversion
  private var presentationQueue: PresentationQueue? = null

  fun startStream() {
    videoJob?.cancel()
    stateJob?.cancel()
    errorJob?.cancel()
    sessionErrorJob?.cancel()
    sessionStateJob?.cancel()
    presentationQueue?.stop()
    presentationQueue = null
    previousDeviceSessionState = null

    // Initialize presentation queue - frames are presented based on timestamp, not arrival time
    // Uses IntArray pooling for efficiency - cheaper than Bitmap.copy()
    val queue =
        PresentationQueue(
            bufferDelayMs = 100L,
            maxQueueSize = 15,
            onFrameReady = { frame ->
              // This is called from the presentation thread at regular intervals
              // when a frame's presentation time has arrived
              viewModelScope.launch(kotlinx.coroutines.Dispatchers.Main) {
                _uiState.update {
                  it.copy(videoFrame = frame.bitmap, videoFrameCount = it.videoFrameCount + 1)
                }
              }
            },
        )
    presentationQueue = queue
    queue.start()
    if (session == null) {
      previousDeviceSessionState = null
      Wearables.createSession(deviceSelector)
          .onSuccess { createdSession ->
            session = createdSession
            sessionErrorJob = viewModelScope.launch {
              createdSession.errors.collect { error -> handleSessionError(error) }
            }
            session?.start()
          }
          .onFailure { error, _ ->
            Log.e(TAG, "Failed to create session: ${error.description}")
            handleSessionError(error)
          }
      if (session == null) return
    }
    startStreamInternal()
  }

  private fun startStreamInternal() {
    Log.d(TAG, "startStreamInternal() - collecting session state")
    sessionStateJob = viewModelScope.launch {
      session?.state?.collect { currentState ->
        val prevState = previousDeviceSessionState
        previousDeviceSessionState = currentState

        if (currentState == DeviceSessionState.STARTED) {
          wearablesViewModel.setDatAppUpdateRequired(false)
          if (prevState == DeviceSessionState.PAUSED && stream != null) {
            // PAUSED → STARTED: device-initiated resume (tap gesture).
            // The SDK handles resume internally via requestCameraOn() → resumeStreaming().
            // Do NOT recreate the stream — just let the SDK resume it.
            Log.d(TAG, "Session resumed from PAUSED — stream stays alive")
            return@collect
          }

          // Camera ON DEMAND: no automatic video stream. The camera (and the glasses'
          // privacy LED) turns on only for look, via ensureCameraOn().
        } else if (currentState == DeviceSessionState.PAUSED) {
          // Tap gesture paused the session — keep the stream alive.
          // The SDK transitions StreamState to PAUSED internally.
          Log.d(TAG, "Session paused (tap gesture) — keeping stream alive for resume")
        }
      }
    }
  }

  /** Start the camera's video stream on demand (for look). Idempotent; the session must be alive. */
  fun ensureCameraOn() {
    if (session == null) {
      Log.w(TAG, "ensureCameraOn: no active session")
      return
    }
    // guard: camera is still null but addCamera is already running (async): don't start a
    // second one, or two parallel addCamera calls → resource busy, no frame arrives
    if (camera != null || cameraStarting) {
      Log.w(TAG, "DIAG ensureCameraOn: SKIP (camera=${camera != null} starting=$cameraStarting)")
      return
    }
    cameraStarting = true
    cameraCancelled = false
    cameraOnAtMs = android.os.SystemClock.elapsedRealtime()
    diagFramesSinceOn = 0
    Log.i(TAG, "DIAG camera ON: addCamera sent")
    videoJob?.cancel()
    stateJob?.cancel()
    errorJob?.cancel()
    session
        // No compressVideo: frames arrive UNcompressed (YUV), as YuvToBitmapConverter expects.
        // compressVideo=true would deliver HEVC and need a MediaCodec decoder.
        ?.addCamera(StreamConfiguration(videoQuality = VideoQuality.MEDIUM, frameRate = 15))
        ?.onSuccess { addedCamera ->
          Log.i(TAG, "DIAG addCamera OK (+${elapsedSinceCameraOn()}ms)")
          cameraStarting = false
          // we were stopped while addCamera was in flight: fold it now, leave no orphan
          if (cameraCancelled) {
            Log.w(TAG, "DIAG addCamera arrived after stopCamera: folding")
            addedCamera.stop()
            return@onSuccess
          }
          camera = addedCamera
          stream = addedCamera.stream
          videoJob = viewModelScope.launch { stream?.videoStream?.collect { handleVideoFrame(it) } }
          stateJob = viewModelScope.launch {
            stream?.state?.collect { streamState ->
              val prev = _uiState.value.streamState
              Log.i(TAG, "DIAG state: $prev -> $streamState (+${elapsedSinceCameraOn()}ms)")
              _uiState.update { it.copy(streamState = streamState) }
              val wasActive = prev !in SESSION_TERMINAL_STATES
              val isTerminated = streamState in SESSION_TERMINAL_STATES
              // unexpected drop (the device closed it): navigate back. OUR stopCamera() cancels
              // this observer BEFORE stop(), so a normal shutdown never gets here
              if (wasActive && isTerminated) {
                stopStream()
                wearablesViewModel.navigateToDeviceSelection()
              }
            }
          }
          errorJob = viewModelScope.launch {
            stream?.errorStream?.collect { error ->
              // DIAG: log ANY stream error, including the swallowed STREAM_ERROR:
              // this is where BATTERY_LOW / THERMAL_HOT / TIMEOUT / PEAK_POWER_LIMIT show up
              Log.e(TAG, "DIAG streamError: $error (+${elapsedSinceCameraOn()}ms)")
              if (error == StreamError.STREAM_ERROR) return@collect
              stopStream()
              wearablesViewModel.navigateToDeviceSelection()
              wearablesViewModel.setRecentError(error.getLocalizedDescription(getApplication()))
            }
          }
          stream?.start()?.onFailure { error, _ ->
            Log.e(TAG, "DIAG stream.start FAILED (+${elapsedSinceCameraOn()}ms): $error / ${error.description}")
            stopCamera()
          }
        }
        ?.onFailure { error, _ ->
          cameraStarting = false
          Log.e(TAG, "DIAG addCamera FAILED (+${elapsedSinceCameraOn()}ms): $error / ${error.description}")
        }
  }

  /** Turn the camera off when idle (LED off), keeping the session alive for the next look.
   *  Observers are cancelled BEFORE stop(), so CLOSED doesn't navigate to the device picker. */
  fun stopCamera() {
    cameraStarting = false
    cameraCancelled = true   // if addCamera is still in flight, its result folds itself
    if (camera == null) return
    Log.d(TAG, "camera OFF (idle)")
    videoJob?.cancel(); videoJob = null
    stateJob?.cancel(); stateJob = null
    errorJob?.cancel(); errorJob = null
    camera?.stop()       // Camera owns the resource: stop it, not the Stream
    camera = null
    stream = null
    _uiState.update { it.copy(streamState = StreamState.CLOSED, videoFrame = null) }
  }

  fun stopStream() {
    videoJob?.cancel()
    videoJob = null
    stateJob?.cancel()
    stateJob = null
    errorJob?.cancel()
    errorJob = null
    sessionErrorJob?.cancel()
    sessionErrorJob = null
    sessionStateJob?.cancel()
    sessionStateJob = null
    presentationQueue?.stop()
    presentationQueue = null
    _uiState.update { INITIAL_STATE }
    cameraStarting = false
    cameraCancelled = true
    camera?.stop()       // Camera owns the camera resource (the Stream closes with it)
    camera = null
    stream = null
    session?.stop()
    session = null
  }

  private fun handleSessionError(error: DeviceSessionError) {
    Log.e(TAG, "Session error: ${error.description}")
    val alreadyShowingUpdateRequired =
        wearablesViewModel.uiState.value.isFirmwareUpdateRequired ||
            wearablesViewModel.uiState.value.isDatAppUpdateRequired

    if (error == DeviceSessionError.DAT_APP_ON_THE_GLASSES_UPDATE_REQUIRED) {
      wearablesViewModel.setDatAppUpdateRequired(true)
    }
    if (alreadyShowingUpdateRequired && error == DeviceSessionError.SESSION_ENDED_BY_DEVICE) {
      stopStream()
      wearablesViewModel.navigateToDeviceSelection()
      return
    }

    wearablesViewModel.setRecentError(error.getLocalizedDescription(getApplication()))
    stopStream()
    wearablesViewModel.navigateToDeviceSelection()
  }

  /**
   * EXPERIMENT (unused): a still WITHOUT the video stream, "like the button on the glasses".
   *
   * addCamera() → wait for CameraState.STARTED → capturePhoto(), WITHOUT `stream.start()`:
   * starting the video pipeline is what costs ~5 s to STREAMING and ~8 s more to the first frame.
   * Result: the SDK answers DeviceDisconnected. The `if (!isStreaming) return` guard in Meta's
   * sample turned out to be a real SDK requirement. Kept for reference.
   *
   * The camera is stopped in finally, so the LED is on only during the shot.
   * Returns a Bitmap or null (the caller then falls back to a video-stream frame).
   */
  suspend fun captureStillNoStream(stateWaitMs: Long = 3000): Bitmap? {
    val current = session ?: run { Log.w(TAG, "DIAG still: no active session"); return null }
    if (camera != null || cameraStarting) {
      Log.w(TAG, "DIAG still: camera busy (camera=${camera != null} starting=$cameraStarting)")
      return null
    }
    val t0 = android.os.SystemClock.elapsedRealtime()
    fun el() = android.os.SystemClock.elapsedRealtime() - t0

    cameraStarting = true
    cameraCancelled = false
    var added: com.meta.wearable.dat.camera.Camera? = null
    current
        .addCamera(StreamConfiguration(videoQuality = VideoQuality.MEDIUM, frameRate = 15))
        .onSuccess { added = it }
        .onFailure { error, _ ->
          Log.e(TAG, "DIAG still: addCamera FAILED (+${el()}ms): $error / ${error.description}")
        }
    cameraStarting = false
    val cam = added ?: return null
    camera = cam
    Log.i(TAG, "DIAG still: addCamera OK (+${el()}ms), NOT calling stream.start()")

    try {
      // wait for the CAMERA (CameraState), not the video stream (StreamState)
      val deadline = android.os.SystemClock.elapsedRealtime() + stateWaitMs
      while (android.os.SystemClock.elapsedRealtime() < deadline &&
          cam.state.value != CameraState.STARTED) {
        delay(50)
      }
      Log.i(TAG, "DIAG still: cameraState=${cam.state.value} (+${el()}ms) → capturePhoto")

      var bmp: Bitmap? = null
      cam.stream
          .capturePhoto()
          .onSuccess { photo ->
            bmp = decodePhotoToBitmap(photo)
            Log.i(TAG, "DIAG still: capturePhoto OK (+${el()}ms) bitmap=${bmp != null} " +
                "${bmp?.width}x${bmp?.height}")
          }
          .onFailure { error, _ ->
            Log.e(TAG, "DIAG still: capturePhoto FAILED (+${el()}ms): $error / ${error.description}")
          }
      return bmp
    } finally {
      cam.stop()          // the LED goes off right away: the camera lives only for the shot
      camera = null
      stream = null
      _uiState.update { it.copy(streamState = StreamState.CLOSED, videoFrame = null) }
      Log.i(TAG, "DIAG still: camera off (+${el()}ms)")
    }
  }

  /**
   * EXPERIMENT (unused): a photo right after the stream starts.
   *
   * capturePhoto() needs a running stream (without it the SDK answers DeviceDisconnected)
   * but doesn't need the first video frame. The timings:
   *     STREAMING       ~1.7 s
   *     first frame     ~6.1 s
   * So the idea was to wait only for STREAMING and shoot, getting a full-size photo
   * instead of the 504x896 preview frame. In practice capturePhoto hung ~10 s and failed
   * with CaptureFailed, so the agent uses a fresh video frame instead. Kept for reference.
   *
   * The camera is stopped in finally: the LED is on only while shooting.
   * null → the caller falls back to a video-stream frame.
   */
  suspend fun captureStillAfterStart(streamWaitMs: Long = 8000): Bitmap? {
    val current = session ?: run { Log.w(TAG, "DIAG shot: no active session"); return null }
    if (camera != null || cameraStarting) {
      Log.w(TAG, "DIAG shot: camera busy (camera=${camera != null} starting=$cameraStarting)")
      return null
    }
    val t0 = android.os.SystemClock.elapsedRealtime()
    fun el() = android.os.SystemClock.elapsedRealtime() - t0

    cameraStarting = true
    cameraCancelled = false
    var added: com.meta.wearable.dat.camera.Camera? = null
    current
        .addCamera(StreamConfiguration(videoQuality = VideoQuality.MEDIUM, frameRate = 15))
        .onSuccess { added = it }
        .onFailure { error, _ ->
          Log.e(TAG, "DIAG shot: addCamera FAILED (+${el()}ms): $error / ${error.description}")
        }
    cameraStarting = false
    val cam = added ?: return null
    camera = cam
    stream = cam.stream
    Log.i(TAG, "DIAG shot: addCamera OK (+${el()}ms) → stream.start()")

    try {
      var startFailed = false
      cam.stream.start().onFailure { error, _ ->
        startFailed = true
        Log.e(TAG, "DIAG shot: stream.start FAILED (+${el()}ms): $error / ${error.description}")
      }
      if (startFailed) return null

      // wait ONLY for STREAMING: the first video frame isn't needed
      val deadline = android.os.SystemClock.elapsedRealtime() + streamWaitMs
      while (android.os.SystemClock.elapsedRealtime() < deadline &&
          cam.stream.state.value != StreamState.STREAMING) {
        delay(50)
      }
      Log.i(TAG, "DIAG shot: streamState=${cam.stream.state.value} (+${el()}ms) → capturePhoto")

      var bmp: Bitmap? = null
      cam.stream
          .capturePhoto()
          .onSuccess { photo ->
            bmp = decodePhotoToBitmap(photo)
            Log.i(TAG, "DIAG shot: capturePhoto OK (+${el()}ms) " +
                "bitmap=${bmp != null} ${bmp?.width}x${bmp?.height}")
          }
          .onFailure { error, _ ->
            Log.e(TAG, "DIAG shot: capturePhoto FAILED (+${el()}ms): $error / ${error.description}")
          }
      return bmp
    } finally {
      cam.stop()
      camera = null
      stream = null
      _uiState.update { it.copy(streamState = StreamState.CLOSED, videoFrame = null) }
      Log.i(TAG, "DIAG shot: camera off (+${el()}ms)")
    }
  }

  fun capturePhoto() {
    if (uiState.value.isCapturing) {
      Log.d(TAG, "Photo capture already in progress, ignoring request")
      return
    }

    if (uiState.value.streamState == StreamState.STREAMING) {
      Log.d(TAG, "Starting photo capture")
      _uiState.update { it.copy(isCapturing = true) }

      viewModelScope.launch {
        stream
            ?.capturePhoto()
            ?.onSuccess { photoData ->
              Log.d(TAG, "Photo capture successful")
              handlePhotoData(photoData)
              _uiState.update { it.copy(isCapturing = false) }
            }
            ?.onFailure { error, _ ->
              Log.e(TAG, "Photo capture failed: ${error.description}")
              _uiState.update { it.copy(isCapturing = false) }
            }
      }
    } else {
      Log.w(
          TAG,
          "Cannot capture photo: stream not active (state=${uiState.value.streamState})",
      )
    }
  }

  fun showShareDialog() {
    _uiState.update { it.copy(isShareDialogVisible = true) }
  }

  fun hideShareDialog() {
    _uiState.update { it.copy(isShareDialogVisible = false) }
  }

  fun sharePhoto(bitmap: Bitmap) {
    val context = getApplication<Application>()
    val imagesFolder = File(context.cacheDir, "images")
    try {
      imagesFolder.mkdirs()
      val file = File(imagesFolder, "shared_image.png")
      FileOutputStream(file).use { stream ->
        bitmap.compress(Bitmap.CompressFormat.PNG, 90, stream)
      }

      val uri = FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", file)
      val intent = Intent(Intent.ACTION_SEND)
      intent.flags = Intent.FLAG_ACTIVITY_NEW_TASK
      intent.putExtra(Intent.EXTRA_STREAM, uri)
      intent.type = "image/png"
      intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)

      val chooser = Intent.createChooser(intent, "Share Image")
      chooser.flags = Intent.FLAG_ACTIVITY_NEW_TASK
      context.startActivity(chooser)
    } catch (e: IOException) {
      Log.e("StreamViewModel", "Failed to share photo", e)
    }
  }

  private fun handleVideoFrame(videoFrame: VideoFrame) {
    // Keep ONLY the latest frame (no PresentationQueue, no on-screen rendering): minimal
    // load. The agent takes a fresh frame from here on look (capturePhoto on the glasses
    // is unreliable: "Failed to capture", kills the session, loud shutter).
    val bitmap =
        YuvToBitmapConverter.convert(videoFrame.buffer, videoFrame.width, videoFrame.height)
    // DIAG: the first frames after the camera turns on: do they arrive at all, and when
    if (++diagFramesSinceOn <= 3) {
      Log.i(TAG, "DIAG frame #$diagFramesSinceOn (+${elapsedSinceCameraOn()}ms) " +
          "${videoFrame.width}x${videoFrame.height} codecConfig=${videoFrame.isCodecConfig} " +
          "bitmap=${bitmap != null}")
    }
    if (bitmap != null) {
      _uiState.update { it.copy(videoFrame = bitmap, videoFrameCount = it.videoFrameCount + 1) }
    }
  }

  /** PhotoData → Bitmap (a Bitmap is returned as is; HEIC is decoded honoring EXIF rotation). */
  private fun decodePhotoToBitmap(photo: PhotoData): Bitmap? =
      runCatching {
        when (photo) {
          is PhotoData.Bitmap -> photo.bitmap
          is PhotoData.HEIC -> {
            val byteArray = ByteArray(photo.data.remaining())
            photo.data.get(byteArray)
            // Extract EXIF transformation matrix and apply to bitmap
            val exifInfo = getExifInfo(byteArray)
            val transform = getTransform(exifInfo)
            decodeHeic(byteArray, transform)
          }
        }
      }.getOrNull()

  private fun handlePhotoData(photo: PhotoData) {
    _uiState.update {
      it.copy(capturedPhoto = decodePhotoToBitmap(photo), isShareDialogVisible = true)
    }
  }

  // HEIC Decoding with EXIF transformation
  private fun decodeHeic(heicBytes: ByteArray, transform: Matrix): Bitmap {
    val bitmap = BitmapFactory.decodeByteArray(heicBytes, 0, heicBytes.size)
    return applyTransform(bitmap, transform)
  }

  private fun getExifInfo(heicBytes: ByteArray): ExifInterface? {
    return try {
      ByteArrayInputStream(heicBytes).use { inputStream -> ExifInterface(inputStream) }
    } catch (e: IOException) {
      Log.w(TAG, "Failed to read EXIF from HEIC", e)
      null
    }
  }

  private fun getTransform(exifInfo: ExifInterface?): Matrix {
    val matrix = Matrix()

    if (exifInfo == null) {
      return matrix // Identity matrix (no transformation)
    }

    when (
        exifInfo.getAttributeInt(
            ExifInterface.TAG_ORIENTATION,
            ExifInterface.ORIENTATION_NORMAL,
        )
    ) {
      ExifInterface.ORIENTATION_FLIP_HORIZONTAL -> {
        matrix.postScale(-1f, 1f)
      }
      ExifInterface.ORIENTATION_ROTATE_180 -> {
        matrix.postRotate(180f)
      }
      ExifInterface.ORIENTATION_FLIP_VERTICAL -> {
        matrix.postScale(1f, -1f)
      }
      ExifInterface.ORIENTATION_TRANSPOSE -> {
        matrix.postRotate(90f)
        matrix.postScale(-1f, 1f)
      }
      ExifInterface.ORIENTATION_ROTATE_90 -> {
        matrix.postRotate(90f)
      }
      ExifInterface.ORIENTATION_TRANSVERSE -> {
        matrix.postRotate(270f)
        matrix.postScale(-1f, 1f)
      }
      ExifInterface.ORIENTATION_ROTATE_270 -> {
        matrix.postRotate(270f)
      }
      ExifInterface.ORIENTATION_NORMAL,
      ExifInterface.ORIENTATION_UNDEFINED -> {
        // No transformation needed
      }
    }

    return matrix
  }

  private fun applyTransform(bitmap: Bitmap, matrix: Matrix): Bitmap {
    if (matrix.isIdentity) {
      return bitmap
    }

    return try {
      val transformed = Bitmap.createBitmap(bitmap, 0, 0, bitmap.width, bitmap.height, matrix, true)
      if (transformed != bitmap) {
        bitmap.recycle()
      }
      transformed
    } catch (e: OutOfMemoryError) {
      Log.e(TAG, "Failed to apply transformation due to memory", e)
      bitmap
    }
  }

  override fun onCleared() {
    super.onCleared()
    stopStream()
    session?.stop()
    session = null
  }

  class Factory(
      private val application: Application,
      private val wearablesViewModel: WearablesViewModel,
  ) : ViewModelProvider.Factory {
    override fun <T : ViewModel> create(modelClass: Class<T>): T {
      if (modelClass.isAssignableFrom(StreamViewModel::class.java)) {
        @Suppress("UNCHECKED_CAST", "KotlinGenericsCast")
        return StreamViewModel(
            application = application,
            wearablesViewModel = wearablesViewModel,
        )
            as T
      }
      throw IllegalArgumentException("Unknown ViewModel class")
    }
  }
}
