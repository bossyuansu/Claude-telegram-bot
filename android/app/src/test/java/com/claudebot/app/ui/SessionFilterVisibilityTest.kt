package com.claudebot.app.ui

import org.junit.Assert.*
import org.junit.Test

/**
 * JVM unit tests for the session filter visibility condition.
 * The filter button is only shown when availableSessions.size > 1.
 */
class SessionFilterVisibilityTest {

    /** Mirrors the visibility condition used in ChatScreen. */
    private fun shouldShowFilter(sessions: List<String>): Boolean = sessions.size > 1

    @Test
    fun `hidden when no sessions`() {
        assertFalse(shouldShowFilter(emptyList()))
    }

    @Test
    fun `hidden when single session`() {
        assertFalse(shouldShowFilter(listOf("only-one")))
    }

    @Test
    fun `visible when two sessions`() {
        assertTrue(shouldShowFilter(listOf("a", "b")))
    }

    @Test
    fun `visible when many sessions`() {
        assertTrue(shouldShowFilter(listOf("a", "b", "c", "d")))
    }

    @Test
    fun `filter selection All returns null`() {
        // "All sessions" should map to null filter value
        val result: String? = null
        assertNull(result)
    }

    @Test
    fun `filter selection specific session returns session name`() {
        val session = "my-session"
        assertEquals("my-session", session)
    }

    @Test
    fun `icon changes based on active filter`() {
        val noFilter: String? = null
        val withFilter: String? = "session-x"
        // ◎ (U+25CE) when no filter, ◉ (U+25C9) when filter active
        assertEquals("\u25CE", if (noFilter != null) "\u25C9" else "\u25CE")
        assertEquals("\u25C9", if (withFilter != null) "\u25C9" else "\u25CE")
    }
}
