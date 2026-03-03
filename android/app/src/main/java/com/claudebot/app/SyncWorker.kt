package com.claudebot.app

import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.claudebot.app.data.AppDatabase
import com.claudebot.app.data.MessageEntity
import com.claudebot.app.data.SettingsRepository
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.*
import org.json.JSONArray
import org.json.JSONObject
import kotlin.math.abs
import java.util.concurrent.TimeUnit

/**
 * Periodic background worker that briefly connects to the WS server,
 * receives any buffered messages missed while the app was inactive,
 * saves them to the local Room DB, and shows a notification.
 */
class SyncWorker(
    context: Context,
    params: WorkerParameters
) : CoroutineWorker(context, params) {

    companion object {
        const val WORK_NAME = "ws_sync"
        const val IMMEDIATE_WORK_NAME = "ws_sync_immediate"
        private const val MSG_CHANNEL_ID = "bot_messages"
        private const val SYNC_NOTIFICATION_ID = 101
        private const val CONNECT_TIMEOUT_MS = 15_000L
        private const val NOTIFY_EVENT_ACTIONABLE = "actionable"
        private const val NOTIFY_EVENT_TERMINAL = "terminal"
    }

    private data class ActionButton(val text: String, val callbackData: String)
    private data class NotificationCandidate(
        val session: String,
        val messageId: Int,
        val text: String,
        val eventType: String,
        val buttons: List<ActionButton> = emptyList()
    )

    override suspend fun doWork(): Result {
        val settings = SettingsRepository(applicationContext)
        if (!settings.isConfigured) return Result.success()

        val lastSeq = settings.lastSeq
        val knownServerId = settings.knownServerId
        val dao = AppDatabase.get(applicationContext).messageDao()

        val client = OkHttpClient.Builder()
            .readTimeout(CONNECT_TIMEOUT_MS, TimeUnit.MILLISECONDS)
            .pingInterval(10, TimeUnit.SECONDS)
            .build()

        // Build WS URL with last_seq
        val baseUrl = settings.wsUrl()
        val connectUrl = if (lastSeq > 0) {
            val sep = if (baseUrl.contains("?")) "&" else "?"
            "${baseUrl}${sep}last_seq=$lastSeq"
        } else {
            baseUrl
        }

        val collected = mutableListOf<JSONObject>()
        val done = CompletableDeferred<Unit>()
        var newLastSeq = lastSeq
        var newServerId = knownServerId
        var syncError = ""

        val request = Request.Builder().url(connectUrl).build()
        val ws = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                // Connection opened — server will send server_hello then replay
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                try {
                    val json = JSONObject(text)
                    val type = json.optString("type", "")

                    if (type == "server_hello") {
                        val serverId = json.optString("server_id", "")
                        if (serverId != knownServerId) {
                            // Server restarted — accept new numbering
                            newServerId = serverId
                            newLastSeq = 0
                        }
                        return
                    }

                    val seq = json.optInt("seq", 0)
                    if (seq > newLastSeq) {
                        newLastSeq = seq
                    }

                    // Collect replay frames we can persist and/or notify from.
                    if (type == "message" || type == "edit" || (type == "stream" && json.optString("op") == "done")) {
                        collected.add(json)
                    }
                } catch (_: Exception) {}
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                syncError = t.message ?: "WebSocket failure"
                done.complete(Unit)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                done.complete(Unit)
            }
        })

        // Wait for messages to arrive, then close after a brief window
        // The server sends replay immediately on connect, so 5s is plenty
        withTimeoutOrNull(CONNECT_TIMEOUT_MS) {
            // Give the server time to replay, then close
            kotlinx.coroutines.delay(5_000)
        }
        ws.close(1000, "sync done")
        // Wait for close confirmation briefly
        withTimeoutOrNull(2_000) { done.await() }
        client.dispatcher.executorService.shutdown()

        // Save collected messages to DB and accumulate notification candidates.
        val notificationsBySession = linkedMapOf<String, NotificationCandidate>()
        for (json in collected) {
            val type = json.optString("type", "")
            val messageId = if (json.has("message_id") && !json.isNull("message_id"))
                json.getInt("message_id") else 0
            val session = json.optString("session", "")

            if (type == "edit" && messageId > 0) {
                val text = json.optString("text", "")
                // Preserve existing button payload on edit.
                val existing = dao.findByMessageId(messageId)
                val buttons = existing?.buttons ?: ""
                dao.updateByMessageId(messageId, text, session, buttons)
            } else if (type == "message") {
                val text = json.optString("text", "")
                val buttonsJson = extractButtonsJson(json)
                if (messageId > 0) {
                    dao.upsertByMessageId(
                        MessageEntity(
                            messageId = messageId,
                            text = text,
                            isFromBot = true,
                            session = session,
                            buttons = buttonsJson
                        )
                    )
                } else {
                    dao.insert(
                        MessageEntity(
                            messageId = messageId,
                            text = text,
                            isFromBot = true,
                            session = session,
                            buttons = buttonsJson
                        )
                    )
                }

                val actions = extractButtons(json)
                if (actions.isNotEmpty() && messageId > 0) {
                    val sessionKey = session.ifBlank { "default" }
                    notificationsBySession[sessionKey] = NotificationCandidate(
                        session = session,
                        messageId = messageId,
                        text = text,
                        eventType = NOTIFY_EVENT_ACTIONABLE,
                        buttons = actions
                    )
                }
            } else if (type == "stream" && json.optString("op") == "done") {
                val finalText = buildTerminalText(json)
                if (messageId > 0) {
                    dao.upsertByMessageId(
                        MessageEntity(
                            messageId = messageId,
                            text = finalText,
                            isFromBot = true,
                            session = session
                        )
                    )
                }
                if (messageId > 0) {
                    val sessionKey = session.ifBlank { "default" }
                    notificationsBySession[sessionKey] = NotificationCandidate(
                        session = session,
                        messageId = messageId,
                        text = finalText,
                        eventType = NOTIFY_EVENT_TERMINAL
                    )
                }
            }
        }
        dao.deleteDuplicateMessageIds()

        // Persist updated seq
        if (newLastSeq > lastSeq || newServerId != knownServerId) {
            settings.updateWsState(newLastSeq, newServerId)
        } else {
            // Successful sync window even if no new messages.
            settings.wsLastSyncAt = System.currentTimeMillis()
        }

        settings.wsLastError = syncError

        // Notify only actionable or terminal events, grouped by session + deduped by message ID.
        for (candidate in notificationsBySession.values) {
            showNotification(candidate, settings)
        }
        return if (syncError.isNotBlank()) Result.retry() else Result.success()
    }

    private fun showNotification(candidate: NotificationCandidate, settings: SettingsRepository) {
        if (!settings.shouldNotifyEvent(candidate.session, candidate.messageId, candidate.eventType)) return

        val intent = Intent(applicationContext, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val pending = PendingIntent.getActivity(
            applicationContext, 0, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val preview = if (candidate.text.length > 180) candidate.text.take(180) + "..." else candidate.text
        val sessionTag = sessionNotificationTag(candidate.session)
        val title = if (candidate.eventType == NOTIFY_EVENT_ACTIONABLE) "Action Required" else candidate.session.ifEmpty { "Claude Bot" }

        val builder = Notification.Builder(applicationContext, MSG_CHANNEL_ID)
            .setContentTitle(title)
            .setContentText(if (candidate.eventType == NOTIFY_EVENT_ACTIONABLE) candidate.session.ifEmpty { "Claude Bot" } else preview)
            .setStyle(Notification.BigTextStyle().bigText(preview))
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentIntent(pending)
            .setGroup(sessionTag)
            .setOnlyAlertOnce(true)
            .setAutoCancel(true)

        if (candidate.buttons.isNotEmpty()) {
            val baseCode = abs((candidate.messageId * 31) + sessionTag.hashCode())
            candidate.buttons.take(3).forEachIndexed { index, btn ->
                val actionIntent = Intent(applicationContext, NotificationActionReceiver::class.java).apply {
                    action = NotificationActionReceiver.ACTION_CALLBACK
                    putExtra(NotificationActionReceiver.EXTRA_CALLBACK_DATA, btn.callbackData)
                    putExtra(NotificationActionReceiver.EXTRA_MESSAGE_ID, candidate.messageId)
                    putExtra(NotificationActionReceiver.EXTRA_NOTIFICATION_ID, SYNC_NOTIFICATION_ID)
                    putExtra(NotificationActionReceiver.EXTRA_NOTIFICATION_TAG, sessionTag)
                }
                val actionPending = PendingIntent.getBroadcast(
                    applicationContext,
                    baseCode + index,
                    actionIntent,
                    PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
                )
                builder.addAction(Notification.Action.Builder(null, btn.text, actionPending).build())
            }
        }

        applicationContext.getSystemService(NotificationManager::class.java)
            .notify(sessionTag, SYNC_NOTIFICATION_ID, builder.build())
    }

    private fun sessionNotificationTag(session: String): String {
        val key = session.ifBlank { "default" }.trim()
        return "claudebot.session.$key"
    }

    private fun buildTerminalText(event: JSONObject): String {
        val text = event.optString("text", "")
        val cancelled = event.optBoolean("cancelled", false)
        return buildString {
            append(text)
            append("\n\n———\n")
            append(if (cancelled) "⚠️ _cancelled_" else "✓ _complete_")
        }
    }

    private fun extractButtons(message: JSONObject): List<ActionButton> {
        val markup = message.optJSONObject("reply_markup") ?: return emptyList()
        val keyboard = markup.optJSONArray("inline_keyboard") ?: return emptyList()
        val actions = mutableListOf<ActionButton>()
        for (r in 0 until keyboard.length()) {
            val row = keyboard.optJSONArray(r) ?: continue
            for (c in 0 until row.length()) {
                val btn = row.optJSONObject(c) ?: continue
                val text = btn.optString("text", "")
                val data = btn.optString("callback_data", "")
                if (text.isNotBlank() && data.isNotBlank()) {
                    actions.add(ActionButton(text, data))
                }
            }
        }
        return actions
    }

    private fun extractButtonsJson(message: JSONObject): String {
        val markup = message.optJSONObject("reply_markup") ?: return ""
        val keyboard = markup.optJSONArray("inline_keyboard") ?: return ""
        val out = JSONArray()
        for (r in 0 until keyboard.length()) {
            val row = keyboard.optJSONArray(r) ?: continue
            val rowOut = JSONArray()
            for (c in 0 until row.length()) {
                val btn = row.optJSONObject(c) ?: continue
                rowOut.put(
                    JSONObject()
                        .put("text", btn.optString("text", ""))
                        .put("data", btn.optString("callback_data", ""))
                )
            }
            if (rowOut.length() > 0) out.put(rowOut)
        }
        return if (out.length() > 0) out.toString() else ""
    }
}
