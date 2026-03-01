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
import org.json.JSONObject
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
        private const val MSG_CHANNEL_ID = "bot_messages"
        private const val SYNC_NOTIFICATION_ID = 101
        private const val CONNECT_TIMEOUT_MS = 15_000L
    }

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
        var ws: WebSocket? = null

        val request = Request.Builder().url(connectUrl).build()
        ws = client.newWebSocket(request, object : WebSocketListener() {
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

                    // Only collect full messages (not stream fragments)
                    if (type == "message" || type == "edit") {
                        collected.add(json)
                    }
                } catch (_: Exception) {}
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
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

        // Save collected messages to DB
        var newMessageCount = 0
        for (json in collected) {
            val type = json.optString("type", "")
            val messageId = if (json.has("message_id") && !json.isNull("message_id"))
                json.getInt("message_id") else 0
            val text = json.optString("text", "")
            val session = json.optString("session", "")

            if (type == "edit" && messageId != 0) {
                // Update existing message
                dao.updateByMessageId(messageId, text, session, "")
            } else if (type == "message") {
                // Check if we already have this message
                val existing = if (messageId != 0) dao.findByMessageId(messageId) else null
                if (existing == null) {
                    dao.insert(
                        MessageEntity(
                            messageId = messageId,
                            text = text,
                            isFromBot = true,
                            session = session
                        )
                    )
                    newMessageCount++
                }
            }
        }

        // Persist updated seq
        if (newLastSeq > lastSeq || newServerId != knownServerId) {
            settings.lastSeq = newLastSeq
            settings.knownServerId = newServerId
        }

        // Show notification if we got new messages
        if (newMessageCount > 0) {
            showNotification(newMessageCount, collected.lastOrNull())
        }

        return Result.success()
    }

    private fun showNotification(count: Int, lastMsg: JSONObject?) {
        val intent = Intent(applicationContext, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val pending = PendingIntent.getActivity(
            applicationContext, 0, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val session = lastMsg?.optString("session", "") ?: ""
        val preview = if (count == 1) {
            val text = lastMsg?.optString("text", "") ?: ""
            if (text.length > 120) text.take(120) + "..." else text
        } else {
            "$count new messages"
        }
        val notification = Notification.Builder(applicationContext, MSG_CHANNEL_ID)
            .setContentTitle(session.ifEmpty { "Claude Bot" })
            .setContentText(preview)
            .setStyle(Notification.BigTextStyle().bigText(preview))
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentIntent(pending)
            .setAutoCancel(true)
            .build()
        applicationContext.getSystemService(NotificationManager::class.java)
            .notify(SYNC_NOTIFICATION_ID, notification)
    }
}
