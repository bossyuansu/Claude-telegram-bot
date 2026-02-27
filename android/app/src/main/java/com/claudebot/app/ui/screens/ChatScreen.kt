package com.claudebot.app.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.claudebot.app.ChatViewModel
import com.claudebot.app.network.ConnectionState
import com.claudebot.app.ui.components.InputBar
import com.claudebot.app.ui.components.MessageBubble
import com.claudebot.app.ui.theme.*

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatScreen(viewModel: ChatViewModel) {
    val messages = viewModel.messages
    val connState by viewModel.connectionState
    val listState = rememberLazyListState()

    // Auto-scroll to bottom when new messages arrive or last message is edited
    val lastMsg = messages.lastOrNull()
    LaunchedEffect(messages.size, lastMsg?.text) {
        if (messages.isNotEmpty()) {
            listState.animateScrollToItem(messages.size - 1)
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        // Connection indicator dot
                        val dotColor = when (connState) {
                            ConnectionState.CONNECTED -> ConnectedGreen
                            ConnectionState.RECONNECTING, ConnectionState.CONNECTING -> ReconnectingYellow
                            ConnectionState.DISCONNECTED -> DisconnectedRed
                        }
                        Surface(
                            modifier = Modifier.size(8.dp),
                            shape = MaterialTheme.shapes.small,
                            color = dotColor
                        ) {}
                        Spacer(Modifier.width(8.dp))
                        Text("Claude Bot", style = MaterialTheme.typography.titleMedium)
                    }
                },
                actions = {
                    // Reconnect button when disconnected
                    if (connState == ConnectionState.DISCONNECTED) {
                        TextButton(onClick = { viewModel.connect() }) {
                            Text("Connect", fontSize = 12.sp)
                        }
                    }
                    IconButton(onClick = { viewModel.showSettings.value = true }) {
                        Text("\u2699", fontSize = 20.sp)
                    }
                }
            )
        },
        bottomBar = {
            InputBar(
                onSend = { viewModel.sendMessage(it) },
                enabled = connState == ConnectionState.CONNECTED
            )
        }
    ) { padding ->
        if (messages.isEmpty()) {
            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(padding),
                contentAlignment = Alignment.Center
            ) {
                val hint = when (connState) {
                    ConnectionState.CONNECTED -> "Connected. Send a message or /command."
                    ConnectionState.CONNECTING -> "Connecting..."
                    ConnectionState.RECONNECTING -> "Reconnecting..."
                    ConnectionState.DISCONNECTED -> "Disconnected. Tap Connect or check Settings."
                }
                Text(hint, color = SessionLabel, fontSize = 14.sp)
            }
        } else {
            LazyColumn(
                state = listState,
                modifier = Modifier
                    .fillMaxSize()
                    .padding(padding),
                contentPadding = PaddingValues(vertical = 8.dp)
            ) {
                items(messages, key = { "${it.messageId}_${it.timestamp}" }) { msg ->
                    MessageBubble(msg)
                }
            }
        }
    }
}
