package com.claudebot.app

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * The gate must give real mutual exclusion. Regression: tryAcquireBackground() used to also
 * succeed when the owner was already OWNER_BACKGROUND, so the periodic ("ws_sync") and immediate
 * ("ws_sync_immediate") workers — separate unique-work chains that WorkManager runs concurrently —
 * both opened a WebSocket. That caused duplicate server connections, duplicate replay traffic over
 * the relay, and duplicate notifications.
 */
class WsConnectionGateTest {

    @Before
    fun reset() {
        // Singleton: clear whichever owner a previous test left behind.
        WsConnectionGate.release(WsConnectionGate.OWNER_FOREGROUND)
        WsConnectionGate.release(WsConnectionGate.OWNER_BACKGROUND)
        WsConnectionGate.setForegroundActive(false)
    }

    @Test
    fun secondBackgroundWorkerIsRefusedWhileFirstHoldsGate() {
        assertTrue("first background worker acquires", WsConnectionGate.tryAcquireBackground())
        assertFalse("second concurrent background worker must be refused",
            WsConnectionGate.tryAcquireBackground())

        WsConnectionGate.release(WsConnectionGate.OWNER_BACKGROUND)
        assertTrue("after release the gate is available again",
            WsConnectionGate.tryAcquireBackground())
    }

    @Test
    fun backgroundIsRefusedWhileForegroundOwnsGate() {
        WsConnectionGate.acquireForeground()
        assertFalse("background must not connect while the UI owns the socket",
            WsConnectionGate.tryAcquireBackground())
    }

    @Test
    fun foregroundPreemptsBackgroundOwnership() {
        assertTrue(WsConnectionGate.tryAcquireBackground())
        WsConnectionGate.acquireForeground()
        assertEqualsOwner(WsConnectionGate.OWNER_FOREGROUND)

        // A stale background release must not clear foreground ownership.
        WsConnectionGate.release(WsConnectionGate.OWNER_BACKGROUND)
        assertEqualsOwner(WsConnectionGate.OWNER_FOREGROUND)
    }

    private fun assertEqualsOwner(expected: String) {
        org.junit.Assert.assertEquals(expected, WsConnectionGate.currentOwner())
    }
}
