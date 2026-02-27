package com.claudebot.app.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.claudebot.app.data.ChatMessage
import com.claudebot.app.ui.theme.*
import com.claudebot.app.util.MessageSegment
import com.claudebot.app.util.parseMarkdown
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

@Composable
fun MessageBubble(message: ChatMessage) {
    val isBot = message.isFromBot
    val alignment = if (isBot) Alignment.Start else Alignment.End
    val bubbleColor = if (isBot) BotBubble else UserBubble
    val textColor = if (isBot) Color.Black else UserBubbleText
    val shape = RoundedCornerShape(
        topStart = 12.dp, topEnd = 12.dp,
        bottomStart = if (isBot) 2.dp else 12.dp,
        bottomEnd = if (isBot) 12.dp else 2.dp
    )

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 2.dp),
        horizontalAlignment = alignment
    ) {
        // Session label (subtle, above bot messages only)
        if (isBot && message.session.isNotEmpty()) {
            Text(
                text = message.session,
                fontSize = 10.sp,
                color = SessionLabel,
                modifier = Modifier.padding(start = 4.dp, bottom = 1.dp)
            )
        }

        Box(
            modifier = Modifier
                .widthIn(max = 320.dp)
                .clip(shape)
                .background(bubbleColor)
                .padding(horizontal = 10.dp, vertical = 6.dp)
        ) {
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
                                Spacer(Modifier.height(4.dp))
                                Box(
                                    modifier = Modifier
                                        .fillMaxWidth()
                                        .clip(RoundedCornerShape(6.dp))
                                        .background(CodeBlockBg)
                                        .horizontalScroll(rememberScrollState())
                                        .padding(8.dp)
                                ) {
                                    Text(
                                        text = segment.code,
                                        color = CodeBlockText,
                                        fontSize = 12.sp,
                                        fontFamily = FontFamily.Monospace,
                                        lineHeight = 16.sp
                                    )
                                }
                                Spacer(Modifier.height(4.dp))
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

        // Timestamp
        Text(
            text = SimpleDateFormat("HH:mm", Locale.getDefault()).format(Date(message.timestamp)),
            fontSize = 10.sp,
            color = SessionLabel,
            modifier = Modifier.padding(horizontal = 4.dp, vertical = 1.dp)
        )
    }
}
