package com.claudebot.app.ui

import org.junit.Assert.*
import org.junit.Before
import org.junit.Test

/**
 * JVM unit tests for the Mission Control "viewing session" logic.
 * Mirrors ViewModel state without Android dependencies.
 *
 * Single source of truth: `sessionFilter` drives both the filtered chat view
 * and the mismatch banner. No separate "viewingSession" state exists.
 */
class ViewingSessionTest {

    private var sessionFilter: String? = null
    private var currentSession: String = ""
    private var switchedTo: String? = null

    /** Mirrors ViewModel.isViewingOtherSession — derivation, not stored state. */
    private val isViewingOtherSession: Boolean
        get() {
            val filter = sessionFilter ?: return false
            return filter != currentSession
        }

    /** MC tap: sets filter only — never calls switchSession. */
    private fun viewTaskSession(name: String) {
        sessionFilter = name
    }

    /** Quick-switch: sends the switch command first, then clears the filter. */
    private fun quickSwitchToFilteredSession() {
        val target = sessionFilter ?: return
        switchedTo = target          // switchSession(target)
        sessionFilter = null         // clearSessionFilter()
    }

    /** Clear filter: returns to all-sessions view. */
    private fun clearSessionFilter() {
        sessionFilter = null
    }

    @Before
    fun setUp() {
        sessionFilter = null
        currentSession = "default"
        switchedTo = null
    }

    // --- viewTaskSession ---

    @Test
    fun `viewTaskSession sets filter`() {
        viewTaskSession("omni-session")
        assertEquals("omni-session", sessionFilter)
    }

    @Test
    fun `viewTaskSession does NOT switch server session`() {
        viewTaskSession("omni-session")
        assertNull("switchSession must not be called on MC tap", switchedTo)
    }

    // --- isViewingOtherSession (banner visibility) ---

    @Test
    fun `mismatch true when filter differs from current`() {
        currentSession = "default"
        viewTaskSession("omni-session")
        assertTrue(isViewingOtherSession)
    }

    @Test
    fun `mismatch false when filter matches current`() {
        currentSession = "omni-session"
        viewTaskSession("omni-session")
        assertFalse(isViewingOtherSession)
    }

    @Test
    fun `mismatch false when no filter`() {
        assertFalse(isViewingOtherSession)
    }

    @Test
    fun `mismatch resolves when currentSession changes to match filter`() {
        viewTaskSession("omni-session")
        assertTrue(isViewingOtherSession)

        // Simulate server acknowledging the switch
        currentSession = "omni-session"
        assertFalse(isViewingOtherSession)
    }

    // --- quickSwitchToFilteredSession ---

    @Test
    fun `quickSwitch switches then clears filter`() {
        viewTaskSession("omni-session")
        assertTrue(isViewingOtherSession)

        quickSwitchToFilteredSession()
        assertEquals("omni-session", switchedTo)
        assertNull(sessionFilter)
        assertFalse(isViewingOtherSession)
    }

    @Test
    fun `quickSwitch is no-op when no filter`() {
        quickSwitchToFilteredSession()
        assertNull(switchedTo)
    }

    // --- clearSessionFilter ---

    @Test
    fun `clearSessionFilter removes banner and returns to all-sessions`() {
        viewTaskSession("omni-session")
        assertTrue(isViewingOtherSession)

        clearSessionFilter()
        assertNull(sessionFilter)
        assertFalse(isViewingOtherSession)
    }

    @Test
    fun `clearSessionFilter is idempotent`() {
        clearSessionFilter()
        assertNull(sessionFilter)
        assertFalse(isViewingOtherSession)
    }
}
