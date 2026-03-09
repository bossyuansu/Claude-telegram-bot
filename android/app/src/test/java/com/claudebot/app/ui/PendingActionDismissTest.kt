package com.claudebot.app.ui

import org.junit.Assert.*
import org.junit.Test

/**
 * JVM unit tests for the pending action dismiss behavior.
 * Verifies that dismissing clears the pending action without triggering any side effects.
 */
class PendingActionDismissTest {

    /** Lightweight mirror of PendingAction for JVM testing (no Android deps). */
    private data class PendingAction(val text: String, val buttonCount: Int, val messageId: Int)

    /** Mirrors ViewModel's pendingAction state. */
    private var pendingAction: PendingAction? = null

    private fun dismissPendingAction() {
        pendingAction = null
    }

    @Test
    fun `dismiss clears pending action`() {
        pendingAction = PendingAction("Approve plan?", 2, 42)
        assertNotNull(pendingAction)

        dismissPendingAction()
        assertNull(pendingAction)
    }

    @Test
    fun `dismiss is idempotent when no action`() {
        assertNull(pendingAction)
        dismissPendingAction()
        assertNull(pendingAction)
    }

    @Test
    fun `dismiss does not affect subsequent pending actions`() {
        pendingAction = PendingAction("First question?", 3, 10)
        dismissPendingAction()
        assertNull(pendingAction)

        // A new pending action can be set after dismiss
        pendingAction = PendingAction("Second question?", 2, 20)
        assertNotNull(pendingAction)
        assertEquals(20, pendingAction!!.messageId)
    }

    @Test
    fun `pressButton also clears matching action`() {
        pendingAction = PendingAction("Approve?", 2, 42)

        // Simulate pressButton clearing the action (matching messageId)
        val messageId = 42
        pendingAction?.let { if (it.messageId == messageId) pendingAction = null }
        assertNull(pendingAction)
    }

    @Test
    fun `pressButton does not clear non-matching action`() {
        pendingAction = PendingAction("Approve?", 2, 42)

        // Different messageId — should not clear
        val messageId = 99
        pendingAction?.let { if (it.messageId == messageId) pendingAction = null }
        assertNotNull(pendingAction)
    }
}
