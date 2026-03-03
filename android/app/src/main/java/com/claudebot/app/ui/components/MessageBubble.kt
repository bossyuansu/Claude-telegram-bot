package com.claudebot.app.ui.components

import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.animateContentSize
import androidx.compose.animation.core.tween
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.claudebot.app.data.ChatMessage
import com.claudebot.app.data.FileChange
import com.claudebot.app.data.InlineButton
import com.claudebot.app.ui.theme.*
import com.claudebot.app.util.MessageSegment
import com.claudebot.app.util.parseMarkdown
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

@OptIn(ExperimentalFoundationApi::class)
@Composable
fun MessageBubble(
    message: ChatMessage,
    onButtonClick: ((InlineButton) -> Unit)? = null
) {
    val clipboard = LocalClipboardManager.current
    val haptic = LocalHapticFeedback.current
    val scope = rememberCoroutineScope()
    var showCopied by remember { mutableStateOf(false) }
    val borderFlashColor by animateColorAsState(
        targetValue = if (showCopied) AccentOrange else BotBubbleBorder,
        animationSpec = tween(300), label = "copyFlash"
    )
    val isBot = message.isFromBot
    val alignment = if (isBot) Alignment.Start else Alignment.End
    val bubbleColor = if (isBot) BotBubble else UserBubble
    val textColor = if (isBot) BotText else UserBubbleText
    val shape = RoundedCornerShape(
        topStart = 16.dp, topEnd = 16.dp,
        bottomStart = if (isBot) 4.dp else 16.dp,
        bottomEnd = if (isBot) 16.dp else 4.dp
    )

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 3.dp),
        horizontalAlignment = alignment
    ) {
        // Session label
        if (isBot && message.session.isNotEmpty()) {
            Text(
                text = message.session,
                fontSize = 10.sp,
                color = AccentOrangeLight,
                modifier = Modifier.padding(start = 4.dp, bottom = 2.dp)
            )
        }

        val borderMod = if (isBot) {
            Modifier.border(1.dp, borderFlashColor, shape)
        } else {
            Modifier
        }

        Box(
            modifier = Modifier
                .widthIn(max = 320.dp)
                .clip(shape)
                .then(borderMod)
                .background(bubbleColor)
                .combinedClickable(
                    onClick = {},
                    onLongClick = {
                        haptic.performHapticFeedback(HapticFeedbackType.LongPress)
                        clipboard.setText(AnnotatedString(message.text))
                        showCopied = true
                        scope.launch { delay(1500); showCopied = false }
                    }
                )
                .padding(horizontal = 12.dp, vertical = 8.dp)
        ) {
            SelectionContainer {
                Column {
                    val segments = if (isBot) parseMarkdown(message.text) else emptyList()

                    if (isBot) {
                        segments.forEach { segment ->
                            when (segment) {
                                is MessageSegment.Text -> {
                                    Text(
                                        text = segment.annotated,
                                        color = textColor,
                                        fontSize = 14.sp,
                                        lineHeight = 20.sp
                                    )
                                }
                                is MessageSegment.CodeBlock -> {
                                    Spacer(Modifier.height(6.dp))
                                    Column(
                                        modifier = Modifier
                                            .fillMaxWidth()
                                            .clip(RoundedCornerShape(8.dp))
                                            .background(CodeBlockBg)
                                            .border(1.dp, BotBubbleBorder, RoundedCornerShape(8.dp))
                                    ) {
                                        // Header: language + copy
                                        Row(
                                            modifier = Modifier
                                                .fillMaxWidth()
                                                .background(BotBubbleBorder)
                                                .padding(horizontal = 10.dp, vertical = 4.dp),
                                            horizontalArrangement = Arrangement.SpaceBetween,
                                            verticalAlignment = Alignment.CenterVertically
                                        ) {
                                            Text(
                                                text = segment.language.ifEmpty { "code" },
                                                color = SessionLabel,
                                                fontSize = 10.sp,
                                                fontFamily = FontFamily.Monospace
                                            )
                                            var copied by remember { mutableStateOf(false) }
                                            Text(
                                                text = if (copied) "Copied" else "Copy",
                                                color = if (copied) ConnectedGreen else AccentOrange,
                                                fontSize = 10.sp,
                                                modifier = Modifier.clickable {
                                                    clipboard.setText(AnnotatedString(segment.code))
                                                    copied = true
                                                    scope.launch {
                                                        delay(1500)
                                                        copied = false
                                                    }
                                                }
                                            )
                                        }
                                        // Code content
                                        Box(
                                            modifier = Modifier
                                                .horizontalScroll(rememberScrollState())
                                                .padding(10.dp)
                                        ) {
                                            Text(
                                                text = segment.code,
                                                color = CodeBlockText,
                                                fontSize = 12.sp,
                                                fontFamily = FontFamily.Monospace,
                                                lineHeight = 16.sp
                                            )
                                        }
                                    }
                                    Spacer(Modifier.height(6.dp))
                                }
                            }
                        }
                    } else {
                        Text(
                            text = message.text,
                            color = textColor,
                            fontSize = 14.sp,
                            lineHeight = 20.sp
                        )
                    }
                }
            }
        }

        // File changes / diff viewer
        if (message.fileChanges.isNotEmpty()) {
            Spacer(Modifier.height(4.dp))
            FileChangesSection(message.fileChanges)
        }

        // Inline keyboard buttons
        if (message.buttons.isNotEmpty() && onButtonClick != null) {
            Spacer(Modifier.height(4.dp))
            Column(
                modifier = Modifier.widthIn(max = 320.dp),
                verticalArrangement = Arrangement.spacedBy(4.dp)
            ) {
                message.buttons.forEach { row ->
                    Row(
                        horizontalArrangement = Arrangement.spacedBy(4.dp),
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        row.forEach { button ->
                            OutlinedButton(
                                onClick = { onButtonClick(button) },
                                modifier = Modifier.weight(1f),
                                shape = RoundedCornerShape(8.dp),
                                colors = ButtonDefaults.outlinedButtonColors(
                                    contentColor = AccentOrange,
                                ),
                                border = BorderStroke(1.dp, BotBubbleBorder),
                                contentPadding = PaddingValues(horizontal = 8.dp, vertical = 6.dp)
                            ) {
                                Text(
                                    text = button.text,
                                    fontSize = 13.sp,
                                    maxLines = 1
                                )
                            }
                        }
                    }
                }
            }
        }

        // Timestamp + copied indicator
        Row(verticalAlignment = Alignment.CenterVertically) {
            if (message.isReplay) {
                Text(
                    text = "replayed",
                    fontSize = 10.sp,
                    color = AccentOrangeLight,
                    modifier = Modifier.padding(horizontal = 4.dp, vertical = 1.dp)
                )
            }
            Text(
                text = SimpleDateFormat("HH:mm", Locale.getDefault()).format(Date(message.timestamp)),
                fontSize = 10.sp,
                color = TimestampColor,
                modifier = Modifier.padding(horizontal = 4.dp, vertical = 1.dp)
            )
            if (showCopied) {
                Text(
                    text = "Copied",
                    fontSize = 10.sp,
                    color = ConnectedGreen,
                    modifier = Modifier.padding(start = 4.dp)
                )
            }
        }
    }
}

