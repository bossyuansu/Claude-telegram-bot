package com.claudebot.app.ui.components

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.claudebot.app.ui.theme.*
import kotlinx.coroutines.launch

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
    currentSession: String = "",
    isBusy: Boolean = false,
    onCancel: () -> Unit = {}
) {
    var text by remember { mutableStateOf("") }
    var showMenu by remember { mutableStateOf(false) }
    var showShortcuts by remember { mutableStateOf(false) }
    val keyboard = LocalSoftwareKeyboardController.current
    val haptic = LocalHapticFeedback.current
    val scope = rememberCoroutineScope()
    val sendScale = remember { Animatable(1f) }

    // Filter commands when user is typing a slash command
    val typedCmd = text.trim()
    val suggestions = if (typedCmd.startsWith("/") && !typedCmd.contains(" ")) {
        COMMANDS.filter { it.cmd.startsWith(typedCmd, ignoreCase = true) && it.cmd != typedCmd }
    } else {
        emptyList()
    }

    Column {
        // Command autocomplete suggestions (shown above input)
        AnimatedVisibility(
            visible = suggestions.isNotEmpty(),
            enter = fadeIn() + slideInVertically { it / 2 },
            exit = fadeOut() + slideOutVertically { it / 2 }
        ) {
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

        // Quick message shortcuts
        AnimatedVisibility(
            visible = showShortcuts,
            enter = fadeIn() + slideInVertically { it },
            exit = fadeOut() + slideOutVertically { it }
        ) {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .background(InputBg)
                    .horizontalScroll(rememberScrollState())
                    .padding(horizontal = 12.dp, vertical = 4.dp),
                horizontalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                listOf("commit and push", "deploy", "fix it", "run tests", "continue").forEach { shortcut ->
                    Text(
                        shortcut,
                        fontSize = 12.sp,
                        color = AccentOrange,
                        modifier = Modifier
                            .clip(RoundedCornerShape(12.dp))
                            .border(1.dp, InputBorder, RoundedCornerShape(12.dp))
                            .clickable { onSend(shortcut); showShortcuts = false }
                            .padding(horizontal = 10.dp, vertical = 4.dp)
                    )
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
                    onClick = { showMenu = !showMenu; showShortcuts = false },
                    enabled = enabled,
                    modifier = Modifier.size(36.dp),
                    colors = IconButtonDefaults.filledIconButtonColors(
                        containerColor = if (showMenu) AccentOrange else DarkSurfaceVariant,
                        contentColor = if (showMenu) UserBubbleText else AccentOrange,
                        disabledContainerColor = DarkSurfaceVariant,
                        disabledContentColor = PlaceholderText,
                    )
                ) {
                    Text("/", style = MaterialTheme.typography.titleMedium)
                }

                // Shortcuts toggle
                IconButton(
                    onClick = { showShortcuts = !showShortcuts; showMenu = false },
                    enabled = enabled,
                    modifier = Modifier.size(36.dp),
                ) {
                    Text("\u26A1", fontSize = 16.sp)
                }

                Spacer(Modifier.width(2.dp))

                // Text input with compact padding
                var focused by remember { mutableStateOf(false) }
                val borderColor = if (focused) InputBorderFocused else InputBorder
                val shape = RoundedCornerShape(24.dp)

                BasicTextField(
                    value = text,
                    onValueChange = {
                        text = it
                        if (it.startsWith("/")) showMenu = false
                    },
                    modifier = Modifier
                        .weight(1f)
                        .clip(shape)
                        .background(DarkSurfaceVariant, shape)
                        .border(1.dp, borderColor, shape)
                        .padding(horizontal = 14.dp, vertical = 10.dp)
                        .onFocusChanged { focused = it.isFocused },
                    enabled = enabled,
                    textStyle = TextStyle(color = BotText, fontSize = 14.sp),
                    cursorBrush = SolidColor(AccentOrange),
                    maxLines = 4,
                    decorationBox = { innerTextField ->
                        if (text.isEmpty()) {
                            val hint = if (currentSession.isNotEmpty()) currentSession else "Message or /command..."
                            Text(hint, color = PlaceholderText, maxLines = 1, overflow = TextOverflow.Ellipsis, fontSize = 14.sp)
                        }
                        innerTextField()
                    }
                )

                Spacer(Modifier.width(8.dp))

                if (isBusy && text.isBlank()) {
                    // Cancel button when bot is busy and no text typed
                    FilledIconButton(
                        onClick = {
                            haptic.performHapticFeedback(HapticFeedbackType.LongPress)
                            onCancel()
                        },
                        modifier = Modifier.size(40.dp),
                        colors = IconButtonDefaults.filledIconButtonColors(
                            containerColor = DisconnectedRed,
                            contentColor = UserBubbleText,
                        )
                    ) {
                        Text("\u25A0", style = MaterialTheme.typography.titleMedium)
                    }
                } else {
                    // Send button with punch animation
                    FilledIconButton(
                        onClick = {
                            if (text.isNotBlank()) {
                                haptic.performHapticFeedback(HapticFeedbackType.LongPress)
                                scope.launch {
                                    sendScale.animateTo(0.7f, tween(50))
                                    sendScale.animateTo(1.1f, tween(80))
                                    sendScale.animateTo(1f, tween(70))
                                }
                                onSend(text.trim())
                                text = ""
                                keyboard?.hide()
                            }
                        },
                        enabled = enabled && text.isNotBlank(),
                        modifier = Modifier.graphicsLayer(
                            scaleX = sendScale.value,
                            scaleY = sendScale.value
                        ),
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
}
