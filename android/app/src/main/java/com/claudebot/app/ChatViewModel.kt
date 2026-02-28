package com.claudebot.app

import android.app.Application
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.claudebot.app.data.*
import com.claudebot.app.network.ConnectionState
import com.claudebot.app.network.WebSocketManager
import com.claudebot.app.network.WsMessage
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.Executors

class ChatViewModel(application: Application) : AndroidViewModel(application) {

    val settings = SettingsRepository(application)
    val messages = mutableStateListOf<ChatMessage>()
    val connectionState = mutableStateOf(ConnectionState.DISCONNECTED)
    val currentSession = mutableStateOf("")
    val showSettings = mutableStateOf(!settings.isConfigured)

    /** Incremented on every new message or edit — UI observes this to trigger scroll. */
    val scrollTrigger = mutableStateOf(0)

    // Task status (from WS "status" events)
    data class TaskStatus(val mode: String = "", val phase: String = "", val step: Int = 0, val active: Boolean = false)
    val taskStatus = mutableStateOf(TaskStatus())
    val isBotBusy = mutableStateOf(false)

    /** True while a session-changing command (/switch, /new, /resume, /end, /delete) is in-flight. */
    val isSwitchingSession = mutableStateOf(false)

    // Session list (from /api/sessions)
    data class SessionInfo(val name: String, val busy: Boolean, val isActive: Boolean, val lastCli: String)
    val sessionList = mutableStateListOf<SessionInfo>()

    // Paging state
    val isLoadingMore = mutableStateOf(false)
    val allLoaded = mutableStateOf(false)

    // Search state
    val searchQuery = mutableStateOf("")
    val searchResults = mutableStateListOf<ChatMessage>()
    val isSearching = mutableStateOf(false)
    val searchHasMore = mutableStateOf(false)
    private var searchOffset = 0

    private val dao = AppDatabase.get(application).messageDao()
    private val idToIndex = mutableMapOf<Int, Int>()
    private var localIdCounter = -1
    private val httpClient = OkHttpClient()
    private var dbOffset = 0 // How many messages loaded from DB so far
    private val sendExecutor = Executors.newSingleThreadExecutor() // Serialize outgoing HTTP requests

    companion object {
        private const val PAGE_SIZE = 50
        private const val SEARCH_PAGE_SIZE = 30
    }

    private val wsManager = WebSocketManager(
        onMessage = { msg -> handleWsMessage(msg) },
        onStateChange = { state ->
            android.os.Handler(android.os.Looper.getMainLooper()).post {
                connectionState.value = state
            }
        }
    )

    init {
        // Load recent messages from DB
        viewModelScope.launch {
            loadInitialMessages()
            if (settings.isConfigured) connect()
        }
    }

    private suspend fun loadInitialMessages() {
        val entities = withContext(Dispatchers.IO) { dao.getPage(PAGE_SIZE, 0) }
        // getPage returns newest-first, reverse to get chronological order
        val chatMsgs = entities.reversed().map { it.toChatMessage() }
        messages.addAll(chatMsgs)
        rebuildIndex()
        dbOffset = entities.size
        if (entities.size < PAGE_SIZE) allLoaded.value = true
    }

    /** Called when user scrolls near the top — loads older messages. */
    fun loadMore() {
        if (isLoadingMore.value || allLoaded.value) return
        isLoadingMore.value = true
        viewModelScope.launch {
            val entities = withContext(Dispatchers.IO) { dao.getPage(PAGE_SIZE, dbOffset) }
            if (entities.isEmpty()) {
                allLoaded.value = true
            } else {
                val older = entities.reversed().map { it.toChatMessage() }
                // Prepend to the beginning
                messages.addAll(0, older)
                dbOffset += entities.size
                rebuildIndex()
                if (entities.size < PAGE_SIZE) allLoaded.value = true
            }
            isLoadingMore.value = false
        }
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
        val localId = localIdCounter--
        val msg = ChatMessage(
            messageId = localId,
            text = text,
            isFromBot = false,
            timestamp = System.currentTimeMillis()
        )
        android.os.Handler(android.os.Looper.getMainLooper()).post {
            val idx = messages.size
            messages.add(msg)
            if (localId != 0) idToIndex[localId] = idx
            scrollTrigger.value++
        }
        // Persist to DB
        viewModelScope.launch(Dispatchers.IO) {
            dao.insert(msg.toEntity())
            dbOffset++ // We added one more message that's already in-memory
        }
        // Detect session-changing commands
        val cmd = text.trim().split(" ").first().lowercase()
        val isSessionCmd = cmd in setOf("/switch", "/new", "/resume", "/end", "/delete")
        if (isSessionCmd) {
            android.os.Handler(android.os.Looper.getMainLooper()).post {
                isSwitchingSession.value = true
            }
        }

        // Send via HTTP API (serialized to preserve command order)
        sendExecutor.submit {
            try {
                val json = JSONObject()
                    .put("text", text)
                    .toString()
                val body = json.toRequestBody("application/json".toMediaType())
                val url = "http://${settings.host}:${settings.port}/api/message"
                val reqBuilder = Request.Builder().url(url).post(body)
                if (settings.token.isNotBlank()) {
                    reqBuilder.header("Authorization", "Bearer ${settings.token}")
                }
                httpClient.newCall(reqBuilder.build()).execute().use { /* ignore */ }
            } catch (_: Exception) {}
            if (isSessionCmd) {
                android.os.Handler(android.os.Looper.getMainLooper()).post {
                    isSwitchingSession.value = false
                }
            }
        }
    }

