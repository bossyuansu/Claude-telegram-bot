package com.claudebot.app

import android.app.NotificationManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

class NotificationActionReceiver : BroadcastReceiver() {

    companion object {
        const val ACTION_CALLBACK = "com.claudebot.app.ACTION_CALLBACK"
        const val ACTION_CANCEL = "com.claudebot.app.ACTION_CANCEL"
        const val EXTRA_CALLBACK_DATA = "callback_data"
        const val EXTRA_MESSAGE_ID = "message_id"
        const val EXTRA_NOTIFICATION_ID = "notification_id"
        const val EXTRA_NOTIFICATION_TAG = "notification_tag"
    }

    private val httpClient = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(5, TimeUnit.SECONDS)
        .build()

    override fun onReceive(context: Context, intent: Intent) {
        val notificationId = intent.getIntExtra(EXTRA_NOTIFICATION_ID, 100)
        val notificationTag = intent.getStringExtra(EXTRA_NOTIFICATION_TAG)
        val nm = context.getSystemService(NotificationManager::class.java)
        if (notificationTag.isNullOrBlank()) nm.cancel(notificationId) else nm.cancel(notificationTag, notificationId)

        val prefs = context.getSharedPreferences("claude_bot_settings", Context.MODE_PRIVATE)
        val host = prefs.getString("host", "") ?: ""
        val port = prefs.getInt("port", 8642)
        val token = prefs.getString("token", "") ?: ""
        if (host.isBlank()) return

        val pendingResult = goAsync()
        Thread {
            try {
                when (intent.action) {
                    ACTION_CALLBACK -> {
                        val data = intent.getStringExtra(EXTRA_CALLBACK_DATA) ?: return@Thread
                        val messageId = intent.getIntExtra(EXTRA_MESSAGE_ID, 0)
                        val json = JSONObject()
                            .put("data", data)
                            .put("message_id", messageId)
                            .put("chat_id", 0)
                            .toString()
                        post("http://$host:$port/api/callback", json, token)
                    }
                    ACTION_CANCEL -> {
                        val json = JSONObject().put("text", "/cancel").toString()
                        post("http://$host:$port/api/message", json, token)
                    }
                }
            } catch (_: Exception) {
            } finally {
                pendingResult.finish()
            }
        }.start()
    }

    private fun post(url: String, json: String, token: String) {
        val body = json.toRequestBody("application/json".toMediaType())
        val req = Request.Builder().url(url).post(body)
        if (token.isNotBlank()) req.header("Authorization", "Bearer $token")
        httpClient.newCall(req.build()).execute().close()
    }
}
