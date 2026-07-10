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
import androidx.compose.foundation.text.ClickableText
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
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.style.TextDecoration
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
    onButtonClick: ((InlineButton) -> Unit)? = null,
    onDownloadClick: (() -> Unit)? = null,
    onShareClick: (() -> Unit)? = null,
    onRetryClick: (() -> Unit)? = null,
    onFilePathClick: (String) -> Unit = {},
    onUrlClick: (String) -> Unit = {}
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
                                    FilePathText(
                                        text = segment.annotated,
                                        color = textColor,
                                        onFilePathClick = onFilePathClick,
                                        onUrlClick = onUrlClick
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
                        FilePathText(
                            text = AnnotatedString(message.text),
                            color = textColor,
                            onFilePathClick = onFilePathClick,
                            onUrlClick = onUrlClick
                        )
                    }
                }
            }
        }

        // File attachment (from /file command)
        if (message.fileName.isNotEmpty()) {
            Spacer(Modifier.height(4.dp))
            FileAttachment(
                fileName = message.fileName,
                fileSize = message.fileSize,
                isDownloaded = message.localFilePath.isNotBlank(),
                onDownload = onDownloadClick,
                onShare = onShareClick
            )
        }

        // File changes / diff viewer
        if (message.fileChanges.isNotEmpty()) {
            Spacer(Modifier.height(4.dp))
            FileChangesSection(
                changes = message.fileChanges,
                onFilePathClick = onFilePathClick
            )
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

        // Timestamp + copied indicator + send failed
        Row(verticalAlignment = Alignment.CenterVertically) {
            if (message.sendFailed) {
                Text(
                    text = "Failed",
                    fontSize = 10.sp,
                    color = DisconnectedRed,
                    modifier = Modifier.padding(horizontal = 4.dp, vertical = 1.dp)
                )
                if (onRetryClick != null) {
                    Text(
                        text = "Retry",
                        fontSize = 10.sp,
                        color = AccentOrange,
                        modifier = Modifier
                            .clickable { onRetryClick() }
                            .padding(horizontal = 4.dp, vertical = 1.dp)
                    )
                }
            }
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
private fun FilePathText(
    text: AnnotatedString,
    color: Color,
    onFilePathClick: (String) -> Unit,
    onUrlClick: (String) -> Unit
) {
    val annotated = remember(text) { text.withFilePathAnnotations() }
    val hasTapTargets = remember(annotated) { annotated.hasTappableLinkTarget() }

    if (!hasTapTargets) {
        Text(
            text = text,
            color = color,
            fontSize = 14.sp,
            lineHeight = 20.sp
        )
        return
    }

    ClickableText(
        text = annotated,
        style = TextStyle(
            color = color,
            fontSize = 14.sp,
            lineHeight = 20.sp
        ),
        onClick = { offset ->
            val urlLink = annotated
                .getStringAnnotations(URL_TAG, offset, offset)
                .firstOrNull()
                ?.item

            if (urlLink != null) {
                when {
                    isLikelyFilePathTarget(urlLink) -> {
                        val commandPath = normalizeFilePathForCommand(urlLink)
                        if (commandPath.isNotEmpty()) onFilePathClick(commandPath)
                    }
                    isWebUrlTarget(urlLink) -> {
                        val url = normalizeUrlForOpen(urlLink)
                        if (url.isNotEmpty()) onUrlClick(url)
                    }
                }
                return@ClickableText
            }

            val filePath = annotated
                .getStringAnnotations(FILE_PATH_TAG, offset, offset)
                .firstOrNull()
                ?.item

            if (filePath != null) {
                val commandPath = normalizeFilePathForCommand(filePath)
                if (commandPath.isNotEmpty()) onFilePathClick(commandPath)
            }
        }
    )
}

internal const val FILE_PATH_TAG = "FILE_PATH"
internal const val URL_TAG = "URL"
private const val FILE_EXTENSIONS =
    "kt|kts|java|py|js|jsx|ts|tsx|json|md|yml|yaml|toml|gradle|xml|html|css|scss|sql|sh|bash|zsh|rb|go|rs|c|cc|cpp|h|hpp|swift|php|txt|csv|env|lock|png|jpg|jpeg|webp|gif|pdf|docx|pptx|xlsx|zip|tar|gz|log"
private const val FILE_NAME_CHARS = """[^\s`"'<>/\\]+"""

private val FILE_PATH_REGEX = Regex(
    """(?<![A-Za-z0-9_@])(?:(?:~/|/|\./|\../|$FILE_NAME_CHARS/)[^\s`"'<>]+|$FILE_NAME_CHARS\.(?:$FILE_EXTENSIONS)(?::\d+(?::\d+)?)?)""",
    RegexOption.IGNORE_CASE
)
private val SINGLE_FILE_NAME_REGEX = Regex(
    """^$FILE_NAME_CHARS\.(?:$FILE_EXTENSIONS)(?::\d+(?::\d+)?)?$""",
    RegexOption.IGNORE_CASE
)
private val EXTENSIONLESS_FILE_NAMES = setOf(
    ".env",
    ".gitignore",
    ".npmrc",
    "brewfile",
    "containerfile",
    "dockerfile",
    "gemfile",
    "gradlew",
    "jenkinsfile",
    "license",
    "makefile",
    "notice",
    "podfile",
    "procfile",
    "rakefile",
    "readme"
)
private val URL_SCHEME_REGEX = Regex("""^[A-Za-z][A-Za-z0-9+.-]*://""")
private val WEB_URL_REGEX = Regex(
    """(?<![A-Za-z0-9_@])(?:https?://|www\.)[^\s`"'<>]+""",
    RegexOption.IGNORE_CASE
)
private val FILE_LINE_SUFFIX_REGEX = Regex(""":\d+(?::\d+)?$""")
private val HASH_LINE_SUFFIX_REGEX = Regex("""#L\d+(?:-L?\d+)?$""", RegexOption.IGNORE_CASE)

private data class TextRange(val start: Int, val end: Int)

internal fun AnnotatedString.withFilePathAnnotations(): AnnotatedString {
    val source = this
    val builder = AnnotatedString.Builder()
    builder.append(source)
    val urlRanges = source.getStringAnnotations(URL_TAG, 0, source.length)
        .filter { isLikelyFilePathTarget(it.item) || isWebUrlTarget(it.item) }
        .map { TextRange(it.start, it.end) }
        .toMutableList()

    WEB_URL_REGEX.findAll(source.text).forEach { match ->
        val raw = match.value
        val trimmed = trimUrlCandidate(raw)
        if (trimmed.isEmpty()) return@forEach

        val start = match.range.first
        val end = start + trimmed.length
        if (end > source.text.length) return@forEach
        if (!isWebUrlTarget(trimmed)) return@forEach
        if (urlRanges.any { rangesOverlap(start, end, it.start, it.end) }) return@forEach

        builder.addStringAnnotation(URL_TAG, normalizeUrlForOpen(trimmed), start, end)
        builder.addStyle(
            SpanStyle(
                color = AccentOrange,
                textDecoration = TextDecoration.Underline
            ),
            start,
            end
        )
        urlRanges.add(TextRange(start, end))
    }

    FILE_PATH_REGEX.findAll(source.text).forEach { match ->
        val raw = match.value
        val trimmed = trimFilePathCandidate(raw)
        if (trimmed.isEmpty()) return@forEach

        val start = match.range.first
        val end = start + trimmed.length
        if (end > source.text.length) return@forEach
        if (!isLikelyFilePathTarget(trimmed)) return@forEach
        if (trimmed.startsWith("//")) return@forEach
        if (start > 0 && source.text[start - 1] == ':' && trimmed.startsWith("/")) return@forEach
        if (urlRanges.any { rangesOverlap(start, end, it.start, it.end) }) return@forEach

        builder.addStringAnnotation(FILE_PATH_TAG, trimmed, start, end)
        builder.addStyle(
            SpanStyle(
                color = AccentOrange,
                textDecoration = TextDecoration.Underline
            ),
            start,
            end
        )
    }

    return builder.toAnnotatedString()
}

internal fun AnnotatedString.hasTappableFilePath(): Boolean {
    val end = text.length
    return getStringAnnotations(FILE_PATH_TAG, 0, end).isNotEmpty() ||
        getStringAnnotations(URL_TAG, 0, end).any { isLikelyFilePathTarget(it.item) }
}

internal fun AnnotatedString.hasTappableLinkTarget(): Boolean {
    val end = text.length
    return hasTappableFilePath() ||
        getStringAnnotations(URL_TAG, 0, end).any { isWebUrlTarget(it.item) }
}

internal fun normalizeFilePathForCommand(value: String): String {
    var path = trimFilePathCandidate(value)
    if (path.startsWith("file://")) path = path.removePrefix("file://")
    path = path.trim { it == '<' || it == '>' || it == '`' || it == '"' || it == '\'' }
    path = path.replace(HASH_LINE_SUFFIX_REGEX, "")
    path = path.replace(FILE_LINE_SUFFIX_REGEX, "")
    path = normalizeMiddleEllipsis(path)
    return path
}

internal fun normalizeUrlForOpen(value: String): String {
    val url = trimUrlCandidate(value)
    return if (url.startsWith("www.", ignoreCase = true)) "https://$url" else url
}

internal fun isWebUrlTarget(value: String): Boolean {
    val url = normalizeUrlForOpen(value)
    val lower = url.lowercase(Locale.ROOT)
    val scheme = when {
        lower.startsWith("http://") -> "http://"
        lower.startsWith("https://") -> "https://"
        else -> return false
    }
    val authority = url.substring(scheme.length)
        .substringBefore("/")
        .substringBefore("?")
        .substringBefore("#")
    return authority.isNotBlank() && !authority.startsWith(".")
}

private fun normalizeMiddleEllipsis(path: String): String {
    if (path.startsWith(".../")) return path
    val marker = "/.../"
    val markerIndex = path.indexOf(marker)
    if (markerIndex < 0) return path

    val suffix = path.substring(markerIndex + marker.length)
    return if (suffix.isNotBlank()) ".../$suffix" else path
}

private fun trimFilePathCandidate(value: String): String {
    var path = value.trim { it == '`' || it == '"' || it == '\'' || it == '<' || it == '>' }
    while (path.isNotEmpty() && path.last() in listOf('.', ',', ';')) {
        path = path.dropLast(1)
    }
    while (path.isNotEmpty() && path.last() == ':' && !FILE_LINE_SUFFIX_REGEX.containsMatchIn(path)) {
        path = path.dropLast(1)
    }
    path = trimUnbalancedCloser(path, ')', '(')
    path = trimUnbalancedCloser(path, ']', '[')
    path = trimUnbalancedCloser(path, '}', '{')
    return path
}

private fun trimUrlCandidate(value: String): String {
    var url = value.trim { it == '`' || it == '"' || it == '\'' || it == '<' || it == '>' }
    while (url.isNotEmpty() && url.last() in listOf('.', ',', ';')) {
        url = url.dropLast(1)
    }
    url = trimUnbalancedCloser(url, ')', '(')
    url = trimUnbalancedCloser(url, ']', '[')
    url = trimUnbalancedCloser(url, '}', '{')
    return url
}

private fun rangesOverlap(start: Int, end: Int, otherStart: Int, otherEnd: Int): Boolean {
    return start < otherEnd && end > otherStart
}

private fun trimUnbalancedCloser(value: String, close: Char, open: Char): String {
    var path = value
    while (path.isNotEmpty() && path.last() == close && path.count { it == close } > path.count { it == open }) {
        path = path.dropLast(1)
    }
    return path
}

internal fun isLikelyFilePathTarget(value: String): Boolean {
    val path = normalizeFilePathForCommand(value)
    if (path.isEmpty()) return false
    if (path.startsWith("file://")) return true
    if (URL_SCHEME_REGEX.containsMatchIn(path)) return false
    if (path.startsWith("//")) return false
    val hasFileLikeLeaf = hasFileLikeLeaf(path)
    if (path.startsWith("/") || path.startsWith("./") || path.startsWith("../") || path.startsWith("~/")) {
        return hasFileLikeLeaf
    }
    if (path.contains("/")) {
        val firstSegment = path.substringBefore("/")
        return hasFileLikeLeaf && !looksLikeWebHost(firstSegment)
    }
    return hasFileLikeLeaf
}

private fun hasFileLikeLeaf(path: String): Boolean {
    val leaf = path.substringAfterLast("/")
    if (leaf.isBlank()) return false
    return SINGLE_FILE_NAME_REGEX.matches(leaf) ||
        EXTENSIONLESS_FILE_NAMES.contains(leaf.lowercase(Locale.ROOT))
}

private fun looksLikeWebHost(firstSegment: String): Boolean {
    if (firstSegment.startsWith(".")) return false
    if (!firstSegment.contains(".")) return false
    val suffix = firstSegment.substringAfterLast(".")
    return suffix.length in 2..8 && suffix.all { it.isLetter() }
}

@Composable
private fun FileChangesSection(
    changes: List<FileChange>,
    onFilePathClick: (String) -> Unit
) {
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
                FileChangeItem(
                    change = change,
                    onFilePathClick = onFilePathClick
                )
            }
        }
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun FileChangeItem(
    change: FileChange,
    onFilePathClick: (String) -> Unit
) {
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
                        onClick = { onFilePathClick(change.path) },
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
                    color = SessionLabel,
                    modifier = Modifier
                        .clickable { showDiff = !showDiff }
                        .padding(start = 6.dp)
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

@Composable
private fun FileAttachment(
    fileName: String,
    fileSize: Long,
    isDownloaded: Boolean,
    onDownload: (() -> Unit)?,
    onShare: (() -> Unit)? = null
) {
    Row(
        modifier = Modifier
            .widthIn(max = 320.dp)
            .clip(RoundedCornerShape(8.dp))
            .border(1.dp, BotBubbleBorder, RoundedCornerShape(8.dp))
            .background(DarkSurface)
            .padding(10.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Text(
                text = fileName,
                fontSize = 12.sp,
                color = BotText,
                maxLines = 1,
                overflow = androidx.compose.ui.text.style.TextOverflow.Ellipsis
            )
            if (fileSize > 0) {
                val sizeStr = when {
                    fileSize < 1024 -> "${fileSize}B"
                    fileSize < 1024 * 1024 -> "${fileSize / 1024}KB"
                    else -> "${"%.1f".format(fileSize / (1024.0 * 1024.0))}MB"
                }
                Text(text = sizeStr, fontSize = 10.sp, color = SessionLabel)
            }
        }
        if (!isDownloaded && onDownload != null) {
            Text(
                text = "Download",
                color = AccentOrange,
                fontSize = 12.sp,
                modifier = Modifier
                    .clickable { onDownload() }
                    .padding(start = 8.dp)
            )
        } else if (isDownloaded) {
            if (onShare != null) {
                Text(
                    text = "Share",
                    color = AccentOrange,
                    fontSize = 12.sp,
                    modifier = Modifier
                        .clickable { onShare() }
                        .padding(start = 8.dp)
                )
            } else {
                Text(
                    text = "Saved ✓",
                    color = ConnectedGreen,
                    fontSize = 10.sp,
                    modifier = Modifier.padding(start = 8.dp)
                )
            }
        }
    }
}
