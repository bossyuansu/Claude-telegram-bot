package com.claudebot.app.util

import androidx.compose.ui.text.AnnotatedString
import java.net.URI
import java.util.Locale

private data class ImageLink(val end: Int, val url: String, val label: String)

internal fun splitImageSegments(
    text: String,
    formatText: (String) -> AnnotatedString,
): List<MessageSegment> {
    val result = mutableListOf<MessageSegment>()
    var textStart = 0
    var index = 0
    fun appendText(end: Int) {
        val value = text.substring(textStart, end)
        if (value.isNotBlank()) result.add(MessageSegment.Text(formatText(value)))
    }
    while (index < text.length) {
        // Never interpret examples inside inline code or fenced user-message code.
        if (text[index] == '`') {
            var end = index
            while (end < text.length && text[end] == '`') end++
            val delimiter = text.substring(index, end)
            val closing = text.indexOf(delimiter, end)
            index = if (closing >= 0) closing + delimiter.length else text.length
            continue
        }
        if (text[index] == '\\') {
            val escapedImage = text.startsWith("![", index + 1)
            val escapedLink = if (escapedImage || text.getOrNull(index + 1) == '[')
                readMarkdownLink(text, index + 1, escapedImage) else null
            index = escapedLink?.end ?: (index + 2)
            continue
        }
        val explicitImage = text.startsWith("![", index)
        val markdownLink = if (explicitImage || text[index] == '[') readMarkdownLink(text, index, explicitImage) else null
        if (explicitImage && markdownLink == null) {
            // Streaming/incomplete Markdown must stay text until its closing parenthesis arrives.
            index = text.indexOf('\n', index).takeIf { it >= 0 } ?: text.length
            continue
        }
        val link = when {
            markdownLink != null -> markdownLink
            text[index] == '<' && isHttpPrefix(text, index + 1) -> {
                val end = text.indexOf('>', index + 1)
                if (end > index) ImageLink(end + 1, text.substring(index + 1, end), "") else null
            }
            isHttpPrefix(text, index) && (index == 0 || !text[index - 1].let { it.isLetterOrDigit() || it in "_@" }) -> {
                var end = index
                while (end < text.length && !text[end].isWhitespace() && text[end] !in "`\"'<>") end++
                val url = trimImageUrl(text.substring(index, end))
                ImageLink(index + url.length, url, "")
            }
            else -> null
        }
        if (link != null) {
            if (isHttpImageUrl(link.url, explicitImage)) {
                appendText(index)
                result.add(MessageSegment.Image(link.url, link.label))
                textStart = link.end
            }
            // Skip entire ordinary links too: a nested image URL in a query isn't a preview.
            index = link.end.coerceAtLeast(index + 1)
        } else {
            index++
        }
    }
    appendText(text.length)
    return result.ifEmpty { listOf(MessageSegment.Text(formatText(text))) }
}

private fun isHttpPrefix(text: String, index: Int): Boolean =
    text.regionMatches(index, "https://", 0, 8, ignoreCase = true) ||
        text.regionMatches(index, "http://", 0, 7, ignoreCase = true)

private fun isHttpImageUrl(url: String, explicit: Boolean): Boolean {
    val uri = try { URI(url) } catch (_: Exception) { return false }
    if (uri.scheme?.lowercase(Locale.ROOT) !in setOf("http", "https") || uri.host.isNullOrBlank()) return false
    if (explicit) return true // Image endpoints do not need a filename extension.
    val path = (uri.path ?: "").lowercase(Locale.ROOT)
    if (listOf(".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg", ".avif").any(path::endsWith)) return true
    return uri.host.equals("api.qrserver.com", ignoreCase = true) && path.trimEnd('/') == "/v1/create-qr-code"
}

private fun trimImageUrl(value: String): String {
    var url = value.trimEnd('.', ',', ';')
    for ((close, open) in listOf(')' to '(', ']' to '[', '}' to '{')) {
        while (url.endsWith(close) && url.count { it == close } > url.count { it == open }) url = url.dropLast(1)
    }
    return url
}

/** Read a Markdown destination without truncating parentheses or signed URL query strings. */
private fun readMarkdownLink(text: String, start: Int, image: Boolean): ImageLink? {
    val labelStart = start + if (image) 2 else 1
    var index = labelStart
    var brackets = 1
    while (index < text.length) {
        if (text[index] == '\\') { index += 2; continue }
        if (text[index] == '[') brackets++
        if (text[index] == ']') brackets--
        if (brackets == 0) break
        index++
    }
    if (index >= text.length || !text.startsWith("](", index)) return null
    val label = text.substring(labelStart, index).replace("\\]", "]").replace("\\[", "[")
    index += 2
    while (index < text.length && text[index] == ' ') index++
    val url: String
    if (index < text.length && text[index] == '<') {
        val end = text.indexOf('>', index + 1)
        if (end < 0) return null
        url = text.substring(index + 1, end)
        index = end + 1
    } else {
        val urlStart = index
        var parentheses = 0
        while (index < text.length) {
            val char = text[index]
            if (char == '\\') { index += 2; continue }
            if (char == '(') parentheses++
            if (char == ')') {
                if (parentheses == 0) break
                parentheses--
            }
            if (char.isWhitespace() && parentheses == 0) break
            index++
        }
        if (index > text.length) return null
        url = text.substring(urlStart, index).replace("\\(", "(").replace("\\)", ")")
    }
    while (index < text.length && text[index].isWhitespace()) index++
    if (index < text.length && text[index] in "\"'") {
        val quote = text[index++]
        while (index < text.length && text[index] != quote) {
            if (text[index] == '\\') index++
            index++
        }
        if (index >= text.length) return null
        index++
        while (index < text.length && text[index].isWhitespace()) index++
    }
    if (index >= text.length || text[index] != ')' || url.isBlank()) return null
    return ImageLink(index + 1, url, label)
}
