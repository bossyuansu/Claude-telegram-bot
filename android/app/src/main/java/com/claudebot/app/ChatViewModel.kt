package com.claudebot.app

import android.app.Application
import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Intent
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.ProcessLifecycleOwner
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
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.Constraints
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

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

    /** Message IDs being tracked via WS-native stream events (ignore legacy edit events for these). */
    private val streamingMessageIds = mutableSetOf<Int>()

    companion object {
        private const val PAGE_SIZE = 50
        private const val SEARCH_PAGE_SIZE = 30
        private const val MSG_NOTIFICATION_ID = 100
        private const val MSG_CHANNEL_ID = "bot_messages"
    }

    // Track whether the app is in the foreground for notifications
    @Volatile private var isInForeground = true
    private val lifecycleObserver = LifecycleEventObserver { _, event ->
        when (event) {
            Lifecycle.Event.ON_START -> {
                isInForeground = true
                // Clear message notifications when app comes to foreground
                val nm = application.getSystemService(NotificationManager::class.java)
                nm.cancel(MSG_NOTIFICATION_ID)
                // Reconnect WebSocket when app returns to foreground
                if (settings.isConfigured && connectionState.value != ConnectionState.CONNECTED) {
                    connect()
                }
            }
            Lifecycle.Event.ON_STOP -> isInForeground = false
            else -> {}
        }
    }

    private val wsManager = WebSocketManager(
        onMessage = { msg -> handleWsMessage(msg) },
        onStateChange = { state ->
            android.os.Handler(android.os.Looper.getMainLooper()).post {
                connectionState.value = state
            }
        },
        onServerRestart = {
            // Server rebooted — clear stale busy/task state
            android.os.Handler(android.os.Looper.getMainLooper()).post {
                isBotBusy.value = false
                taskStatus.value = TaskStatus()
                isSwitchingSession.value = false
            }
        },
        onSeqUpdate = { seq, serverId ->
            settings.lastSeq = seq
            settings.knownServerId = serverId
        }
    )

    init {
        // Register lifecycle observer for foreground tracking
        ProcessLifecycleOwner.get().lifecycle.addObserver(lifecycleObserver)
        // Create notification channel for background messages
        createMessageNotificationChannel()
        // Schedule periodic background sync
        scheduleSyncWorker()
        // Load recent messages from DB
        viewModelScope.launch {
            loadInitialMessages()
            // Scroll to bottom after loading history
            if (messages.isNotEmpty()) scrollTrigger.value++
            if (settings.isConfigured) {
                // Restore WS seq state so reconnect doesn't replay everything
                wsManager.restoreState(settings.lastSeq, settings.knownServerId)
                connect()
            }
        }
    }

    private fun scheduleSyncWorker() {
        val constraints = Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED)
            .build()
        val request = PeriodicWorkRequestBuilder<SyncWorker>(15, TimeUnit.MINUTES)
            .setConstraints(constraints)
            .build()
        WorkManager.getInstance(getApplication())
            .enqueueUniquePeriodicWork(
                SyncWorker.WORK_NAME,
                ExistingPeriodicWorkPolicy.KEEP,
                request
            )
    }

    private fun createMessageNotificationChannel() {
        val channel = android.app.NotificationChannel(
            MSG_CHANNEL_ID,
            "Bot Messages",
            NotificationManager.IMPORTANCE_HIGH
        ).apply {
            description = "Notifications for new bot messages"
        }
        getApplication<Application>().getSystemService(NotificationManager::class.java)
            .createNotificationChannel(channel)
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

    private fun notifyIfBackgrounded(text: String, session: String) {
        if (isInForeground) return
        val app = getApplication<Application>()
        val intent = Intent(app, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val pending = PendingIntent.getActivity(
            app, 0, intent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val preview = if (text.length > 120) text.take(120) + "..." else text
        val notification = Notification.Builder(app, MSG_CHANNEL_ID)
            .setContentTitle(session.ifEmpty { "Claude Bot" })
            .setContentText(preview)
            .setStyle(Notification.BigTextStyle().bigText(preview))
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentIntent(pending)
            .setAutoCancel(true)
            .build()
        app.getSystemService(NotificationManager::class.java)
            .notify(MSG_NOTIFICATION_ID, notification)
    }

    /** No-op — foreground service removed to avoid ForegroundServiceDidNotStartInTimeException.
     *  Background sync is handled by WorkManager's periodic SyncWorker instead. */
    private fun updateKeepAlive(busy: Boolean) { /* no-op */ }

    // --- WS message handling ---

    private fun handleWsMessage(msg: WsMessage) {
        val handler = android.os.Handler(android.os.Looper.getMainLooper())
        handler.post {
            when (msg.type) {
                "stream" -> handleStreamEvent(msg)
                "message" -> {
                    val mid = msg.messageId ?: 0
                    // Skip legacy "message" events for stream-tracked messages
                    // (the initial "Thinking..." and continuation messages)
                    if (mid != 0 && mid in streamingMessageIds) return@post
                    // Skip if we already have this message in memory (replay dedup)
                    if (mid != 0 && idToIndex.containsKey(mid)) return@post
                    if (msg.session.isNotEmpty()) currentSession.value = msg.session
                    val chatMsg = ChatMessage(
                        messageId = mid,
                        text = msg.text,
                        isFromBot = true,
                        session = msg.session,
                        timestamp = System.currentTimeMillis(),
                        buttons = msg.buttons
                    )
                    val idx = messages.size
                    messages.add(chatMsg)
                    if (mid != 0) idToIndex[mid] = idx
                    scrollTrigger.value++
                    notifyIfBackgrounded(msg.text, msg.session)
                    viewModelScope.launch(Dispatchers.IO) {
                        dao.insert(chatMsg.toEntity())
                        dbOffset++
                    }
                }
                "status" -> {
                    if (msg.mode == "busy") {
                        isBotBusy.value = msg.active
                        updateKeepAlive(msg.active)
                    } else {
                        taskStatus.value = TaskStatus(
                            mode = msg.mode,
                            phase = msg.phase,
                            step = msg.step,
                            active = msg.active
                        )
                        if (msg.active) {
                            isBotBusy.value = true
                            updateKeepAlive(true)
                        }
                    }
                }
                "edit" -> {
                    val mid = msg.messageId ?: return@post
                    // Skip legacy "edit" events for stream-tracked messages
                    if (mid in streamingMessageIds) return@post
                    if (msg.session.isNotEmpty()) currentSession.value = msg.session
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

    private fun handleStreamEvent(msg: WsMessage) {
        val mid = msg.messageId ?: return
        when (msg.op) {
            "start" -> {
                // Mark this message for stream-based updates (skip legacy edit events)
                streamingMessageIds.add(mid)
                if (msg.session.isNotEmpty()) currentSession.value = msg.session
                val existing = idToIndex[mid]
                if (existing != null && existing < messages.size && messages[existing].messageId == mid) {
                    // Message already exists (from DB load or replay) — reuse it
                    messages[existing] = messages[existing].copy(text = "")
                } else if (existing != null) {
                    // Stale index — rebuild and check again
                    val fallback = messages.indexOfLast { it.messageId == mid }
                    if (fallback >= 0) {
                        idToIndex[mid] = fallback
                        messages[fallback] = messages[fallback].copy(text = "")
                    } else {
                        val chatMsg = ChatMessage(
                            messageId = mid, text = "", isFromBot = true,
                            session = msg.session, timestamp = System.currentTimeMillis()
                        )
                        val idx = messages.size
                        messages.add(chatMsg)
                        idToIndex[mid] = idx
                    }
                } else {
                    // New message — create empty bot message
                    val chatMsg = ChatMessage(
                        messageId = mid,
                        text = "",
                        isFromBot = true,
                        session = msg.session,
                        timestamp = System.currentTimeMillis()
                    )
                    val idx = messages.size
                    messages.add(chatMsg)
                    idToIndex[mid] = idx
                }
                scrollTrigger.value++
            }
            "skip" -> {
                // Server tells us to ignore this message ID (TG continuation message)
                streamingMessageIds.add(mid)
            }
            "append" -> {
                // If we missed the "start" (e.g. reconnect mid-stream), create the message
                if (mid !in streamingMessageIds) {
                    streamingMessageIds.add(mid)
                    if (idToIndex[mid] == null) {
                        val chatMsg = ChatMessage(
                            messageId = mid, text = "", isFromBot = true,
                            session = msg.session, timestamp = System.currentTimeMillis()
                        )
                        val newIdx = messages.size
                        messages.add(chatMsg)
                        idToIndex[mid] = newIdx
                    }
                }
                val idx = idToIndex[mid]
                if (idx != null && idx < messages.size && messages[idx].messageId == mid) {
                    // Strip tool status line if present before appending text
                    var currentText = messages[idx].text
                    if (currentText.endsWith("\u200B")) {
                        // Remove the tool status line (everything from last newline before marker)
                        val lastNl = currentText.lastIndexOf('\n', currentText.length - 2)
                        currentText = if (lastNl >= 0) currentText.substring(0, lastNl) else ""
                    }
                    messages[idx] = messages[idx].copy(text = currentText + msg.text)
                    scrollTrigger.value++
                }
            }
            "tool" -> {
                val idx = idToIndex[mid]
                if (idx != null && idx < messages.size && messages[idx].messageId == mid) {
                    var currentText = messages[idx].text
                    // Remove existing tool status line if present
                    if (currentText.endsWith("\u200B")) {
                        val lastNl = currentText.lastIndexOf('\n', currentText.length - 2)
                        currentText = if (lastNl >= 0) currentText.substring(0, lastNl) else ""
                    }
                    // Append tool status as a replaceable line (marked with zero-width space)
                    val toolLine = formatToolStatus(msg.tool, msg.path)
                    messages[idx] = messages[idx].copy(text = currentText + "\n" + toolLine + "\u200B")
                    scrollTrigger.value++
                }
            }
            "done" -> {
                streamingMessageIds.remove(mid)
                if (msg.session.isNotEmpty()) currentSession.value = msg.session
                val finalText = buildString {
                    append(msg.text)
                    append(formatFileChanges(msg.fileChanges))
                    append("\n\n———\n")
                    append(if (msg.cancelled) "⚠️ _cancelled_" else "✓ _complete_")
                }
                val idx = idToIndex[mid]
                if (idx != null && idx < messages.size && messages[idx].messageId == mid) {
                    val updated = messages[idx].copy(
                        text = finalText,
                        session = msg.session.ifEmpty { messages[idx].session }
                    )
                    messages[idx] = updated
                    scrollTrigger.value++
                    notifyIfBackgrounded(finalText, msg.session)
                    // Persist final text to DB
                    viewModelScope.launch(Dispatchers.IO) {
                        dao.insert(updated.toEntity())
                        dbOffset++
                    }
                } else {
                    // Index missing or stale — search list before creating
                    val fallback = messages.indexOfLast { it.messageId == mid }
                    if (fallback >= 0) {
                        idToIndex[mid] = fallback
                        val updated = messages[fallback].copy(
                            text = finalText,
                            session = msg.session.ifEmpty { messages[fallback].session }
                        )
                        messages[fallback] = updated
                        scrollTrigger.value++
                        notifyIfBackgrounded(finalText, msg.session)
                        viewModelScope.launch(Dispatchers.IO) {
                            dao.insert(updated.toEntity())
                            dbOffset++
                        }
                    } else {
                        val chatMsg = ChatMessage(
                            messageId = mid,
                            text = finalText,
                            isFromBot = true,
                            session = msg.session,
                            timestamp = System.currentTimeMillis()
                        )
                        val newIdx = messages.size
                        messages.add(chatMsg)
                        idToIndex[mid] = newIdx
                        scrollTrigger.value++
                        notifyIfBackgrounded(finalText, msg.session)
                        viewModelScope.launch(Dispatchers.IO) {
                            dao.insert(chatMsg.toEntity())
                            dbOffset++
                        }
                    }
                }
            }
        }
    }

    private fun formatFileChanges(changes: List<Map<String, String>>): String {
        if (changes.isEmpty()) return ""
        val sb = StringBuilder("\n\n\uD83D\uDCC1 *File Operations:*")
        for (change in changes) {
            val type = change["type"] ?: ""
            val path = change["path"] ?: ""
            val shortPath = if (path.length > 80) path.takeLast(77).let { "...$it" } else path
            when (type) {
                "write" -> sb.append("\n  ✅ Created: `$shortPath`")
                "edit" -> sb.append("\n  ✅ Edited: `$shortPath`")
                "bash" -> sb.append("\n  ✅ Ran: `$shortPath`")
                "read" -> sb.append("\n  \uD83D\uDCD6 Read: `$shortPath`")
                "glob", "grep" -> sb.append("\n  \uD83D\uDD0D Search: `$shortPath`")
                else -> sb.append("\n  ✅ $type: `$shortPath`")
            }
        }
        return sb.toString()
    }

    private fun formatToolStatus(tool: String, path: String): String {
        val shortPath = if (path.length > 60) "..." + path.takeLast(57) else path
        return when (tool) {
            "bash" -> "\uD83D\uDD27 Running: `$shortPath`"
            "write" -> "\uD83D\uDD27 Writing: `$shortPath`"
            "edit" -> "\uD83D\uDD27 Editing: `$shortPath`"
            "read" -> "\uD83D\uDD27 Reading: `$shortPath`"
            "glob", "grep" -> "\uD83D\uDD0D Searching: `$shortPath`"
            else -> "\uD83D\uDD27 $tool: `$shortPath`"
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
        ProcessLifecycleOwner.get().lifecycle.removeObserver(lifecycleObserver)
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
