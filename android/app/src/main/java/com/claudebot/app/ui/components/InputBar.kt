package com.claudebot.app.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.claudebot.app.ui.theme.*

private data class BotCommand(val cmd: String, val desc: String, val needsArgs: Boolean)

private val COMMANDS = listOf(
    BotCommand("/claude", "Run Claude task", true),
    BotCommand("/codex", "Run Codex task", true),
    BotCommand("/gemini", "Run Gemini task", true),
    BotCommand("/justdoit", "Autonomous implementation", true),
    BotCommand("/omni", "Multi-agent engineering", true),
    BotCommand("/deepreview", "Multi-phase code review", false),
    BotCommand("/new", "New session in ~/project", true),
    BotCommand("/resume", "Pick a session to resume", false),
    BotCommand("/sessions", "List all sessions", false),
    BotCommand("/switch", "Switch session by name", true),
    BotCommand("/status", "Show current status", false),
    BotCommand("/cancel", "Cancel current task", false),
    BotCommand("/plan", "Enter plan mode", false),
    BotCommand("/approve", "Approve current plan", false),
    BotCommand("/reject", "Reject current plan", false),
    BotCommand("/reset", "Clear conversation history", false),
    BotCommand("/end", "End current session", false),
    BotCommand("/delete", "Delete a session", true),
    BotCommand("/help", "Show all commands", false),
)

@Composable
fun InputBar(
    onSend: (String) -> Unit,
    enabled: Boolean = true,
    currentSession: String = ""
) {
    var text by remember { mutableStateOf("") }
    var showMenu by remember { mutableStateOf(false) }
    val keyboard = LocalSoftwareKeyboardController.current
    val haptic = LocalHapticFeedback.current

    // Filter commands when user is typing a slash command
    val typedCmd = text.trim()
    val suggestions = if (typedCmd.startsWith("/") && !typedCmd.contains(" ")) {
        COMMANDS.filter { it.cmd.startsWith(typedCmd, ignoreCase = true) && it.cmd != typedCmd }
    } else {
        emptyList()
    }

    Column {
        // Command autocomplete suggestions (shown above input)
        if (suggestions.isNotEmpty()) {
            Surface(
                color = DarkSurface,
                shadowElevation = 4.dp,
            ) {
                LazyColumn(
                    modifier = Modifier
                        .fillMaxWidth()
                        .heightIn(max = 200.dp)
                ) {
                    items(suggestions) { cmd ->
                        Row(
                            modifier = Modifier
                                .fillMaxWidth()
                                .clickable {
                                    if (cmd.needsArgs) {
                                        text = "${cmd.cmd} "
                                    } else {
                                        text = ""
                                        onSend(cmd.cmd)
                                        keyboard?.hide()
                                    }
                                }
                                .padding(horizontal = 16.dp, vertical = 10.dp),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Text(cmd.cmd, color = AccentOrange, fontSize = 14.sp)
                            Spacer(Modifier.width(12.dp))
                            Text(cmd.desc, color = SessionLabel, fontSize = 12.sp)
                        }
                    }
                }
            }
        }

        // Command menu dropdown (from / button)
        if (showMenu) {
            Surface(
                color = DarkSurface,
                shadowElevation = 4.dp,
            ) {
                LazyColumn(
                    modifier = Modifier
                        .fillMaxWidth()
                        .heightIn(max = 300.dp)
                ) {
                    items(COMMANDS) { cmd ->
                        Row(
                            modifier = Modifier
                                .fillMaxWidth()
                                .clickable {
                                    showMenu = false
                                    if (cmd.needsArgs) {
                                        text = "${cmd.cmd} "
                                    } else {
                                        text = ""
                                        onSend(cmd.cmd)
                                        keyboard?.hide()
                                    }
                                }
                                .padding(horizontal = 16.dp, vertical = 10.dp),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Text(cmd.cmd, color = AccentOrange, fontSize = 14.sp)
                            Spacer(Modifier.width(12.dp))
                            Text(cmd.desc, color = SessionLabel, fontSize = 12.sp)
                        }
                    }
                }
            }
        }

        // Input row
        Surface(
            color = InputBg,
            shadowElevation = 8.dp
        ) {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 8.dp, vertical = 8.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                // Command menu button
                FilledIconButton(
                    onClick = { showMenu = !showMenu },
                    enabled = enabled,
                    modifier = Modifier.size(40.dp),
                    colors = IconButtonDefaults.filledIconButtonColors(
                        containerColor = if (showMenu) AccentOrange else DarkSurfaceVariant,
                        contentColor = if (showMenu) UserBubbleText else AccentOrange,
                        disabledContainerColor = DarkSurfaceVariant,
                        disabledContentColor = PlaceholderText,
                    )
                ) {
                    Text("/", style = MaterialTheme.typography.titleMedium)
                }

                Spacer(Modifier.width(4.dp))

                OutlinedTextField(
                    value = text,
                    onValueChange = {
                        text = it
                        if (it.startsWith("/")) showMenu = false
                    },
                    modifier = Modifier.weight(1f),
                    placeholder = {
                        val hint = if (currentSession.isNotEmpty()) currentSession else "Message or /command..."
                        Text(hint, color = PlaceholderText, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    },
                    shape = RoundedCornerShape(24.dp),
                    maxLines = 4,
                    enabled = enabled,
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedBorderColor = InputBorderFocused,
                        unfocusedBorderColor = InputBorder,
                        focusedTextColor = BotText,
                        unfocusedTextColor = BotText,
                        cursorColor = AccentOrange,
                        focusedContainerColor = DarkSurfaceVariant,
                        unfocusedContainerColor = DarkSurfaceVariant,
                    )
                )

                Spacer(Modifier.width(8.dp))

                FilledIconButton(
                    onClick = {
                        if (text.isNotBlank()) {
                            haptic.performHapticFeedback(HapticFeedbackType.LongPress)
                            onSend(text.trim())
                            text = ""
                            keyboard?.hide()
                        }
                    },
                    enabled = enabled && text.isNotBlank(),
                    colors = IconButtonDefaults.filledIconButtonColors(
                        containerColor = AccentOrange,
                        contentColor = UserBubbleText,
                        disabledContainerColor = DarkSurfaceVariant,
                        disabledContentColor = PlaceholderText,
                    )
                ) {
                    Text("\u2191", style = MaterialTheme.typography.titleMedium)
                }
            }
        }
    }
}
