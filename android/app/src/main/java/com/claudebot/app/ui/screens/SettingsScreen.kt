package com.claudebot.app.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import com.claudebot.app.ChatViewModel
import com.claudebot.app.network.ConnectionState
import com.claudebot.app.ui.theme.*
import kotlinx.coroutines.delay
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(viewModel: ChatViewModel) {
    val settings = viewModel.settings
    val connectionState by viewModel.connectionState
    var diagnosticsTick by remember { mutableStateOf(0L) }

    var host by remember { mutableStateOf(settings.host) }
    var port by remember { mutableStateOf(settings.port.toString()) }
    var token by remember { mutableStateOf(settings.token) }

    LaunchedEffect(Unit) {
        while (true) {
            delay(1000)
            diagnosticsTick++
        }
    }

    val fieldColors = OutlinedTextFieldDefaults.colors(
        focusedBorderColor = InputBorderFocused,
        unfocusedBorderColor = InputBorder,
        focusedTextColor = BotText,
        unfocusedTextColor = BotText,
        cursorColor = AccentOrange,
        focusedContainerColor = DarkSurfaceVariant,
        unfocusedContainerColor = DarkSurfaceVariant,
        focusedLabelColor = AccentOrangeLight,
        unfocusedLabelColor = SessionLabel,
        focusedPlaceholderColor = PlaceholderText,
        unfocusedPlaceholderColor = PlaceholderText,
    )

    Scaffold(
        containerColor = DarkBg,
        topBar = {
            TopAppBar(
                title = { Text("Settings", color = TopBarTitle) },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = TopBarBg,
                ),
                navigationIcon = {
                    TextButton(onClick = { viewModel.showSettings.value = false }) {
                        Text("\u2190 Back", color = AccentOrange)
                    }
                }
            )
        }
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(horizontal = 16.dp, vertical = 8.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            OutlinedTextField(
                value = host,
                onValueChange = { host = it },
                label = { Text("Tailscale IP") },
                placeholder = { Text("100.118.238.103") },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
                colors = fieldColors
            )

            OutlinedTextField(
                value = port,
                onValueChange = { port = it.filter { c -> c.isDigit() } },
                label = { Text("Port") },
                placeholder = { Text("8642") },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                colors = fieldColors
            )

            OutlinedTextField(
                value = token,
                onValueChange = { token = it },
                label = { Text("API Token (optional)") },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
                colors = fieldColors
            )

            Spacer(Modifier.height(8.dp))

            Button(
                onClick = {
                    settings.host = host.trim()
                    settings.port = port.toIntOrNull() ?: 8642
                    settings.token = token.trim()
                    viewModel.reconnect()
                    viewModel.showSettings.value = false
                },
                modifier = Modifier.fillMaxWidth(),
                enabled = host.isNotBlank(),
                colors = ButtonDefaults.buttonColors(
                    containerColor = AccentOrange,
                    contentColor = UserBubbleText,
                    disabledContainerColor = DarkSurfaceVariant,
                    disabledContentColor = PlaceholderText,
                )
            ) {
                Text("Save & Connect")
            }

            if (host.isNotBlank()) {
                val p = port.toIntOrNull() ?: 8642
                Text(
                    text = "ws://$host:$p/ws",
                    style = MaterialTheme.typography.bodySmall,
                    color = TimestampColor
                )
            }

            HorizontalDivider(color = BotBubbleBorder)

            val lastSync = settings.wsLastSyncAt
            val nowMs = remember(diagnosticsTick) { System.currentTimeMillis() }
            val lastSyncText = if (lastSync > 0L) {
                val ts = SimpleDateFormat("HH:mm:ss", Locale.getDefault()).format(Date(lastSync))
                val ageSec = ((nowMs - lastSync).coerceAtLeast(0L) / 1000L)
                "$ts (${ageSec}s ago)"
            } else {
                "Never"
            }
            val stateLabel = when (connectionState) {
                ConnectionState.CONNECTED -> "CONNECTED"
                ConnectionState.CONNECTING -> "CONNECTING"
                ConnectionState.RECONNECTING -> "RECONNECTING"
                ConnectionState.DISCONNECTED -> "DISCONNECTED"
            }

            Card(
                colors = CardDefaults.cardColors(containerColor = DarkSurfaceVariant),
                modifier = Modifier.fillMaxWidth()
            ) {
                Column(
                    modifier = Modifier.padding(12.dp),
                    verticalArrangement = Arrangement.spacedBy(6.dp)
                ) {
                    Text("Connection Diagnostics", color = AccentOrangeLight, style = MaterialTheme.typography.titleSmall)
                    Text("State: $stateLabel", color = BotText, style = MaterialTheme.typography.bodySmall)
                    Text("Last Sync: $lastSyncText", color = BotText, style = MaterialTheme.typography.bodySmall)
                    Text("Last Seq: ${settings.lastSeq}", color = BotText, style = MaterialTheme.typography.bodySmall)
                    Text(
                        "Server ID: ${settings.knownServerId.ifBlank { "(none)" }}",
                        color = BotText,
                        style = MaterialTheme.typography.bodySmall
                    )
                    Text(
                        "Last Error: ${settings.wsLastError.ifBlank { "(none)" }}",
                        color = if (settings.wsLastError.isBlank()) TimestampColor else AccentOrange,
                        style = MaterialTheme.typography.bodySmall
                    )
                }
            }
        }
    }
}
