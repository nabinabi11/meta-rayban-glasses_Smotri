/*
 * AgentScreen — the app UI.
 * Server address → connect the glasses → start → working screen: status, the agent's
 * activity feed, chat (you / agent) with the frames it looked at, voice picker, talk button.
 */
package com.meta.wearable.dat.externalsampleapps.cameraaccess.agent

import android.app.Application
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Slider
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.meta.wearable.dat.core.types.Permission
import com.meta.wearable.dat.core.types.PermissionStatus
import com.meta.wearable.dat.externalsampleapps.cameraaccess.stream.StreamViewModel
import com.meta.wearable.dat.externalsampleapps.cameraaccess.wearables.WearablesViewModel

private val MALE_VOICES = listOf("Kai", "Vincent", "Neil", "Dylan", "Moon", "Arthur", "Ethan")

@Composable
fun AgentScreen(
    wearablesVM: WearablesViewModel,
    agentVM: AgentViewModel,
    onConnectGlasses: () -> Unit,
    onRequestWearablesPermission: suspend (Permission) -> PermissionStatus,
) {
    val w by wearablesVM.uiState.collectAsStateWithLifecycle()
    val ctx = LocalContext.current
    var server by remember { mutableStateOf(ServerPrefs.get(ctx)) }
    var editingServer by remember { mutableStateOf(false) }

    when {
        // step 0: where to connect (the line from the server log). Editable before the agent starts.
        server.isEmpty() || editingServer -> ServerSetupScreen(initial = server) { url ->
            ServerPrefs.set(ctx, url)
            server = ServerPrefs.get(ctx)
            editingServer = false
        }
        !w.isRegistered -> CenterScreen {
            Text("Smotri — AI agent for your glasses", style = MaterialTheme.typography.headlineSmall)
            Text("glasses: ${w.registrationState}")
            Button(onClick = onConnectGlasses, enabled = w.canStartRegistration) {
                Text(if (w.isRegistering) "connecting…" else "Connect glasses")
            }
            ServerLink(server) { editingServer = true }
        }
        !w.isStreaming -> CenterScreen {
            Text("Glasses connected ✓")
            Button(onClick = { wearablesVM.navigateToStreaming(onRequestWearablesPermission) }) {
                Text("Start agent")
            }
            ServerLink(server) { editingServer = true }
        }
        else -> {
            val app = LocalContext.current.applicationContext as Application
            val streamVM: StreamViewModel = viewModel(factory = StreamViewModel.Factory(app, wearablesVM))
            LaunchedEffect(Unit) {
                streamVM.startStream()
                agentVM.start(streamVM)
            }
            WorkingScreen(agentVM)
        }
    }
}

@Composable
private fun WorkingScreen(agentVM: AgentViewModel) {
    val a by agentVM.ui.collectAsStateWithLifecycle()

    // NOTE: no live video background: rendering 24 fps overloaded the phone.
    // Frames are taken only on demand (look) and shown INLINE in the feed. Dark background.
    Box(Modifier.fillMaxSize().background(Color(0xFF101418))) {

        Column(Modifier.fillMaxSize().padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text(if (a.warmingUp) "⏳ starting…" else statusLine(a.state),
                 color = Color.White, style = MaterialTheme.typography.titleMedium)
            a.lastError?.let { Text("error: $it", color = Color(0xFFFF8A80), style = MaterialTheme.typography.bodySmall) }

            // the agent's activity feed
            if (a.tools.isNotEmpty()) {
                Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    a.tools.forEach { AssistChip(onClick = {}, label = { Text(it) }) }
                }
            }

            // chat: you / agent
            Column(Modifier.weight(1f).fillMaxWidth().verticalScroll(rememberScrollState()),
                   verticalArrangement = Arrangement.spacedBy(8.dp)) {
                // previous exchanges (utterance → frame if it looked → answer)
                a.history.forEach { ex ->
                    if (ex.user.isNotBlank()) Bubble("You", ex.user, Alignment.End, Color(0x553F51B5))
                    ex.frame?.let { FrameThumb(it) }
                    if (ex.agent.isNotBlank()) Bubble("Agent", ex.agent, Alignment.Start, Color(0x55000000))
                    ex.durationSec?.let { TimeLabel(it) }
                }
                // current exchange
                if (a.userText.isNotBlank()) Bubble("You", a.userText, Alignment.End, Color(0x553F51B5))
                a.lastFrame?.let { FrameThumb(it) }
                if (a.answer.isNotBlank()) Bubble("Agent", a.answer, Alignment.Start, Color(0x55000000))
                a.lastTurnSec?.let { TimeLabel(it) }
            }

            // player (radio / track) when something is playing
            a.media?.let { m ->
                MediaCard(m, onToggle = { agentVM.mediaToggle() },
                          onSeek = { agentVM.mediaSeekTo(it) }, onStop = { agentVM.mediaStop() })
            }

            // voice picker (live)
            Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                MALE_VOICES.forEach { v ->
                    FilterChip(selected = a.voice == v, onClick = { agentVM.setVoice(v) }, label = { Text(v) })
                }
            }

            val talking = a.state == AgentState.LISTENING
            Button(
                onClick = { if (talking) agentVM.stopTalk() else agentVM.startTalk() },
                enabled = !a.warmingUp,
                modifier = Modifier.fillMaxWidth(),
            ) { Text(if (a.warmingUp) "starting…" else buttonLabel(a.state)) }
        }
    }
}

