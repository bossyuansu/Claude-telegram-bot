package com.claudebot.app.data

data class InlineButton(
    val text: String,
    val callbackData: String
)

data class ChatMessage(
    val messageId: Int,
    val text: String,
    val isFromBot: Boolean,
    val session: String = "",
    val timestamp: Long = System.currentTimeMillis(),
    val buttons: List<List<InlineButton>> = emptyList() // rows of buttons
)
