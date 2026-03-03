package com.claudebot.app

/**
 * Process-local gate to ensure only one WS owner is active at a time.
 * Foreground UI and background SyncWorker must coordinate through this lock.
 */
object WsConnectionGate {
    const val OWNER_FOREGROUND = "foreground"
    const val OWNER_BACKGROUND = "background"

    private val lock = Any()
    private var owner: String? = null
    @Volatile private var foregroundActive: Boolean = false

    fun setForegroundActive(active: Boolean) {
        foregroundActive = active
    }

    fun isForegroundActive(): Boolean = foregroundActive

    /** Foreground takes priority and can preempt background ownership. */
    fun acquireForeground() {
        synchronized(lock) {
            owner = OWNER_FOREGROUND
        }
    }

    /** Background may acquire only when no current owner is active. */
    fun tryAcquireBackground(): Boolean {
        synchronized(lock) {
            return when (owner) {
                null, OWNER_BACKGROUND -> {
                    owner = OWNER_BACKGROUND
                    true
                }
                else -> false
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

