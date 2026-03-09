package com.claudebot.app

import android.app.Application
import android.util.Log
import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Intent
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateMapOf
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
import androidx.work.BackoffPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.Constraints
import androidx.work.OneTimeWorkRequestBuilder
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

    // Mission Control: all active autonomous tasks across sessions
    data class ActiveTask(
        val mode: String,
        val session: String,
        val task: String,
        val phase: String,
        val step: Int,
        val started: Long,
        val paused: Boolean = false
    )
    val activeTasks = mutableStateMapOf<String, ActiveTask>()

    // Scheduled tasks
    data class ScheduledTask(
        val id: String,
        val cwd: String,
        val prompt: String,
        val scheduleType: String,  // "cron" | "once"
        val cronExpr: String?,
        val runAt: String?,
        val enabled: Boolean,
        val nextRun: Long?,        // epoch seconds
        val lastRun: Long?,
        val lastResult: String?,
        val runCount: Int,
    )
    val scheduledTasks = mutableStateListOf<ScheduledTask>()

    /** True while a session-changing command (/switch, /new, /resume, /end, /delete) is in-flight. */
    val isSwitchingSession = mutableStateOf(false)

    /** Pending action bar — shown when a message with buttons arrives (plan approval, questions). */
    data class PendingAction(val text: String, val buttons: List<List<InlineButton>>, val messageId: Int)
    val pendingAction = mutableStateOf<PendingAction?>(null)

    // Session list (from /api/sessions)
    data class SessionInfo(val name: String, val busy: Boolean, val isActive: Boolean, val lastCli: String)
    val sessionList = mutableStateListOf<SessionInfo>()

    // Session filter state
    val sessionFilter = mutableStateOf<String?>(null)
    val availableSessions = mutableStateListOf<String>()
    /** Incremented on every filter change; in-flight loads compare to detect staleness. */
    private var filterGeneration = 0

    /** The session the user is currently viewing (filter takes priority over active). */
    val effectiveSession: String
        get() = sessionFilter.value ?: currentSession.value

    /** True when the chat is filtered to a session that differs from the server-side active session. */
    val isViewingOtherSession: Boolean
        get() {
            val filter = sessionFilter.value ?: return false
            return filter != currentSession.value
        }

    /** Set filter from Mission Control tap — filters chat, does NOT switch the server session. */
    fun viewTaskSession(sessionName: String) {
        setSessionFilter(sessionName)
    }

    /** Switch server session to match the current filter, then clear the filter. */
    fun quickSwitchToFilteredSession() {
        val target = sessionFilter.value ?: return
        switchSession(target)
        setSessionFilter(null)
    }

    /** Clear the session filter and return to the normal (all-sessions) view. */
    fun clearSessionFilter() {
        setSessionFilter(null)
    }

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
    @Volatile private var wsStateReady = false

    /** Message IDs being tracked via WS-native stream events (ignore legacy edit events for these). */
    private val streamingMessageIds = mutableSetOf<Int>()
    /** Message IDs currently being inserted from WS replay; prevents duplicate races. */
    private val pendingIncomingMessageIds = mutableSetOf<Int>()
    /** Positive message IDs already persisted in local DB (used for replay dedupe without async races). */
    private val persistedMessageIds = mutableSetOf<Int>()
    /** Positive message IDs that already reached terminal state ("done"). */
    private val finalizedMessageIds = mutableSetOf<Int>()

    /** Debounce DB writes during streaming — track last persist time per message ID. */
    private val lastStreamPersist = mutableMapOf<Int, Long>()
    private val STREAM_PERSIST_INTERVAL_MS = 2000L

    companion object {
        private const val PAGE_SIZE = 50
        private const val SEARCH_PAGE_SIZE = 30
        private const val MSG_NOTIFICATION_ID = 100
        private const val MSG_CHANNEL_ID = "bot_messages"
        private const val NOTIFY_EVENT_ACTIONABLE = "actionable"
        private const val NOTIFY_EVENT_TERMINAL = "terminal"
    }

    // Track whether the app is in the foreground for notifications
    @Volatile private var isInForeground = true
    private val lifecycleObserver = LifecycleEventObserver { _, event ->
        when (event) {
            Lifecycle.Event.ON_START -> {
                isInForeground = true
                WsConnectionGate.setForegroundActive(true)
                // Foreground owns WS and can preempt any background catch-up run.
                WsConnectionGate.acquireForeground()
                // Clear message notifications when app comes to foreground
                val nm = application.getSystemService(NotificationManager::class.java)
                nm.cancelAll()
                // Foreground owns the live WS. Stop background catch-up worker.
                if (settings.isConfigured) {
                    cancelSyncWorkers()
                    if (wsStateReady) {
                        val (lastSeq, knownServerId) = settings.getWsState()
                        wsManager.restoreState(lastSeq, knownServerId)
                        connect()
                    }
                }
            }
            Lifecycle.Event.ON_STOP -> {
                isInForeground = false
                WsConnectionGate.setForegroundActive(false)
                // Background mode: release live WS and rely on periodic catch-up.
                if (settings.isConfigured) {
                    disconnect()
                    scheduleImmediateSyncWorker()
                    scheduleSyncWorker()
                }
            }
            else -> {}
        }
    }

    private val wsManager = WebSocketManager(
        onMessage = { msg -> handleWsMessage(msg) },
        onStateChange = { state ->
            android.os.Handler(android.os.Looper.getMainLooper()).post {
                connectionState.value = state
                settings.wsLastState = state.name
                if (state == ConnectionState.CONNECTED) {
                    settings.wsLastError = ""
                    fetchActiveTasks()
                    fetchSessions()
                }
            }
        },
        onServerRestart = {
            // Server rebooted — clear stale busy/task state
            android.os.Handler(android.os.Looper.getMainLooper()).post {
                isBotBusy.value = false
                taskStatus.value = TaskStatus()
                activeTasks.clear()
                isSwitchingSession.value = false
            }
        },
        onSeqUpdate = { seq, serverId ->
            settings.updateWsState(seq, serverId)
            if (settings.wsLastError.isNotBlank()) {
                settings.wsLastError = ""
            }
        },
        onError = { message ->
            settings.wsLastError = message
        }
    )

    init {
        android.util.Log.d("WS", "ChatViewModel init START")
        // Register lifecycle observer for foreground tracking
        ProcessLifecycleOwner.get().lifecycle.addObserver(lifecycleObserver)
        // Create notification channel for background messages
        createMessageNotificationChannel()
        // App starts in foreground: keep periodic worker cancelled.
        cancelSyncWorkers()
        // Load recent messages from DB
        viewModelScope.launch {
            try {
                android.util.Log.d("WS", "init: loading messages...")
                withContext(Dispatchers.IO) {
                    dao.deleteDuplicateMessageIds()
                    persistedMessageIds.clear()
                    persistedMessageIds.addAll(dao.getPersistedMessageIds())
                    finalizedMessageIds.clear()
                    finalizedMessageIds.addAll(dao.getFinalizedMessageIds("———"))
                }
                loadInitialMessages()
                fetchLocalSessions()
                android.util.Log.d("WS", "init: loaded ${messages.size} messages, configured=${settings.isConfigured}")
                // Scroll to bottom after loading history
                if (messages.isNotEmpty()) scrollTrigger.value++
                if (settings.isConfigured) {
                    // Read seq/server_id atomically to avoid mismatched state on reconnect.
                    val (lastSeq, knownServerId) = settings.getWsState()
                    // Restore WS seq state so reconnect doesn't replay everything.
                    wsManager.restoreState(lastSeq, knownServerId)
                    wsStateReady = true
                    if (ProcessLifecycleOwner.get().lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)) {
                        connect()
                    }
                }
            } catch (e: Exception) {
                android.util.Log.e("WS", "init FAILED", e)
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
                ExistingPeriodicWorkPolicy.UPDATE,
                request
            )
    }

    private fun scheduleImmediateSyncWorker() {
        val constraints = Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED)
            .build()
        val request = OneTimeWorkRequestBuilder<SyncWorker>()
            .setConstraints(constraints)
            .setInitialDelay(5, TimeUnit.SECONDS)
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
            .build()
        WorkManager.getInstance(getApplication())
            .enqueueUniqueWork(
                SyncWorker.IMMEDIATE_WORK_NAME,
                ExistingWorkPolicy.REPLACE,
                request
            )
    }

    private fun cancelSyncWorkers() {
        WorkManager.getInstance(getApplication())
            .cancelUniqueWork(SyncWorker.WORK_NAME)
        WorkManager.getInstance(getApplication())
            .cancelUniqueWork(SyncWorker.IMMEDIATE_WORK_NAME)
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
        val gen = filterGeneration
        val filter = sessionFilter.value
        val currentOffset = dbOffset
        viewModelScope.launch {
            val entities = withContext(Dispatchers.IO) {
                if (filter != null) dao.getPageBySession(filter, PAGE_SIZE, currentOffset)
                else dao.getPage(PAGE_SIZE, currentOffset)
            }
            // Discard if filter changed while we were loading
            if (gen != filterGeneration) {
                isLoadingMore.value = false
                return@launch
            }
            if (entities.isEmpty()) {
                allLoaded.value = true
            } else {
                val older = entities.reversed().map { it.toChatMessage() }
                messages.addAll(0, older)
                dbOffset += entities.size
                rebuildIndex()
                if (entities.size < PAGE_SIZE) allLoaded.value = true
            }
            isLoadingMore.value = false
        }
    }

    fun fetchLocalSessions() {
        viewModelScope.launch {
            val sessions = withContext(Dispatchers.IO) { dao.getDistinctSessions() }
            availableSessions.clear()
            availableSessions.addAll(sessions)
        }
    }

    fun setSessionFilter(session: String?) {
        sessionFilter.value = session
        // Update busy state to reflect the newly viewed session
        val viewing = session ?: currentSession.value
        isBotBusy.value = viewing.isNotEmpty() && activeTasks.containsKey(viewing)
        filterGeneration++
        val gen = filterGeneration
        messages.clear()
        idToIndex.clear()
        dbOffset = 0
        allLoaded.value = false
        isLoadingMore.value = false
        viewModelScope.launch {
            val entities = withContext(Dispatchers.IO) {
                if (session != null) dao.getPageBySession(session, PAGE_SIZE, 0)
                else dao.getPage(PAGE_SIZE, 0)
            }
            // Discard if another filter change happened while loading
            if (gen != filterGeneration) return@launch
            val chatMsgs = entities.reversed().map { it.toChatMessage() }
            messages.addAll(chatMsgs)
            rebuildIndex()
            dbOffset = entities.size
            if (entities.size < PAGE_SIZE) allLoaded.value = true
            if (messages.isNotEmpty()) scrollTrigger.value++
        }
    }

    private fun matchesSessionFilter(session: String): Boolean {
        val filter = sessionFilter.value ?: return true
        return session == filter
    }

    fun connect() {
        android.util.Log.d("WS", "ChatViewModel.connect() configured=${settings.isConfigured} url=${settings.wsUrl()}")
        if (!settings.isConfigured) return
        WsConnectionGate.acquireForeground()
        wsManager.connect(settings.wsUrl())
    }

    fun disconnect() {
        wsManager.disconnect()
        WsConnectionGate.release(WsConnectionGate.OWNER_FOREGROUND)
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

    /** Dismiss the pending action bar without selecting any option. */
    fun dismissPendingAction() {
        pendingAction.value = null
    }

    fun pressButton(messageId: Int, button: InlineButton) {
        val handler = android.os.Handler(android.os.Looper.getMainLooper())
        handler.post {
            // Clear pending action bar if this was the active one
            pendingAction.value?.let { if (it.messageId == messageId) pendingAction.value = null }
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
                    val activeSession = list.firstOrNull { it.isActive }?.name
                    android.os.Handler(android.os.Looper.getMainLooper()).post {
                        sessionList.clear()
                        sessionList.addAll(list)
                        if (!activeSession.isNullOrEmpty()) {
                            currentSession.value = activeSession
                        }
                    }
                }
            } catch (_: Exception) {}
        }.start()
    }

    fun switchSession(name: String) {
        sendMessage("/switch $name")
    }

    fun fetchActiveTasks() {
        Thread {
            try {
                val url = "http://${settings.host}:${settings.port}/api/active-tasks/0"
                val reqBuilder = Request.Builder().url(url).get()
                if (settings.token.isNotBlank()) {
                    reqBuilder.header("Authorization", "Bearer ${settings.token}")
                }
                httpClient.newCall(reqBuilder.build()).execute().use { resp ->
                    if (!resp.isSuccessful) return@Thread
                    val json = JSONObject(resp.body?.string() ?: return@Thread)
                    val arr = json.optJSONArray("tasks") ?: return@Thread
                    val tasks = mutableMapOf<String, ActiveTask>()
                    for (i in 0 until arr.length()) {
                        val t = arr.getJSONObject(i)
                        val session = t.optString("session", "")
                        if (session.isNotEmpty()) {
                            tasks[session] = ActiveTask(
                                mode = t.optString("mode", ""),
                                session = session,
                                task = t.optString("task", ""),
                                phase = t.optString("phase", ""),
                                step = t.optInt("step", 0),
                                started = t.optLong("started", 0),
                                paused = t.optBoolean("paused", false)
                            )
                        }
                    }
                    android.os.Handler(android.os.Looper.getMainLooper()).post {
                        // Merge instead of clear+putAll to avoid overwriting
                        // more-recent WS updates that arrived during the HTTP call
                        val stale = activeTasks.keys - tasks.keys
                        stale.forEach { activeTasks.remove(it) }
                        activeTasks.putAll(tasks)
                        // Derive isBotBusy from active tasks for the session being viewed
                        val viewing = effectiveSession
                        isBotBusy.value = viewing.isNotEmpty() && tasks.containsKey(viewing)
                    }
                }
            } catch (e: Exception) {
                Log.w("ChatVM", "fetchActiveTasks failed: ${e.message}")
            }
        }.start()
    }

    fun cancelTask(sessionName: String) {
        // Optimistic removal for immediate UI feedback
        activeTasks.remove(sessionName)
        sendExecutor.submit {
            try {
                val url = "http://${settings.host}:${settings.port}/api/cancel-task"
                val json = JSONObject().put("session", sessionName).toString()
                val body = json.toRequestBody("application/json".toMediaType())
                val reqBuilder = Request.Builder().url(url).post(body)
                if (settings.token.isNotBlank()) {
                    reqBuilder.header("Authorization", "Bearer ${settings.token}")
                }
                httpClient.newCall(reqBuilder.build()).execute().use { /* ignore */ }
            } catch (e: Exception) {
                Log.w("ChatVM", "cancelTask failed: ${e.message}")
            }
        }
    }

    fun pauseTask(sessionName: String) {
        // Optimistic update
        activeTasks[sessionName]?.let { activeTasks[sessionName] = it.copy(paused = true) }
        sendExecutor.submit {
            try {
                val url = "http://${settings.host}:${settings.port}/api/pause-task"
                val json = JSONObject().put("session", sessionName).toString()
                val body = json.toRequestBody("application/json".toMediaType())
                val reqBuilder = Request.Builder().url(url).post(body)
                if (settings.token.isNotBlank()) {
                    reqBuilder.header("Authorization", "Bearer ${settings.token}")
                }
                httpClient.newCall(reqBuilder.build()).execute().use { /* ignore */ }
            } catch (e: Exception) {
                Log.w("ChatVM", "pauseTask failed: ${e.message}")
            }
        }
    }

    fun resumeTask(sessionName: String) {
        // Optimistic update
        activeTasks[sessionName]?.let { activeTasks[sessionName] = it.copy(paused = false) }
        sendExecutor.submit {
            try {
                val url = "http://${settings.host}:${settings.port}/api/resume-task"
                val json = JSONObject().put("session", sessionName).toString()
                val body = json.toRequestBody("application/json".toMediaType())
                val reqBuilder = Request.Builder().url(url).post(body)
                if (settings.token.isNotBlank()) {
                    reqBuilder.header("Authorization", "Bearer ${settings.token}")
                }
                httpClient.newCall(reqBuilder.build()).execute().use { /* ignore */ }
            } catch (e: Exception) {
                Log.w("ChatVM", "resumeTask failed: ${e.message}")
            }
        }
    }

    // --- Scheduled tasks ---

    fun fetchScheduledTasks() {
        Thread {
            try {
                val url = "http://${settings.host}:${settings.port}/api/scheduled-tasks/0"
                val reqBuilder = Request.Builder().url(url).get()
                if (settings.token.isNotBlank()) {
                    reqBuilder.header("Authorization", "Bearer ${settings.token}")
                }
                httpClient.newCall(reqBuilder.build()).execute().use { resp ->
                    if (!resp.isSuccessful) return@Thread
                    val arr = JSONArray(resp.body?.string() ?: return@Thread)
                    val tasks = mutableListOf<ScheduledTask>()
                    for (i in 0 until arr.length()) {
                        val t = arr.getJSONObject(i)
                        tasks.add(ScheduledTask(
                            id = t.optString("id"),
                            cwd = t.optString("cwd", ""),
                            prompt = t.optString("prompt"),
                            scheduleType = t.optString("schedule_type"),
                            cronExpr = if (t.has("cron_expr") && !t.isNull("cron_expr")) t.optString("cron_expr") else null,
                            runAt = if (t.has("run_at") && !t.isNull("run_at")) t.optString("run_at") else null,
                            enabled = t.optBoolean("enabled", true),
                            nextRun = if (t.has("next_run") && !t.isNull("next_run")) t.optLong("next_run") else null,
                            lastRun = if (t.has("last_run") && !t.isNull("last_run")) t.optLong("last_run") else null,
                            lastResult = if (t.has("last_result") && !t.isNull("last_result")) t.optString("last_result") else null,
                            runCount = t.optInt("run_count", 0),
                        ))
                    }
                    android.os.Handler(android.os.Looper.getMainLooper()).post {
                        scheduledTasks.clear()
                        scheduledTasks.addAll(tasks)
                    }
                }
            } catch (_: Exception) {}
        }.start()
    }

    fun createScheduledTask(
        sessionName: String, prompt: String, scheduleType: String,
        cronExpr: String?, runAt: String?,
    ) {
        sendExecutor.submit {
            try {
                val url = "http://${settings.host}:${settings.port}/api/schedule-task"
                val json = JSONObject().apply {
                    put("session_name", sessionName)
                    put("prompt", prompt)
                    put("schedule_type", scheduleType)
                    if (cronExpr != null) put("cron_expr", cronExpr)
                    if (runAt != null) put("run_at", runAt)
                }.toString()
                val body = json.toRequestBody("application/json".toMediaType())
                val reqBuilder = Request.Builder().url(url).post(body)
                if (settings.token.isNotBlank()) {
                    reqBuilder.header("Authorization", "Bearer ${settings.token}")
                }
                httpClient.newCall(reqBuilder.build()).execute().use { /* re-fetch will come via WS */ }
            } catch (e: Exception) {
                Log.w("ChatVM", "createScheduledTask failed: ${e.message}")
            }
        }
    }

    fun toggleScheduledTask(taskId: String, enabled: Boolean) {
        // Optimistic update
        val idx = scheduledTasks.indexOfFirst { it.id == taskId }
        if (idx >= 0) scheduledTasks[idx] = scheduledTasks[idx].copy(enabled = enabled)
        sendExecutor.submit {
            try {
                val url = "http://${settings.host}:${settings.port}/api/schedule-task/$taskId"
                val json = JSONObject().put("enabled", enabled).toString()
                val body = json.toRequestBody("application/json".toMediaType())
                val reqBuilder = Request.Builder().url(url).put(body)
                if (settings.token.isNotBlank()) {
                    reqBuilder.header("Authorization", "Bearer ${settings.token}")
                }
                httpClient.newCall(reqBuilder.build()).execute().use { /* ignore */ }
            } catch (e: Exception) {
                Log.w("ChatVM", "toggleScheduledTask failed: ${e.message}")
            }
        }
    }

    fun updateScheduledTask(taskId: String, prompt: String?, cronExpr: String?, runAt: String?) {
        sendExecutor.submit {
            try {
                val url = "http://${settings.host}:${settings.port}/api/schedule-task/$taskId"
                val json = JSONObject().apply {
                    if (prompt != null) put("prompt", prompt)
                    if (cronExpr != null) put("cron_expr", cronExpr)
                    if (runAt != null) put("run_at", runAt)
                }.toString()
                val body = json.toRequestBody("application/json".toMediaType())
                val reqBuilder = Request.Builder().url(url).put(body)
                if (settings.token.isNotBlank()) {
                    reqBuilder.header("Authorization", "Bearer ${settings.token}")
                }
                httpClient.newCall(reqBuilder.build()).execute().use { /* WS will refresh */ }
            } catch (e: Exception) {
                Log.w("ChatVM", "updateScheduledTask failed: ${e.message}")
            }
        }
        // Re-fetch to get server-computed next_run
        fetchScheduledTasks()
    }

    fun triggerScheduledTask(taskId: String) {
        sendExecutor.submit {
            try {
                val url = "http://${settings.host}:${settings.port}/api/schedule-task/$taskId/trigger"
                val body = "".toRequestBody("application/json".toMediaType())
                val reqBuilder = Request.Builder().url(url).post(body)
                if (settings.token.isNotBlank()) {
                    reqBuilder.header("Authorization", "Bearer ${settings.token}")
                }
                httpClient.newCall(reqBuilder.build()).execute().use { /* WS will update state */ }
            } catch (e: Exception) {
                Log.w("ChatVM", "triggerScheduledTask failed: ${e.message}")
            }
        }
    }

    fun deleteScheduledTask(taskId: String) {
        // Optimistic removal
        scheduledTasks.removeAll { it.id == taskId }
        sendExecutor.submit {
            try {
                val url = "http://${settings.host}:${settings.port}/api/schedule-task/$taskId"
                val reqBuilder = Request.Builder().url(url).delete()
                if (settings.token.isNotBlank()) {
                    reqBuilder.header("Authorization", "Bearer ${settings.token}")
                }
                httpClient.newCall(reqBuilder.build()).execute().use { /* ignore */ }
            } catch (e: Exception) {
                Log.w("ChatVM", "deleteScheduledTask failed: ${e.message}")
            }
        }
    }

    private fun sessionNotificationTag(session: String): String {
        val key = session.ifBlank { "default" }.trim()
        return "claudebot.session.$key"
    }

    private fun notifyIfBackgrounded(
        text: String,
        session: String,
        eventType: String,
        buttons: List<List<InlineButton>> = emptyList(),
        messageId: Int = 0,
        isReplay: Boolean = false,
    ) {
        if (isInForeground || isReplay) return
        if (!settings.shouldNotifyEvent(session, messageId, eventType)) return
        val app = getApplication<Application>()
        val tapIntent = Intent(app, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val tapPending = PendingIntent.getActivity(
            app, 0, tapIntent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val hasButtons = eventType == NOTIFY_EVENT_ACTIONABLE && buttons.flatten().isNotEmpty()
        // Show full text for actionable messages (plan approval etc), truncate otherwise
        val preview = if (hasButtons) text else if (text.length > 120) text.take(120) + "..." else text
        val title = if (hasButtons) "Action Required" else session.ifEmpty { "Claude Bot" }
        val sessionTag = sessionNotificationTag(session)

        val builder = Notification.Builder(app, MSG_CHANNEL_ID)
            .setContentTitle(title)
            .setContentText(if (hasButtons) session.ifEmpty { "Claude Bot" } else preview)
            .setStyle(Notification.BigTextStyle().bigText(preview))
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentIntent(tapPending)
            .setGroup(sessionTag)
            .setOnlyAlertOnce(true)
            .setAutoCancel(true)

        // Add button actions (flatten rows, max 3 per Android limit)
        var requestCode = 200
        for (btn in buttons.flatten().take(3)) {
            val actionIntent = Intent(app, NotificationActionReceiver::class.java).apply {
                action = NotificationActionReceiver.ACTION_CALLBACK
                putExtra(NotificationActionReceiver.EXTRA_CALLBACK_DATA, btn.callbackData)
                putExtra(NotificationActionReceiver.EXTRA_MESSAGE_ID, messageId)
                putExtra(NotificationActionReceiver.EXTRA_NOTIFICATION_ID, MSG_NOTIFICATION_ID)
                putExtra(NotificationActionReceiver.EXTRA_NOTIFICATION_TAG, sessionTag)
            }
            val actionPending = PendingIntent.getBroadcast(
                app, requestCode++, actionIntent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            builder.addAction(Notification.Action.Builder(null, btn.text, actionPending).build())
        }

        app.getSystemService(NotificationManager::class.java)
            .notify(sessionTag, MSG_NOTIFICATION_ID, builder.build())
    }

    /** No-op — foreground service removed to avoid ForegroundServiceDidNotStartInTimeException. */
    @Suppress("UNUSED_PARAMETER")
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
                    if (mid > 0 && mid in streamingMessageIds) return@post
                    // Skip if we already have this message in memory (replay dedup)
                    if (mid > 0 && findMessageIndex(mid) != null) return@post
                    // Skip if this replayed message is already persisted but not currently loaded.
                    if (mid > 0 && msg.isReplay && mid in persistedMessageIds) return@post
                    if (mid > 0 && mid in pendingIncomingMessageIds) return@post

                    val chatMsg = ChatMessage(
                        messageId = mid,
                        text = msg.text,
                        isFromBot = true,
                        session = msg.session,
                        isReplay = msg.isReplay,
                        timestamp = System.currentTimeMillis(),
                        buttons = msg.buttons
                    )

                    val showInUi = matchesSessionFilter(msg.session)
                    if (showInUi) {
                        val idx = messages.size
                        messages.add(chatMsg)
                        if (mid > 0) idToIndex[mid] = idx
                        scrollTrigger.value++
                    }
                    if (mid > 0) {
                        persistedMessageIds.add(mid)
                        pendingIncomingMessageIds.add(mid)
                    }
                    val actionable = msg.buttons.flatten().isNotEmpty()
                    if (actionable && showInUi) {
                        pendingAction.value = PendingAction(msg.text, msg.buttons, mid)
                        notifyIfBackgrounded(
                            text = msg.text,
                            session = msg.session,
                            eventType = NOTIFY_EVENT_ACTIONABLE,
                            buttons = msg.buttons,
                            messageId = mid,
                            isReplay = msg.isReplay
                        )
                    }
                    if (msg.session.isNotEmpty() && msg.session !in availableSessions) {
                        availableSessions.add(msg.session)
                    }
                    viewModelScope.launch(Dispatchers.IO) {
                        try {
                            if (mid > 0) {
                                dao.upsertByMessageId(chatMsg.toEntity())
                            } else {
                                dao.insert(chatMsg.toEntity())
                            }
                            if (showInUi) dbOffset++
                        } finally {
                            if (mid > 0) {
                                withContext(Dispatchers.Main) { pendingIncomingMessageIds.remove(mid) }
                            }
                        }
                    }
                }
                "status" -> {
                    // Only show busy/cancel for the session being viewed
                    val isCurrentSession = msg.session.isEmpty() || msg.session == effectiveSession
                    if (msg.mode == "busy") {
                        if (isCurrentSession) {
                            isBotBusy.value = msg.active
                        }
                        updateKeepAlive(msg.active)
                        // Refresh MC data so CLI runs (Claude/Codex/Gemini) appear
                        fetchActiveTasks()
                    } else {
                        if (isCurrentSession) {
                            taskStatus.value = TaskStatus(
                                mode = msg.mode,
                                phase = msg.phase,
                                step = msg.step,
                                active = msg.active
                            )
                            isBotBusy.value = msg.active
                        }
                        updateKeepAlive(msg.active)
                        // Update Mission Control map
                        val sessionName = msg.session
                        if (sessionName.isNotEmpty() && msg.mode in setOf("omni", "justdoit", "deepreview")) {
                            if (msg.active) {
                                val existing = activeTasks[sessionName]
                                activeTasks[sessionName] = ActiveTask(
                                    mode = msg.mode,
                                    session = sessionName,
                                    task = msg.task.ifEmpty { existing?.task ?: "" },
                                    phase = msg.phase,
                                    step = msg.step,
                                    started = if (msg.started > 0) msg.started else (existing?.started ?: 0),
                                    paused = msg.paused
                                )
                            } else {
                                activeTasks.remove(sessionName)
                            }
                        }
                    }
                }
                "active_session" -> {
                    if (msg.session.isNotEmpty()) {
                        currentSession.value = msg.session
                    }
                }
                "schedule" -> {
                    fetchScheduledTasks()
                }
                "edit" -> {
                    val mid = msg.messageId ?: return@post
                    // Skip legacy "edit" events for stream-tracked messages
                    if (mid in streamingMessageIds) return@post
                    val idx = findMessageIndex(mid)
                    if (idx != null) {
                        val updated = messages[idx].copy(
                            text = msg.text,
                            session = msg.session.ifEmpty { messages[idx].session },
                            isReplay = messages[idx].isReplay || msg.isReplay
                        )
                        messages[idx] = updated
                        if (idx == messages.lastIndex) scrollTrigger.value++
                        viewModelScope.launch(Dispatchers.IO) {
                            dao.updateByMessageId(mid, updated.text, updated.session, updated.buttonsToJson())
                        }
                    } else {
                        // Replayed edit for an older, non-loaded message: keep DB as source of truth.
                        if (msg.isReplay && mid in persistedMessageIds) return@post

                        val chatMsg = ChatMessage(
                            messageId = mid,
                            text = msg.text,
                            isFromBot = true,
                            session = msg.session,
                            isReplay = msg.isReplay,
                            timestamp = System.currentTimeMillis()
                        )
                        persistedMessageIds.add(mid)
                        val editShowInUi = matchesSessionFilter(msg.session)
                        if (editShowInUi) {
                            val newIdx = messages.size
                            messages.add(chatMsg)
                            idToIndex[mid] = newIdx
                            scrollTrigger.value++
                        }
                        viewModelScope.launch(Dispatchers.IO) {
                            dao.upsertByMessageId(chatMsg.toEntity())
                            if (editShowInUi) dbOffset++
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
                val existing = findMessageIndex(mid)
                if (existing != null) {
                    if (messages[existing].text.contains("———")) {
                        streamingMessageIds.remove(mid)
                        return  // Already finalized, skip
                    }
                    messages[existing] = messages[existing].copy(
                        text = "",
                        isReplay = messages[existing].isReplay || msg.isReplay
                    )
                    if (existing == messages.lastIndex) scrollTrigger.value++
                    return
                }

                // Already finalized from earlier replay/session; do not resurrect.
                if (msg.isReplay && mid in finalizedMessageIds) {
                    streamingMessageIds.remove(mid)
                    return
                }

                // New stream message
                val chatMsg = ChatMessage(
                    messageId = mid, text = "", isFromBot = true,
                    session = msg.session, isReplay = msg.isReplay, timestamp = System.currentTimeMillis()
                )
                persistedMessageIds.add(mid)
                if (msg.session.isNotEmpty() && msg.session !in availableSessions) {
                    availableSessions.add(msg.session)
                }
                if (matchesSessionFilter(msg.session)) {
                    val idx = messages.size
                    messages.add(chatMsg)
                    idToIndex[mid] = idx
                    scrollTrigger.value++
                }
                viewModelScope.launch(Dispatchers.IO) {
                    dao.upsertByMessageId(chatMsg.toEntity())
                }
                lastStreamPersist[mid] = System.currentTimeMillis()
            }
            "skip" -> {
                // Server tells us to ignore this message ID (TG continuation message)
                streamingMessageIds.add(mid)
            }
            "append" -> {
                // If we missed the "start" (e.g. reconnect mid-stream), create the message
                var idx = findMessageIndex(mid)
                if (mid !in streamingMessageIds) {
                    streamingMessageIds.add(mid)
                }
                if (idx == null) {
                    // Replayed append for a finalized message we already have in DB: ignore.
                    if (msg.isReplay && mid in finalizedMessageIds) return
                    // Filtered-out session: skip in-memory tracking
                    if (!matchesSessionFilter(msg.session)) return
                    val chatMsg = ChatMessage(
                        messageId = mid, text = "", isFromBot = true,
                        session = msg.session, isReplay = msg.isReplay, timestamp = System.currentTimeMillis()
                    )
                    val newIdx = messages.size
                    messages.add(chatMsg)
                    idToIndex[mid] = newIdx
                    persistedMessageIds.add(mid)
                    // Persist new entry immediately
                    viewModelScope.launch(Dispatchers.IO) { dao.upsertByMessageId(chatMsg.toEntity()) }
                    lastStreamPersist[mid] = System.currentTimeMillis()
                    idx = newIdx
                }
                val appendIdx = idx
                if (messages[appendIdx].text.contains("———")) {
                    // Terminal text is authoritative; ignore late append replay/update.
                    streamingMessageIds.remove(mid)
                    lastStreamPersist.remove(mid)
                    finalizedMessageIds.add(mid)
                    return
                }
                // Strip tool status line if present before appending text
                var currentText = messages[appendIdx].text
                if (currentText.endsWith("\u200B")) {
                    // Remove the tool status line (everything from last newline before marker)
                    val lastNl = currentText.lastIndexOf('\n', currentText.length - 2)
                    currentText = if (lastNl >= 0) currentText.substring(0, lastNl) else ""
                }
                val newText = currentText + msg.text
                messages[appendIdx] = messages[appendIdx].copy(
                    text = newText,
                    isReplay = messages[appendIdx].isReplay || msg.isReplay
                )
                if (appendIdx == messages.lastIndex) scrollTrigger.value++
                // Debounced DB persist — save accumulated text every 2s
                val now = System.currentTimeMillis()
                val lastPersist = lastStreamPersist[mid] ?: 0L
                if (now - lastPersist >= STREAM_PERSIST_INTERVAL_MS) {
                    lastStreamPersist[mid] = now
                    val session = messages[appendIdx].session
                    val timestamp = messages[appendIdx].timestamp
                    viewModelScope.launch(Dispatchers.IO) {
                        dao.upsertByMessageId(
                            MessageEntity(
                                messageId = mid, text = newText, isFromBot = true,
                                session = session, timestamp = timestamp
                            )
                        )
                    }
                }
            }
            "tool" -> {
                val idx = findMessageIndex(mid)
                if (idx != null) {
                    var currentText = messages[idx].text
                    // Remove existing tool status line if present
                    if (currentText.endsWith("\u200B")) {
                        val lastNl = currentText.lastIndexOf('\n', currentText.length - 2)
                        currentText = if (lastNl >= 0) currentText.substring(0, lastNl) else ""
                    }
                    // Append tool status as a replaceable line (marked with zero-width space)
                    val toolLine = formatToolStatus(msg.tool, msg.path)
                    messages[idx] = messages[idx].copy(text = currentText + "\n" + toolLine + "\u200B")
                    if (idx == messages.lastIndex) scrollTrigger.value++
                }
            }
            "done" -> {
                streamingMessageIds.remove(mid)
                lastStreamPersist.remove(mid)
                val doneExisting = findMessageIndex(mid)
                // Skip if already finalized in memory (replay of completed message)
                if (doneExisting != null && messages[doneExisting].text.contains("———")) return
                val parsedChanges = msg.fileChanges.map { fc ->
                    FileChange(
                        type = fc["type"] ?: "",
                        path = fc["path"] ?: "",
                        old = fc["old"] ?: "",
                        new = fc["new"] ?: "",
                        content = fc["content"] ?: ""
                    )
                }
                val finalText = buildString {
                    append(msg.text)
                    append("\n\n———\n")
                    append(if (msg.cancelled) "⚠️ _cancelled_" else "✓ _complete_")
                }

                val idx = doneExisting
                if (idx != null) {
                    val updated = messages[idx].copy(
                        text = finalText,
                        session = msg.session.ifEmpty { messages[idx].session },
                        isReplay = messages[idx].isReplay || msg.isReplay,
                        fileChanges = parsedChanges
                    )
                    messages[idx] = updated
                    if (idx == messages.lastIndex) scrollTrigger.value++
                    notifyIfBackgrounded(
                        text = finalText,
                        session = updated.session,
                        eventType = NOTIFY_EVENT_TERMINAL,
                        messageId = mid,
                        isReplay = msg.isReplay
                    )
                    persistedMessageIds.add(mid)
                    finalizedMessageIds.add(mid)
                    viewModelScope.launch(Dispatchers.IO) {
                        dao.upsertByMessageId(updated.toEntity())
                    }
                } else {
                    // Replayed terminal event for an already finalized, non-loaded message.
                    if (msg.isReplay && mid in finalizedMessageIds) return

                    val chatMsg = ChatMessage(
                        messageId = mid,
                        text = finalText,
                        isFromBot = true,
                        session = msg.session,
                        isReplay = msg.isReplay,
                        timestamp = System.currentTimeMillis(),
                        fileChanges = parsedChanges
                    )
                    persistedMessageIds.add(mid)
                    finalizedMessageIds.add(mid)
                    if (msg.session.isNotEmpty() && msg.session !in availableSessions) {
                        availableSessions.add(msg.session)
                    }
                    if (matchesSessionFilter(msg.session)) {
                        val newIdx = messages.size
                        messages.add(chatMsg)
                        idToIndex[mid] = newIdx
                        scrollTrigger.value++
                    }
                    notifyIfBackgrounded(
                        text = finalText,
                        session = chatMsg.session,
                        eventType = NOTIFY_EVENT_TERMINAL,
                        messageId = mid,
                        isReplay = msg.isReplay
                    )
                    viewModelScope.launch(Dispatchers.IO) {
                        dao.upsertByMessageId(chatMsg.toEntity())
                    }
                }
            }
        }
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

    /** Validate fast map lookup and recover from stale indexes by linear fallback. */
    private fun findMessageIndex(messageId: Int): Int? {
        if (messageId == 0) return null
        val idx = idToIndex[messageId]
        if (idx != null && idx < messages.size && messages[idx].messageId == messageId) {
            return idx
        }
        val fallback = messages.indexOfLast { it.messageId == messageId }
        if (fallback >= 0) {
            idToIndex[messageId] = fallback
            return fallback
        }
        idToIndex.remove(messageId)
        return null
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
