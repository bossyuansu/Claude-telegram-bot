package com.claudebot.app.data

import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "messages")
data class MessageEntity(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val messageId: Int,
    val text: String,
    val isFromBot: Boolean,
    val session: String = "",
    val timestamp: Long = System.currentTimeMillis(),
    val buttons: String = "" // JSON-encoded buttons, empty when none
)