@Composable
private fun FileChangesSection(changes: List<FileChange>) {
    var expanded by remember { mutableStateOf(false) }
    Column(
        modifier = Modifier
            .widthIn(max = 320.dp)
            .clip(RoundedCornerShape(8.dp))
            .border(1.dp, BotBubbleBorder, RoundedCornerShape(8.dp))
            .background(DarkSurface)
            .animateContentSize()
    ) {
        // Header
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .clickable { expanded = !expanded }
                .padding(horizontal = 10.dp, vertical = 8.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text(
                text = "File Operations (${changes.size})",
                fontSize = 12.sp,
                color = AccentOrangeLight,
                fontFamily = FontFamily.Monospace
            )
            Text(
                text = if (expanded) "▲" else "▼",
                fontSize = 10.sp,
                color = SessionLabel
            )
        }

        if (expanded) {
            changes.forEach { change ->
                FileChangeItem(change)
            }
        }
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun FileChangeItem(change: FileChange) {
    val clipboard = LocalClipboardManager.current
    val haptic = LocalHapticFeedback.current
    val scope = rememberCoroutineScope()
    var showDiff by remember { mutableStateOf(false) }
    var showCopiedPath by remember { mutableStateOf(false) }
    val hasDiff = change.old.isNotEmpty() || change.new.isNotEmpty() || change.content.isNotEmpty()
    val icon = when (change.type) {
        "edit" -> "✏️"
        "write" -> "📝"
        "delete" -> "🗑️"
        "move" -> "📦"
        "bash" -> "⚡"
        "read" -> "📖"
        "glob" -> "🔍"
        "grep" -> "🔎"
        else -> "📄"
    }
    val parts = change.path.split("/")
    val shortPath = parts.takeLast(3).joinToString("/").ifEmpty { change.path.take(60) }
    val isTruncated = parts.size > 3
    val displayPath = if (isTruncated) ".../$shortPath" else shortPath

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .animateContentSize()
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 10.dp, vertical = 4.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text(text = icon, fontSize = 12.sp)
            Spacer(Modifier.width(6.dp))
            Text(
                text = if (showCopiedPath) "Copied!" else displayPath,
                fontSize = 11.sp,
                color = if (showCopiedPath) ConnectedGreen else BotText,
                fontFamily = FontFamily.Monospace,
                modifier = Modifier
                    .weight(1f)
                    .combinedClickable(
                        onClick = { if (hasDiff) showDiff = !showDiff },
                        onLongClick = {
                            haptic.performHapticFeedback(HapticFeedbackType.LongPress)
                            clipboard.setText(AnnotatedString(change.path))
                            showCopiedPath = true
                            scope.launch { delay(1500); showCopiedPath = false }
                        }
                    ),
                maxLines = 1
            )
            if (hasDiff) {
                Text(
                    text = if (showDiff) "▲" else "▼",
                    fontSize = 9.sp,
                    color = SessionLabel
                )
            }
        }

        if (showDiff) {
            when (change.type) {
                "edit" -> DiffView(old = change.old, new = change.new)
                "write" -> NewFileView(content = change.content)
            }
        }
    }
}

