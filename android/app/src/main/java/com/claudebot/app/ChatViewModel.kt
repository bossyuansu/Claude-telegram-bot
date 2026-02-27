package com.claudebot.app

import android.app.Application
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.lifecycle.AndroidViewModel
import com.claudebot.app.data.ChatMessage
import com.claudebot.app.data.SettingsRepository
import com.claudebot.app.network.ConnectionState
import com.claudebot.app.network.WebSocketManager
import com.claudebot.app.network.WsMessage

class ChatViewModel(application: Application) : AndroidViewModel(application) {

    val settings = SettingsRepository(application)
    val messages = mutableStateListOf<ChatMessage>()
    val connectionState = mutableStateOf(ConnectionState.DISCONNECTED)
    val showSettings = mutableStateOf(!settings.isConfigured)

    // messageId -> index in messages list for O(1) edit lookup
    private val idToIndex = mutableMapOf<Int, Int>()
    private var localIdCounter = -1  // Negative IDs for user-sent messages

    private val wsManager = WebSocketManager(
        onMessage = { msg -> handleWsMessage(msg) },
        onStateChange = { state -> connectionState.value = state }
    )

    init {
        if (settings.isConfigured) connect()
    }

    fun connect() {
        if (!settings.isConfigured) return
        wsManager.connect(settings.wsUrl())
    }

    fun disconnect() {
        wsManager.disconnect()
    }

    fun reconnect() {
        disconnect()
        connect()
    }

    fun sendMessage(text: String) {
        if (text.isBlank()) return
        // Optimistic local message
        val localId = localIdCounter--
        val msg = ChatMessage(
            messageId = localId,
            text = text,
            isFromBot = false,
            timestamp = System.currentTimeMillis()
        )
        messages.add(msg)
        wsManager.send(text)
    }

    private fun handleWsMessage(msg: WsMessage) {
        // Must update state on main thread — post to main handler
        val handler = android.os.Handler(android.os.Looper.getMainLooper())
        handler.post {
            when (msg.type) {
                "message" -> {
                    val chatMsg = ChatMessage(
                        messageId = msg.messageId ?: 0,
                        text = msg.text,
                        isFromBot = true,
                        session = msg.session,
                        timestamp = System.currentTimeMillis()
                    )
                    val idx = messages.size
                    messages.add(chatMsg)
                    if (msg.messageId != null && msg.messageId != 0) {
                        idToIndex[msg.messageId] = idx
                    }
                }
                "edit" -> {
                    val mid = msg.messageId ?: return@post
                    val idx = idToIndex[mid]
                    if (idx != null && idx < messages.size && messages[idx].messageId == mid) {
                        messages[idx] = messages[idx].copy(
                            text = msg.text,
                            session = msg.session.ifEmpty { messages[idx].session }
                        )
                    }
                    // Fallback: linear scan if index mapping is stale
                    else {
                        val fallbackIdx = messages.indexOfLast { it.messageId == mid }
                        if (fallbackIdx >= 0) {
                            idToIndex[mid] = fallbackIdx
                            messages[fallbackIdx] = messages[fallbackIdx].copy(
                                text = msg.text,
                                session = msg.session.ifEmpty { messages[fallbackIdx].session }
                            )
                        }
                    }
                }
            }
        }
    }

    override fun onCleared() {
        super.onCleared()
        wsManager.disconnect()
    }
}
