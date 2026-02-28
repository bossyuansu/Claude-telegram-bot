package com.claudebot.app.network

import com.claudebot.app.data.InlineButton
import okhttp3.*
import org.json.JSONObject
import java.util.TreeMap
import java.util.concurrent.TimeUnit

enum class ConnectionState { DISCONNECTED, CONNECTING, CONNECTED, RECONNECTING }

data class WsMessage(
    val type: String,        // "message", "edit", "error", "status"
    val messageId: Int?,
    val text: String,
    val session: String,
    val seq: Int = 0,
    val buttons: List<List<InlineButton>> = emptyList(),
    // Status fields (for type="status")
    val mode: String = "",
    val phase: String = "",
    val step: Int = 0,
    val active: Boolean = false
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
    private var baseUrl: String = ""
    private var shouldReconnect = false
    private var reconnectAttempt = 0
    private var reconnectThread: Thread? = null

    /** Last delivered sequence number — sent on reconnect so server replays missed messages. */
    @Volatile var lastSeq: Int = 0
        private set

    /** Next expected seq. Messages with seq < this are dupes; seq > this are buffered. */
    private var expectedSeq: Int = 1

    /** Out-of-order messages waiting for the gap to be filled. */
    private val pendingBuffer = TreeMap<Int, WsMessage>()

    /** Whether we've already requested a resend for the current gap. */
    private var resendRequested = false

    fun connect(wsUrl: String) {
        baseUrl = wsUrl
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
        pendingBuffer.clear()
        resendRequested = false
        onStateChange(ConnectionState.DISCONNECTED)
    }

    fun send(text: String) {
        val json = JSONObject().put("text", text).toString()
        ws?.send(json)
    }

    private fun doConnect() {
        if (!shouldReconnect) return
        onStateChange(if (reconnectAttempt == 0) ConnectionState.CONNECTING else ConnectionState.RECONNECTING)

        // Append last_seq to URL so server replays missed messages
        val connectUrl = if (lastSeq > 0) {
            val sep = if (baseUrl.contains("?")) "&" else "?"
            "${baseUrl}${sep}last_seq=$lastSeq"
        } else {
            baseUrl
        }

        val request = Request.Builder().url(connectUrl).build()
        ws = client.newWebSocket(request, object : WebSocketListener() {

            override fun onOpen(webSocket: WebSocket, response: Response) {
                reconnectAttempt = 0
                pendingBuffer.clear()
                resendRequested = false
                // Align expectedSeq with what we've already delivered
                expectedSeq = lastSeq + 1
                onStateChange(ConnectionState.CONNECTED)
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                try {
                    val json = JSONObject(text)
                    val seq = json.optInt("seq", 0)

                    val msg = parseMessage(json, seq)

                    if (seq == 0) {
                        // No seq (e.g. error frames) — deliver immediately
                        onMessage(msg)
                        return
                    }

                    when {
                        seq < expectedSeq -> {
                            // Already delivered — discard duplicate
                        }
                        seq == expectedSeq -> {
                            // In order — deliver and flush any consecutive buffered messages
                            deliver(msg)
                            flushPending()
                        }
                        else -> {
                            // Out of order — buffer and request resend for the gap
                            pendingBuffer[seq] = msg
                            if (!resendRequested) {
                                resendRequested = true
                                val resendReq = JSONObject()
                                    .put("type", "resend")
                                    .put("from_seq", expectedSeq)
                                    .toString()
                                webSocket.send(resendReq)
                            }
                        }
                    }
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

    /** Deliver a message and advance the expected seq. */
    private fun deliver(msg: WsMessage) {
        lastSeq = msg.seq
        expectedSeq = msg.seq + 1
        onMessage(msg)
    }

    /** Flush consecutive messages from the pending buffer. */
    private fun flushPending() {
        while (pendingBuffer.containsKey(expectedSeq)) {
            val msg = pendingBuffer.remove(expectedSeq)!!
            deliver(msg)
        }
        if (pendingBuffer.isEmpty()) {
            resendRequested = false
        }
    }

    private fun parseMessage(json: JSONObject, seq: Int): WsMessage {
        val buttons = mutableListOf<List<InlineButton>>()
        val markup = json.optJSONObject("reply_markup")
        if (markup != null) {
            val keyboard = markup.optJSONArray("inline_keyboard")
            if (keyboard != null) {
                for (r in 0 until keyboard.length()) {
                    val row = keyboard.getJSONArray(r)
                    val rowButtons = mutableListOf<InlineButton>()
                    for (c in 0 until row.length()) {
                        val btn = row.getJSONObject(c)
                        rowButtons.add(InlineButton(
                            text = btn.optString("text", ""),
                            callbackData = btn.optString("callback_data", "")
                        ))
                    }
                    buttons.add(rowButtons)
                }
            }
        }
        return WsMessage(
            type = json.optString("type", ""),
            messageId = if (json.has("message_id") && !json.isNull("message_id"))
                json.getInt("message_id") else null,
            text = json.optString("text", ""),
            session = json.optString("session", ""),
            seq = seq,
            buttons = buttons,
            mode = json.optString("mode", ""),
            phase = json.optString("phase", ""),
            step = json.optInt("step", 0),
            active = json.optBoolean("active", false)
        )
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
