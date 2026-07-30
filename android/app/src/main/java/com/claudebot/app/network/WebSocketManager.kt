package com.claudebot.app.network

import com.claudebot.app.data.InlineButton
import okhttp3.*
import org.json.JSONObject
import android.util.Log
import java.util.TreeMap
import java.util.concurrent.TimeUnit

enum class ConnectionState { DISCONNECTED, CONNECTING, CONNECTED, RECONNECTING }

data class WsMessage(
    val type: String,        // "message", "edit", "error", "status", "stream"
    val messageId: Int?,
    val text: String,
    val session: String,
    val seq: Int = 0,
    val isReplay: Boolean = false,
    val createdAt: Long = 0L,   // epoch ms when the message was created server-side (not received)
    val buttons: List<List<InlineButton>> = emptyList(),
    // Status fields (for type="status")
    val mode: String = "",
    val phase: String = "",
    val step: Int = 0,
    val active: Boolean = false,
    val task: String = "",       // task description (for Mission Control)
    val started: Long = 0,      // epoch seconds when task started
    val paused: Boolean = false, // whether the task is paused
    // Stream fields (for type="stream")
    val op: String = "",           // "start", "append", "tool", "done"
    val tool: String = "",         // tool name for op="tool"
    val path: String = "",         // tool path for op="tool"
    val cancelled: Boolean = false,
    val fileChanges: List<Map<String, String>> = emptyList(),
    // File fields (for type="file")
    val fileName: String = "",
    val fileSize: Long = 0,
    val mimeType: String = "",
    val isImage: Boolean = false,
    val downloadPath: String = "",
    // Goal fields (for type="goal")
    val goalEvent: String = "",    // started, milestone_started, milestone_completed, iteration, replan, completed, failed, paused, cancelled, escalation
    val goalId: String = "",
    val goalTitle: String = "",
    val milestonesTotal: Int = 0,
    val milestonesDone: Int = 0,
    val milestoneTitle: String = "",
    val iteration: Int = 0,
    val outcome: String = "",      // success, failure (for iteration events)
    val reason: String = "",       // pause/cancel reason
)

/** Server-side creation time (epoch ms), falling back to now for legacy payloads lacking created_at. */
val WsMessage.createdOrNow: Long
    get() = if (createdAt > 0L) createdAt else System.currentTimeMillis()