    fun pressButton(messageId: Int, button: InlineButton) {
        val handler = android.os.Handler(android.os.Looper.getMainLooper())
        handler.post {
            val idx = idToIndex[messageId]
            if (idx != null && idx < messages.size && messages[idx].messageId == messageId) {
                messages[idx] = messages[idx].copy(buttons = emptyList())
                // Update DB
                viewModelScope.launch(Dispatchers.IO) {
                    dao.updateByMessageId(messageId, messages[idx].text, messages[idx].session, "")
                }
            }
        }
        sendExecutor.submit {
            try {
                val json = JSONObject()
                    .put("data", button.callbackData)
                    .put("message_id", messageId)
                    .put("chat_id", 0)
                    .toString()
                val body = json.toRequestBody("application/json".toMediaType())
                val url = "http://${settings.host}:${settings.port}/api/callback"
                val reqBuilder = Request.Builder().url(url).post(body)
                if (settings.token.isNotBlank()) {
                    reqBuilder.header("Authorization", "Bearer ${settings.token}")
                }
                httpClient.newCall(reqBuilder.build()).execute().use { /* ignore */ }
            } catch (_: Exception) {}
        }
    }

    // --- Search ---

    fun search(query: String) {
        searchQuery.value = query
        if (query.isBlank()) {
            searchResults.clear()
            isSearching.value = false
            return
        }
        isSearching.value = true
        searchOffset = 0
        viewModelScope.launch {
            val entities = withContext(Dispatchers.IO) { dao.search(query, SEARCH_PAGE_SIZE, 0) }
            searchResults.clear()
            searchResults.addAll(entities.map { it.toChatMessage() })
            searchOffset = entities.size
            searchHasMore.value = entities.size >= SEARCH_PAGE_SIZE
            isSearching.value = false
        }
    }

    fun searchMore() {
        if (isSearching.value || !searchHasMore.value) return
        val query = searchQuery.value
        if (query.isBlank()) return
        isSearching.value = true
        viewModelScope.launch {
            val entities = withContext(Dispatchers.IO) { dao.search(query, SEARCH_PAGE_SIZE, searchOffset) }
            searchResults.addAll(entities.map { it.toChatMessage() })
            searchOffset += entities.size
            searchHasMore.value = entities.size >= SEARCH_PAGE_SIZE
            isSearching.value = false
        }
    }

    fun clearSearch() {
        searchQuery.value = ""
        searchResults.clear()
        isSearching.value = false
        searchHasMore.value = false
    }

    // --- Sessions ---

    fun fetchSessions() {
        Thread {
            try {
                val url = "http://${settings.host}:${settings.port}/api/sessions/0"
                val reqBuilder = Request.Builder().url(url).get()
                if (settings.token.isNotBlank()) {
                    reqBuilder.header("Authorization", "Bearer ${settings.token}")
                }
                httpClient.newCall(reqBuilder.build()).execute().use { resp ->
                    if (!resp.isSuccessful) return@Thread
                    val json = JSONObject(resp.body?.string() ?: return@Thread)
                    val arr = json.optJSONArray("sessions") ?: return@Thread
                    val list = mutableListOf<SessionInfo>()
                    for (i in 0 until arr.length()) {
                        val s = arr.getJSONObject(i)
                        list.add(SessionInfo(
                            name = s.optString("name", ""),
                            busy = s.optBoolean("busy", false),
                            isActive = s.optBoolean("is_active", false),
                            lastCli = s.optString("last_cli", "")
                        ))
                    }
                    android.os.Handler(android.os.Looper.getMainLooper()).post {
                        sessionList.clear()
                        sessionList.addAll(list)
                    }
                }
            } catch (_: Exception) {}
        }.start()
    }

    fun switchSession(name: String) {
        sendMessage("/switch $name")
    }

