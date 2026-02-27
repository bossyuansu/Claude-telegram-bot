package com.claudebot.app.network

import okhttp3.*
import org.json.JSONObject
import java.util.concurrent.TimeUnit

enum class ConnectionState { DISCONNECTED, CONNECTING, CONNECTED, RECONNECTING }

data class WsMessage(
    val type: String,        // "message", "edit", "error"
    val messageId: Int?,
    val text: String,
    val session: String
)

class WebSocketManager(
    private val onMessage: (WsMessage) -> Unit,
    private val onStateChange: (ConnectionState) -> Unit
) {
    private val client = OkHttpClient.Builder()
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .pingInterval(30, TimeUnit.SECONDS)
        .build()

    private var ws: WebSocket? = null
    private var url: String = ""
    private var shouldReconnect = false
    private var reconnectAttempt = 0
    private var reconnectThread: Thread? = null

    fun connect(wsUrl: String) {
        url = wsUrl
        shouldReconnect = true
        reconnectAttempt = 0
        doConnect()
    }

    fun disconnect() {
        shouldReconnect = false
        reconnectThread?.interrupt()
        reconnectThread = null
        ws?.close(1000, "User disconnect")
        ws = null
        onStateChange(ConnectionState.DISCONNECTED)
    }

    fun send(text: String) {
        val json = JSONObject().put("text", text).toString()
        ws?.send(json)
    }

    private fun doConnect() {
        if (!shouldReconnect) return
        onStateChange(if (reconnectAttempt == 0) ConnectionState.CONNECTING else ConnectionState.RECONNECTING)

        val request = Request.Builder().url(url).build()
        ws = client.newWebSocket(request, object : WebSocketListener() {

            override fun onOpen(webSocket: WebSocket, response: Response) {
                reconnectAttempt = 0
                onStateChange(ConnectionState.CONNECTED)
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                try {
                    val json = JSONObject(text)
                    val msg = WsMessage(
                        type = json.optString("type", ""),
                        messageId = if (json.has("message_id") && !json.isNull("message_id"))
                            json.getInt("message_id") else null,
                        text = json.optString("text", ""),
                        session = json.optString("session", "")
                    )
                    onMessage(msg)
                } catch (_: Exception) {}
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                scheduleReconnect()
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                if (code != 1000) scheduleReconnect()
                else onStateChange(ConnectionState.DISCONNECTED)
            }
        })
    }

    private fun scheduleReconnect() {
        if (!shouldReconnect) {
            onStateChange(ConnectionState.DISCONNECTED)
            return
        }
        onStateChange(ConnectionState.RECONNECTING)
        reconnectAttempt++
        val delay = minOf(1000L * (1L shl minOf(reconnectAttempt, 5)), 30_000L)

        reconnectThread = Thread {
            try {
                Thread.sleep(delay)
                doConnect()
            } catch (_: InterruptedException) {}
        }.also { it.isDaemon = true; it.start() }
    }
}
