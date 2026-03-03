package com.claudebot.app.data

data class InlineButton(
    val text: String,
    val callbackData: String
)

data class FileChange(
    val type: String,       // "edit", "write", "bash", "read", "glob", "grep"
    val path: String,
    val old: String = "",   // For edit: old_string
    val new: String = "",   // For edit: new_string
    val content: String = "" // For write: file content
)

data class ChatMessage(
    val messageId: Int,
    val text: String,
    val isFromBot: Boolean,
    val session: String = "",
    val isReplay: Boolean = false,
    val timestamp: Long = System.currentTimeMillis(),
    val buttons: List<List<InlineButton>> = emptyList(), // rows of buttons
    val fileChanges: List<FileChange> = emptyList()
)