    // --- WS message handling ---

    private fun handleWsMessage(msg: WsMessage) {
        val handler = android.os.Handler(android.os.Looper.getMainLooper())
        handler.post {
            when (msg.type) {
                "message" -> {
                    if (msg.session.isNotEmpty()) currentSession.value = msg.session
                    val chatMsg = ChatMessage(
                        messageId = msg.messageId ?: 0,
                        text = msg.text,
                        isFromBot = true,
                        session = msg.session,
                        timestamp = System.currentTimeMillis(),
                        buttons = msg.buttons
                    )
                    val idx = messages.size
                    messages.add(chatMsg)
                    if (msg.messageId != null && msg.messageId != 0) {
                        idToIndex[msg.messageId] = idx
                    }
                    scrollTrigger.value++
                    viewModelScope.launch(Dispatchers.IO) {
                        dao.insert(chatMsg.toEntity())
                        dbOffset++
                    }
                }
                "status" -> {
                    if (msg.mode == "busy") {
                        isBotBusy.value = msg.active
                    } else {
                        taskStatus.value = TaskStatus(
                            mode = msg.mode,
                            phase = msg.phase,
                            step = msg.step,
                            active = msg.active
                        )
                        if (msg.active) isBotBusy.value = true
                    }
                }
                "edit" -> {
                    if (msg.session.isNotEmpty()) currentSession.value = msg.session
                    val mid = msg.messageId ?: return@post
                    val idx = idToIndex[mid]
                    if (idx != null && idx < messages.size && messages[idx].messageId == mid) {
                        val updated = messages[idx].copy(
                            text = msg.text,
                            session = msg.session.ifEmpty { messages[idx].session }
                        )
                        messages[idx] = updated
                        scrollTrigger.value++
                        viewModelScope.launch(Dispatchers.IO) {
                            dao.updateByMessageId(mid, updated.text, updated.session, updated.buttonsToJson())
                        }
                    } else {
                        val fallbackIdx = messages.indexOfLast { it.messageId == mid }
                        if (fallbackIdx >= 0) {
                            idToIndex[mid] = fallbackIdx
                            val updated = messages[fallbackIdx].copy(
                                text = msg.text,
                                session = msg.session.ifEmpty { messages[fallbackIdx].session }
                            )
                            messages[fallbackIdx] = updated
                            scrollTrigger.value++
                            viewModelScope.launch(Dispatchers.IO) {
                                dao.updateByMessageId(mid, updated.text, updated.session, updated.buttonsToJson())
                            }
                        } else {
                            val chatMsg = ChatMessage(
                                messageId = mid,
                                text = msg.text,
                                isFromBot = true,
                                session = msg.session,
                                timestamp = System.currentTimeMillis()
                            )
                            val newIdx = messages.size
                            messages.add(chatMsg)
                            idToIndex[mid] = newIdx
                            scrollTrigger.value++
                            viewModelScope.launch(Dispatchers.IO) {
                                dao.insert(chatMsg.toEntity())
                                dbOffset++
                            }
                        }
                    }
                }
            }
        }
    }

    private fun rebuildIndex() {
        idToIndex.clear()
        messages.forEachIndexed { idx, msg ->
            if (msg.messageId != 0) idToIndex[msg.messageId] = idx
        }
    }

    override fun onCleared() {
        super.onCleared()
        wsManager.disconnect()
    }
}

// --- Conversion helpers ---

private fun ChatMessage.toEntity() = MessageEntity(
    messageId = messageId,
    text = text,
    isFromBot = isFromBot,
    session = session,
    timestamp = timestamp,
    buttons = buttonsToJson()
)

fun ChatMessage.buttonsToJson(): String {
    if (buttons.isEmpty()) return ""
    val arr = JSONArray()
    for (row in buttons) {
        val rowArr = JSONArray()
        for (btn in row) {
            rowArr.put(JSONObject().put("text", btn.text).put("data", btn.callbackData))
        }
        arr.put(rowArr)
    }
    return arr.toString()
}

private fun MessageEntity.toChatMessage(): ChatMessage {
    val btns = if (buttons.isNotEmpty()) {
        try {
            val arr = JSONArray(buttons)
            (0 until arr.length()).map { r ->
                val row = arr.getJSONArray(r)
                (0 until row.length()).map { c ->
                    val obj = row.getJSONObject(c)
                    InlineButton(obj.optString("text", ""), obj.optString("data", ""))
                }
            }
        } catch (_: Exception) { emptyList() }
    } else emptyList()

    return ChatMessage(
        messageId = messageId,
        text = text,
        isFromBot = isFromBot,
        session = session,
        timestamp = timestamp,
        buttons = btns
    )
}
