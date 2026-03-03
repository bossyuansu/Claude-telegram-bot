package com.claudebot.app.data

import android.content.Context
import android.content.SharedPreferences

class SettingsRepository(context: Context) {

    private val prefs: SharedPreferences =
        context.getSharedPreferences("claude_bot_settings", Context.MODE_PRIVATE)
    private val wsStateLock = Any()
    private val notificationLock = Any()

    var host: String
        get() = prefs.getString("host", "100.118.238.103") ?: "100.118.238.103"
        set(value) = prefs.edit().putString("host", value).apply()

    var port: Int
        get() = prefs.getInt("port", 8642)
        set(value) = prefs.edit().putInt("port", value).apply()

    var token: String
        get() = prefs.getString("token", "") ?: ""
        set(value) = prefs.edit().putString("token", value).apply()

    var wsLastSyncAt: Long
        get() = prefs.getLong("ws_last_sync_at", 0L)
        set(value) = prefs.edit().putLong("ws_last_sync_at", value).apply()

    var wsLastError: String
        get() = prefs.getString("ws_last_error", "") ?: ""
        set(value) = prefs.edit().putString("ws_last_error", value).apply()

    var wsLastState: String
        get() = prefs.getString("ws_last_state", "DISCONNECTED") ?: "DISCONNECTED"
        set(value) = prefs.edit().putString("ws_last_state", value).apply()

    var lastSeq: Int
        get() = prefs.getInt("last_seq", 0)
        set(value) {
            synchronized(wsStateLock) {
                val current = prefs.getInt("last_seq", 0)
                val next = maxOf(current, value)
                if (next != current) {
                    prefs.edit().putInt("last_seq", next).commit()
                }
            }
        }

    var knownServerId: String
        get() = prefs.getString("known_server_id", "") ?: ""
        set(value) {
            synchronized(wsStateLock) {
                val current = prefs.getString("known_server_id", "") ?: ""
                if (current != value) {
                    prefs.edit().putString("known_server_id", value).commit()
                }
            }
        }

    fun getWsState(): Pair<Int, String> {
        synchronized(wsStateLock) {
            val seq = prefs.getInt("last_seq", 0)
            val serverId = prefs.getString("known_server_id", "") ?: ""
            return seq to serverId
        }
    }

    fun updateWsState(seq: Int, serverId: String) {
        synchronized(wsStateLock) {
            val currentSeq = prefs.getInt("last_seq", 0)
            val currentServerId = prefs.getString("known_server_id", "") ?: ""
            val serverChanged = serverId.isNotBlank() && currentServerId != serverId
            // Seq monotonicity is per server instance. If server_id changed,
            // reset baseline to the new server's sequence space.
            val nextSeq = if (serverChanged) maxOf(0, seq) else maxOf(currentSeq, seq)
            val now = System.currentTimeMillis()
            if (nextSeq == currentSeq && currentServerId == serverId) {
                prefs.edit().putLong("ws_last_sync_at", now).apply()
                return
            }
            prefs.edit()
                .putInt("last_seq", nextSeq)
                .putString("known_server_id", serverId)
                .putLong("ws_last_sync_at", now)
                .commit()
        }
    }

    /**
     * Returns true only for the first notification of (eventType, session, messageId).
     * Used to suppress replay/duplicate notifications across WS + background sync paths.
     */
    fun shouldNotifyEvent(session: String, messageId: Int, eventType: String): Boolean {
        if (messageId == 0) return true
        val sessionKey = session.ifBlank { "default" }
        val dedupeKey = "$eventType|$sessionKey|$messageId"
        synchronized(notificationLock) {
            val maxKeys = 1000
            val current = (prefs.getStringSet("notified_event_keys", emptySet()) ?: emptySet()).toMutableSet()
            if (dedupeKey in current) return false
            if (current.size >= maxKeys) current.clear()
            current.add(dedupeKey)
            prefs.edit().putStringSet("notified_event_keys", current).apply()
            return true
        }
    }

    val isConfigured: Boolean
        get() = host.isNotBlank()

    fun wsUrl(): String {
        val base = "ws://$host:$port/ws"
        return if (token.isNotBlank()) "$base?token=$token" else base
    }
}