@Composable
private fun MediaCard(m: MediaUi, onToggle: () -> Unit, onSeek: (Long) -> Unit, onStop: () -> Unit) {
    Surface(color = Color(0x552962FF), shape = RoundedCornerShape(12.dp), modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text("🎵 ${m.title}", color = Color.White, maxLines = 1,
                 style = MaterialTheme.typography.titleSmall)
            if (m.durationMs > 0) {
                // track / podcast: seekable
                var dragPos by remember { mutableStateOf<Float?>(null) }
                val pos = dragPos ?: m.positionMs.toFloat()
                Slider(
                    value = pos.coerceIn(0f, m.durationMs.toFloat()),
                    valueRange = 0f..m.durationMs.toFloat(),
                    onValueChange = { dragPos = it },
                    onValueChangeFinished = { dragPos?.let { onSeek(it.toLong()) }; dragPos = null },
                )
                Text("${fmtMs(pos.toLong())} / ${fmtMs(m.durationMs)}",
                     color = Color(0xFFB0BEC5), style = MaterialTheme.typography.labelSmall)
            } else {
                Text("● live", color = Color(0xFFB0BEC5), style = MaterialTheme.typography.labelSmall)
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = onToggle) { Text(if (m.playing) "Pause" else "Play") }
                Button(onClick = onStop) { Text("Stop") }
            }
        }
    }
}

private fun fmtMs(ms: Long): String {
    val s = ms / 1000; return "%d:%02d".format(s / 60, s % 60)
}

@Composable
private fun TimeLabel(sec: Double) {
    Text("⏱ %.1f s".format(sec), color = Color(0xFF78909C),
         style = MaterialTheme.typography.labelSmall)
}

@Composable
private fun FrameThumb(bmp: android.graphics.Bitmap) {
    Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.Start) {
        Text("👁 looked", color = Color(0xFFB0BEC5), style = MaterialTheme.typography.labelSmall)
        Image(
            bmp.asImageBitmap(), null,
            Modifier.size(160.dp, 120.dp).clip(RoundedCornerShape(10.dp)),
            contentScale = ContentScale.Crop,
        )
    }
}

@Composable
private fun Bubble(who: String, text: String, align: Alignment.Horizontal, bg: Color) {
    Column(Modifier.fillMaxWidth(), horizontalAlignment = align) {
        Text(who, color = Color(0xFFB0BEC5), style = MaterialTheme.typography.labelSmall)
        Surface(color = bg, shape = RoundedCornerShape(12.dp)) {
            Text(text, color = Color.White, modifier = Modifier.padding(10.dp),
                 style = MaterialTheme.typography.bodyLarge)
        }
    }
}

/** Paste the connection line the server prints on start. */
@Composable
private fun ServerSetupScreen(initial: String, onSave: (String) -> Unit) {
    var text by remember { mutableStateOf(initial) }
    var error by remember { mutableStateOf<String?>(null) }
    CenterScreen {
        Text("Smotri server", style = MaterialTheme.typography.headlineSmall)
        Text("Paste the line your server printed on start:\nws://<ip>:8000/ws?token=…")
        OutlinedTextField(
            value = text,
            onValueChange = { text = it; error = null },
            label = { Text("Server address") },
            isError = error != null,
            supportingText = { error?.let { Text(it) } },
            modifier = Modifier.fillMaxWidth(),
        )
        Button(onClick = {
            error = ServerPrefs.validate(text)
            if (error == null) onSave(text)
        }) { Text("Save") }
    }
}

@Composable
private fun ServerLink(url: String, onEdit: () -> Unit) {
    // show only the host: never display the token
    val host = url.substringAfter("://").substringBefore("/").substringBefore("?")
    TextButton(onClick = onEdit) { Text("server: $host  ✎") }
}

@Composable
private fun CenterScreen(content: @Composable () -> Unit) {
    Column(
        Modifier.fillMaxSize().padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(16.dp, Alignment.CenterVertically),
    ) { content() }
}

private fun statusLine(s: AgentState) = when (s) {
    AgentState.DISCONNECTED -> "● no connection…"
    AgentState.IDLE -> "say ${Config.WAKE_HINT}…"
    AgentState.LISTENING -> "● listening…"
    AgentState.THINKING -> "… thinking"
    AgentState.SPEAKING -> "🔊 answering"
    AgentState.FOLLOWUP -> "● still listening, go on"
}

private fun buttonLabel(s: AgentState) = when (s) {
    AgentState.LISTENING -> "Listening… (stop)"
    AgentState.THINKING -> "thinking…"
    AgentState.SPEAKING -> "interrupt"
    AgentState.FOLLOWUP -> "still listening, just talk"
    else -> "Talk (or say ${Config.WAKE_HINT})"
}
