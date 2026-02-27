package com.claudebot.app.data

data class ChatMessage(
    val messageId: Int,
    val text: String,
    val isFromBot: Boolean,
    val session: String = "",
    val timestamp: Long = System.currentTimeMillis()
)
