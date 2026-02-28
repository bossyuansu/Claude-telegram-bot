package com.claudebot.app.util

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.unit.sp
import com.claudebot.app.ui.theme.AccentOrange
import com.claudebot.app.ui.theme.InlineCodeBg

sealed class MessageSegment {
    data class Text(val annotated: AnnotatedString) : MessageSegment()
    data class CodeBlock(val code: String, val language: String = "") : MessageSegment()
}

fun parseMarkdown(text: String): List<MessageSegment> {
    val segments = mutableListOf<MessageSegment>()
    val parts = text.split("```")

    for (i in parts.indices) {
        val part = parts[i]
        if (i % 2 == 1) {
            // Inside code block
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
        val lines = text.split("\n")
        for ((lineIdx, line) in lines.withIndex()) {
            if (lineIdx > 0) append("\n")
            parseLine(line)
        }
    }
}

private val LINK_REGEX = Regex("""\[([^\]]+)]\(([^)]+)\)""")
private val HEADER_REGEX = Regex("""^(#{1,3})\s+(.+)$""")
private val BULLET_REGEX = Regex("""^(\s*)[*\-+]\s+(.+)$""")
private val CHECKBOX_UNCHECKED = Regex("""^(\s*)[*\-+]\s+\[ ]\s+(.+)$""")
private val CHECKBOX_CHECKED = Regex("""^(\s*)[*\-+]\s+\[x]\s+(.+)$""", RegexOption.IGNORE_CASE)

private fun AnnotatedString.Builder.parseLine(line: String) {
    // Headers: # ## ###
    val headerMatch = HEADER_REGEX.matchEntire(line)
    if (headerMatch != null) {
        val level = headerMatch.groupValues[1].length
        val content = headerMatch.groupValues[2]
        val size = when (level) { 1 -> 18.sp; 2 -> 16.sp; else -> 15.sp }
        pushStyle(SpanStyle(fontWeight = FontWeight.Bold, fontSize = size))
        parseInlineFormatting(content)
        pop()
        return
    }

    // Checkbox items: - [x] done / - [ ] pending
    val checkedMatch = CHECKBOX_CHECKED.matchEntire(line)
    if (checkedMatch != null) {
        val indent = checkedMatch.groupValues[1]
        append(indent)
        append("\u2611 ") // checked box
        pushStyle(SpanStyle(textDecoration = TextDecoration.LineThrough, color = Color(0xFF7C7C96)))
        parseInlineFormatting(checkedMatch.groupValues[2])
        pop()
        return
    }
    val uncheckedMatch = CHECKBOX_UNCHECKED.matchEntire(line)
    if (uncheckedMatch != null) {
        val indent = uncheckedMatch.groupValues[1]
        append(indent)
        append("\u2610 ") // unchecked box
        parseInlineFormatting(uncheckedMatch.groupValues[2])
        return
    }

    // Bullet lists: - * +
    val bulletMatch = BULLET_REGEX.matchEntire(line)
    if (bulletMatch != null) {
        val indent = bulletMatch.groupValues[1]
        append(indent)
        append("  \u2022 ")
        parseInlineFormatting(bulletMatch.groupValues[2])
        return
    }

    // Normal line with inline formatting
    parseInlineFormatting(line)
}

private fun AnnotatedString.Builder.parseInlineFormatting(text: String) {
    // First, find all link positions to avoid formatting inside links
    val linkMatches = LINK_REGEX.findAll(text).toList()
    var cursor = 0

    for (match in linkMatches) {
        // Process text before this link
        if (match.range.first > cursor) {
            parseBasicFormatting(text.substring(cursor, match.range.first))
        }
        // Render link
        val linkText = match.groupValues[1]
        val url = match.groupValues[2]
        pushStyle(SpanStyle(color = AccentOrange, textDecoration = TextDecoration.Underline))
        pushStringAnnotation("URL", url)
        append(linkText)
        pop() // annotation
        pop() // style
        cursor = match.range.last + 1
    }

    // Remaining text after last link
    if (cursor < text.length) {
        parseBasicFormatting(text.substring(cursor))
    }
}

private fun AnnotatedString.Builder.parseBasicFormatting(text: String) {
    var i = 0
    while (i < text.length) {
        when {
            // Strikethrough: ~text~
            text[i] == '~' && i + 1 < text.length && text[i + 1] != ' ' -> {
                val end = text.indexOf('~', i + 1)
                if (end > i && text[end - 1] != ' ') {
                    pushStyle(SpanStyle(textDecoration = TextDecoration.LineThrough))
                    append(text.substring(i + 1, end))
                    pop()
                    i = end + 1
                } else {
                    append(text[i])
                    i++
                }
            }
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
            // Bold+italic: ***text*** or ___text___
            text[i] == '*' && i + 2 < text.length && text[i + 1] == '*' && text[i + 2] == '*' -> {
                val end = text.indexOf("***", i + 3)
                if (end > i) {
                    pushStyle(SpanStyle(fontWeight = FontWeight.Bold, fontStyle = FontStyle.Italic))
                    append(text.substring(i + 3, end))
                    pop()
                    i = end + 3
                } else {
                    append(text[i])
                    i++
                }
            }
            // Bold: **text**
            text[i] == '*' && i + 1 < text.length && text[i + 1] == '*' -> {
                val end = text.indexOf("**", i + 2)
                if (end > i) {
                    pushStyle(SpanStyle(fontWeight = FontWeight.Bold))
                    append(text.substring(i + 2, end))
                    pop()
                    i = end + 2
                } else {
                    append(text[i])
                    i++
                }
            }
            // Bold (single *): *text*
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
            // Italic: _text_
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