class WebSocketManager(
    private val onMessage: (WsMessage) -> Unit,
    private val onStateChange: (ConnectionState) -> Unit,
    private val onServerRestart: (() -> Unit)? = null,
    private val onSeqUpdate: ((seq: Int, serverId: String) -> Unit)? = null,
    private val onError: ((String) -> Unit)? = null
) {
    companion object {
        private const val TAG = "WS"
    }
    private val client = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
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

    /** Server boot ID — changes on server restart. */
    private var knownServerId: String? = null

    /** Restore persisted state from a previous app session. */
    fun restoreState(seq: Int, serverId: String) {
        lastSeq = seq
        expectedSeq = seq + 1
        knownServerId = serverId.ifEmpty { null }
    }

    fun connect(wsUrl: String) {
        Log.d(TAG, "connect() called url=$wsUrl")
        // Force-close any existing connection before opening a new one
        reconnectThread?.interrupt()
        reconnectThread = null
        ws?.cancel()
        ws = null
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
        // Force-close any existing connection to prevent dual connections.
        // cancel() is immediate (unlike close() which waits for handshake).
        val oldWs = ws
        ws = null
        oldWs?.cancel()
        onStateChange(if (reconnectAttempt == 0) ConnectionState.CONNECTING else ConnectionState.RECONNECTING)

        // Append last_seq to URL so server replays missed messages
        val connectUrl = if (lastSeq > 0) {
            val sep = if (baseUrl.contains("?")) "&" else "?"
            "${baseUrl}${sep}last_seq=$lastSeq"
        } else {
            baseUrl
        }

        Log.d(TAG, "doConnect() url=$connectUrl attempt=$reconnectAttempt")
        val request = Request.Builder().url(connectUrl).build()
        val newWs = client.newWebSocket(request, object : WebSocketListener() {
            /** Guard: ignore callbacks from a replaced/stale WebSocket. */
            private fun isStale(webSocket: WebSocket) = ws !== webSocket

            override fun onOpen(webSocket: WebSocket, response: Response) {
                if (isStale(webSocket)) return
                Log.d(TAG, "onOpen — connected!")
                reconnectAttempt = 0
                pendingBuffer.clear()
                resendRequested = false
                // If lastSeq is 0 (fresh or reset), accept whatever seq comes first
                expectedSeq = if (lastSeq == 0) 0 else lastSeq + 1
                onStateChange(ConnectionState.CONNECTED)
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                if (isStale(webSocket)) return
                handleIncomingText(text) { webSocket.send(it) }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                if (isStale(webSocket)) return
                Log.e(TAG, "onFailure: ${t.message}", t)
                onError?.invoke(t.message ?: "WebSocket failure")
                scheduleReconnect()
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                if (isStale(webSocket)) return
                Log.d(TAG, "onClosed code=$code reason=$reason")
                if (code != 1000) {
                    val msg = if (reason.isNotBlank()) "Closed ($code): $reason" else "Closed ($code)"
                    onError?.invoke(msg)
                    scheduleReconnect()
                }
                else onStateChange(ConnectionState.DISCONNECTED)
            }
        })
        ws = newWs
    }

    internal fun handleIncomingText(text: String, sendRaw: (String) -> Boolean = { ws?.send(it) ?: false }) {
        try {
            val json = JSONObject(text)

            // Handle server_hello — detect server restarts
            if (json.optString("type") == "server_hello") {
                val serverId = json.optString("server_id", "")
                val changed = knownServerId != serverId
                if (changed && knownServerId != null) {
                    // Server restarted — notify app to clear stale state
                    onServerRestart?.invoke()
                }
                // New server (restart or first connect) — accept whatever seq
                // the first replayed message has, since old numbering is gone.
                if (changed) {
                    // Reset in-memory seq tracking (0 = accept next seq as starting point)
                    // and reset persisted seq baseline to this new server instance.
                    expectedSeq = 0
                    lastSeq = 0
                    pendingBuffer.clear()
                    resendRequested = false
                    onSeqUpdate?.invoke(0, serverId)
                }
                knownServerId = serverId
                return
            }

            val seq = json.optInt("seq", 0)

            val msg = parseMessage(json, seq)

            if (seq == 0) {
                // No seq (e.g. error frames) — deliver immediately
                onMessage(msg)
                return
            }

            // First message after server change — adopt its seq as starting point
            if (expectedSeq == 0) {
                expectedSeq = seq
                lastSeq = seq - 1
            }

            // Detect server restart: seq jumped back to near 1
            if (seq < expectedSeq && expectedSeq - seq > 100) {
                // Server restarted — reset seq tracking to accept new numbering
                pendingBuffer.clear()
                resendRequested = false
                expectedSeq = seq
                lastSeq = seq - 1
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
                    // Out of order — buffer it.
                    pendingBuffer[seq] = msg
                    if (msg.isReplay) {
                        // A replayed/resent message that still sits ABOVE our gap means the
                        // server no longer has the missing seqs (evicted from its bounded replay
                        // buffer) — the gap is UNFILLABLE. Without this, the client waited forever
                        // for messages that no longer exist and the whole stream froze. Skip it:
                        // jump to the earliest message we do have and deliver, accepting the
                        // discontinuity (recent messages beat a permanently-stuck stream).
                        val skipTo = pendingBuffer.keys.minOrNull() ?: seq
                        Log.w(TAG, "Unfillable gap [$expectedSeq..${skipTo - 1}] " +
                                "(${skipTo - expectedSeq} lost msgs) — skipping forward")
                        expectedSeq = skipTo
                        resendRequested = false
                        flushPending()
                    } else if (!resendRequested) {
                        // Live message ahead of a gap — ask the server to resend the missing span.
                        // If the server still has it, the gap fills; if not, the resend response
                        // comes back marked is_replay and the branch above skips it.
                        resendRequested = true
                        val resendReq = JSONObject()
                            .put("type", "resend")
                            .put("from_seq", expectedSeq)
                            .toString()
                        sendRaw(resendReq)
                    }
                }
            }
        } catch (_: Exception) {}
    }

    /** Deliver a message and advance the expected seq. */
    private fun deliver(msg: WsMessage) {
        lastSeq = msg.seq
        expectedSeq = msg.seq + 1
        onMessage(msg)
        // Persist seq + server ID
        onSeqUpdate?.invoke(lastSeq, knownServerId ?: "")
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
        // Parse stream file_changes array
        val fileChanges = mutableListOf<Map<String, String>>()
        val fcArr = json.optJSONArray("file_changes")
        if (fcArr != null) {
            for (i in 0 until fcArr.length()) {
                val obj = fcArr.getJSONObject(i)
                val map = mutableMapOf<String, String>()
                obj.keys().forEach { key -> map[key] = obj.optString(key, "") }
                fileChanges.add(map)
            }
        }

        return WsMessage(
            type = json.optString("type", ""),
            messageId = if (json.has("message_id") && !json.isNull("message_id"))
                json.getInt("message_id") else null,
            text = json.optString("text", ""),
            session = json.optString("session", ""),
            seq = seq,
            isReplay = json.optBoolean("is_replay", false),
            createdAt = json.optLong("created_at", 0L),
            buttons = buttons,
            mode = json.optString("mode", ""),
            phase = json.optString("phase", ""),
            step = json.optInt("step", 0),
            active = json.optBoolean("active", false),
            task = json.optString("task", ""),
            started = json.optLong("started", 0),
            paused = json.optBoolean("paused", false),
            op = json.optString("op", ""),
            tool = json.optString("tool", ""),
            path = json.optString("path", ""),
            cancelled = json.optBoolean("cancelled", false),
            fileChanges = fileChanges,
            fileName = json.optString("file_name", ""),
            fileSize = json.optLong("file_size", 0),
            mimeType = json.optString("mime_type", ""),
            isImage = json.optBoolean("is_image", false),
            downloadPath = json.optString("file_path", ""),
            goalEvent = json.optString("event", ""),
            goalId = json.optString("goal_id", ""),
            goalTitle = json.optString("title", ""),
            milestonesTotal = json.optInt("milestones_total", 0),
            milestonesDone = json.optInt("milestones_done", 0),
            milestoneTitle = json.optString("milestone_title", ""),
            iteration = json.optInt("iteration", 0),
            outcome = json.optString("outcome", ""),
            reason = json.optString("reason", "")
        )
    }

    private fun scheduleReconnect() {
        if (!shouldReconnect) {
            onStateChange(ConnectionState.DISCONNECTED)
            return
        }
        onStateChange(ConnectionState.RECONNECTING)
        reconnectAttempt++
        // Fast retries: 500ms, 1s, 2s, 4s, 8s, then cap at 15s
        val delay = minOf(500L * (1L shl minOf(reconnectAttempt - 1, 4)), 15_000L)
        Log.d(TAG, "scheduleReconnect attempt=$reconnectAttempt delay=${delay}ms")

        reconnectThread = Thread {
            try {
                Thread.sleep(delay)
                doConnect()
            } catch (_: InterruptedException) {}
        }.also { it.isDaemon = true; it.start() }
    }
}
