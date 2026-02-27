package com.claudebot.app.util

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import com.claudebot.app.ui.theme.InlineCodeBg

sealed class MessageSegment {
    data class Text(val annotated: AnnotatedString) : MessageSegment()
    data class CodeBlock(val code: String, val language: String = "") : MessageSegment()
}

fun parseMarkdown(text: String): List<MessageSegment> {
    val segments = mutableListOf<MessageSegment>()
    // Split by ``` code blocks
    val parts = text.split("```")

    for (i in parts.indices) {
        val part = parts[i]
        if (i % 2 == 1) {
            // Inside code block — first line may be language hint
            val lines = part.split("\n", limit = 2)
            val lang = if (lines.size > 1 && lines[0].trim().matches(Regex("^[a-zA-Z0-9_+-]+$")))
                lines[0].trim() else ""
            val code = if (lang.isNotEmpty() && lines.size > 1) lines[1] else part
            segments.add(MessageSegment.CodeBlock(code.trimEnd(), lang))
        } else if (part.isNotEmpty()) {
            segments.add(MessageSegment.Text(parseInlineMarkdown(part)))
        }
    }

    return segments.ifEmpty { listOf(MessageSegment.Text(AnnotatedString(text))) }
}

private fun parseInlineMarkdown(text: String): AnnotatedString {
    return buildAnnotatedString {
        var i = 0
        while (i < text.length) {
            when {
                // Inline code: `...`
                text[i] == '`' -> {
                    val end = text.indexOf('`', i + 1)
                    if (end > i) {
                        pushStyle(SpanStyle(
                            fontFamily = FontFamily.Monospace,
                            background = InlineCodeBg
                        ))
                        append(text.substring(i + 1, end))
                        pop()
                        i = end + 1
                    } else {
                        append(text[i])
                        i++
                    }
                }
                // Bold: *...*
                text[i] == '*' && i + 1 < text.length && text[i + 1] != ' ' -> {
                    val end = text.indexOf('*', i + 1)
                    if (end > i && text[end - 1] != ' ') {
                        pushStyle(SpanStyle(fontWeight = FontWeight.Bold))
                        append(text.substring(i + 1, end))
                        pop()
                        i = end + 1
                    } else {
                        append(text[i])
                        i++
                    }
                }
                // Italic: _..._
                text[i] == '_' && i + 1 < text.length && text[i + 1] != ' ' -> {
                    val end = text.indexOf('_', i + 1)
                    if (end > i && text[end - 1] != ' ') {
                        pushStyle(SpanStyle(fontStyle = FontStyle.Italic))
                        append(text.substring(i + 1, end))
                        pop()
                        i = end + 1
                    } else {
                        append(text[i])
                        i++
                    }
                }
                else -> {
                    append(text[i])
                    i++
                }
            }
        }
    }
}
