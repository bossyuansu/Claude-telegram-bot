package com.claudebot.app

/**
 * Process-local gate to ensure only one WS owner is active at a time.
 * Foreground UI and background SyncWorker must coordinate through this lock.
 */
object WsConnectionGate {
    const val OWNER_FOREGROUND = "foreground"
    const val OWNER_BACKGROUND = "background"

    /** A background hold older than this is treated as abandoned (worker died without releasing),
     *  so a crash can never block background sync permanently. */
    private const val STALE_BACKGROUND_HOLD_MS = 5 * 60 * 1000L

    private val lock = Any()
    private var owner: String? = null
    private var acquiredAtMs: Long = 0L
    @Volatile private var foregroundActive: Boolean = false

    fun setForegroundActive(active: Boolean) {
        foregroundActive = active
    }

    fun isForegroundActive(): Boolean = foregroundActive

    /** Foreground takes priority and can preempt background ownership. */
    fun acquireForeground() {
        synchronized(lock) {
            owner = OWNER_FOREGROUND
            acquiredAtMs = System.currentTimeMillis()
        }
    }

    /**
     * Background may acquire ONLY when nothing else holds the gate.
     *
     * This previously also returned true when the owner was already OWNER_BACKGROUND, which meant
     * it granted no mutual exclusion between background workers. The periodic ("ws_sync") and
     * immediate ("ws_sync_immediate") workers are separate unique-work chains, so WorkManager runs
     * them concurrently — both acquired, both opened a WebSocket, and the first to finish released
     * the gate out from under the other. That produced duplicate server connections (observed up
     * to 4 at once), duplicate replay traffic on an already-thin relay link, and duplicate
     * notifications for the same messages.
     */
    fun tryAcquireBackground(): Boolean {
        synchronized(lock) {
            val now = System.currentTimeMillis()
            val staleHold = owner == OWNER_BACKGROUND && now - acquiredAtMs > STALE_BACKGROUND_HOLD_MS
            return if (owner == null || staleHold) {
                owner = OWNER_BACKGROUND
                acquiredAtMs = now
                true
            } else {
                false
            }
        }
    }

    fun release(ownerName: String) {
        synchronized(lock) {
            if (owner == ownerName) owner = null
        }
    }

    fun currentOwner(): String? = synchronized(lock) { owner }
}

