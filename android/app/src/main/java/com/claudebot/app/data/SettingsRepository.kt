package com.claudebot.app.data

import android.content.Context
import android.content.SharedPreferences

class SettingsRepository(context: Context) {

    private val prefs: SharedPreferences =
        context.getSharedPreferences("claude_bot_settings", Context.MODE_PRIVATE)

    var host: String
        get() = prefs.getString("host", "100.118.238.103") ?: "100.118.238.103"
        set(value) = prefs.edit().putString("host", value).apply()

    var port: Int
        get() = prefs.getInt("port", 8642)
        set(value) = prefs.edit().putInt("port", value).apply()

    var chatId: String
        get() = prefs.getString("chat_id", "") ?: ""
        set(value) = prefs.edit().putString("chat_id", value).apply()

    var token: String
        get() = prefs.getString("token", "") ?: ""
        set(value) = prefs.edit().putString("token", value).apply()

    val isConfigured: Boolean
        get() = host.isNotBlank() && chatId.isNotBlank()

    fun wsUrl(): String {
        val base = "ws://$host:$port/ws/$chatId"
        return if (token.isNotBlank()) "$base?token=$token" else base
    }
}
