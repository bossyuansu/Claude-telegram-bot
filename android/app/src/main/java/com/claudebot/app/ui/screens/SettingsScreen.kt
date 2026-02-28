package com.claudebot.app.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import com.claudebot.app.ChatViewModel
import com.claudebot.app.ui.theme.*

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(viewModel: ChatViewModel) {
    val settings = viewModel.settings

    var host by remember { mutableStateOf(settings.host) }
    var port by remember { mutableStateOf(settings.port.toString()) }
    var token by remember { mutableStateOf(settings.token) }

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
        }
    }
}