@Composable
private fun DiffView(old: String, new: String) {
    val diffText = buildAnnotatedString {
        if (old.isNotEmpty()) {
            old.lines().forEach { line ->
                withStyle(SpanStyle(color = DiffRemovedText)) {
                    append("- $line\n")
                }
            }
        }
        if (new.isNotEmpty()) {
            new.lines().forEach { line ->
                withStyle(SpanStyle(color = DiffAddedText)) {
                    append("+ $line\n")
                }
            }
        }
    }

    Box(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 6.dp, vertical = 2.dp)
            .clip(RoundedCornerShape(4.dp))
            .background(CodeBlockBg)
            .horizontalScroll(rememberScrollState())
            .padding(8.dp)
    ) {
        Column {
            Text(
                text = diffText,
                fontSize = 10.sp,
                fontFamily = FontFamily.Monospace,
                lineHeight = 14.sp
            )
            if (old.length >= 2990 || new.length >= 2990) {
                Text(
                    text = "(truncated)",
                    fontSize = 9.sp,
                    color = SessionLabel
                )
            }
        }
    }
}

@Composable
private fun NewFileView(content: String) {
    val text = buildAnnotatedString {
        content.lines().forEach { line ->
            withStyle(SpanStyle(color = DiffAddedText)) {
                append("+ $line\n")
            }
        }
    }

    Box(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 6.dp, vertical = 2.dp)
            .clip(RoundedCornerShape(4.dp))
            .background(CodeBlockBg)
            .horizontalScroll(rememberScrollState())
            .padding(8.dp)
    ) {
        Column {
            Text(
                text = text,
                fontSize = 10.sp,
                fontFamily = FontFamily.Monospace,
                lineHeight = 14.sp
            )
            if (content.length >= 2990) {
                Text(
                    text = "(truncated)",
                    fontSize = 9.sp,
                    color = SessionLabel
                )
            }
        }
    }
}
