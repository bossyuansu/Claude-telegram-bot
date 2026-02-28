package com.claudebot.app.ui.components

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
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
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.claudebot.app.data.ChatMessage
import com.claudebot.app.data.InlineButton
import com.claudebot.app.ui.theme.*
import com.claudebot.app.util.MessageSegment
import com.claudebot.app.util.parseMarkdown
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

@Composable
fun MessageBubble(
    message: ChatMessage,
    onButtonClick: ((InlineButton) -> Unit)? = null
) {
    val clipboard = LocalClipboardManager.current
    val scope = rememberCoroutineScope()
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
            Modifier.border(1.dp, BotBubbleBorder, shape)
        } else {
            Modifier
        }

        Box(
            modifier = Modifier
                .widthIn(max = 320.dp)
                .clip(shape)
                .then(borderMod)
                .background(bubbleColor)
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

        // Timestamp
        Text(
            text = SimpleDateFormat("HH:mm", Locale.getDefault()).format(Date(message.timestamp)),
            fontSize = 10.sp,
            color = TimestampColor,
            modifier = Modifier.padding(horizontal = 4.dp, vertical = 1.dp)
        )
    }
}
