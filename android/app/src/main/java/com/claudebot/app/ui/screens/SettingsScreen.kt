package com.claudebot.app.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import com.claudebot.app.ChatViewModel

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(viewModel: ChatViewModel) {
    val settings = viewModel.settings

    var host by remember { mutableStateOf(settings.host) }
    var port by remember { mutableStateOf(settings.port.toString()) }
    var chatId by remember { mutableStateOf(settings.chatId) }
    var token by remember { mutableStateOf(settings.token) }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Settings") },
                navigationIcon = {
                    TextButton(onClick = { viewModel.showSettings.value = false }) {
                        Text("\u2190 Back")
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
                singleLine = true
            )

            OutlinedTextField(
                value = port,
                onValueChange = { port = it.filter { c -> c.isDigit() } },
                label = { Text("Port") },
                placeholder = { Text("8642") },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number)
            )

            OutlinedTextField(
                value = chatId,
                onValueChange = { chatId = it.filter { c -> c.isDigit() } },
                label = { Text("Telegram Chat ID") },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number)
            )

            OutlinedTextField(
                value = token,
                onValueChange = { token = it },
                label = { Text("API Token (optional)") },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true
            )

            Spacer(Modifier.height(8.dp))

            Button(
                onClick = {
                    settings.host = host.trim()
                    settings.port = port.toIntOrNull() ?: 8642
                    settings.chatId = chatId.trim()
                    settings.token = token.trim()
                    viewModel.reconnect()
                    viewModel.showSettings.value = false
                },
                modifier = Modifier.fillMaxWidth(),
                enabled = host.isNotBlank() && chatId.isNotBlank()
            ) {
                Text("Save & Connect")
            }

            // Show current WS URL for reference
            if (host.isNotBlank() && chatId.isNotBlank()) {
                val p = port.toIntOrNull() ?: 8642
                Text(
                    text = "ws://$host:$p/ws/$chatId",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        }
    }
}
