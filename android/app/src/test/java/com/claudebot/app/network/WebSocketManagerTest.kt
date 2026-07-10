package com.claudebot.app.network

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class WebSocketManagerTest {
    private fun manager(delivered: MutableList<WsMessage>) = WebSocketManager(
        onMessage = { delivered.add(it) },
        onStateChange = {},
    )

    /** A missed span replayed in order (as TCP guarantees) is delivered contiguously — no resend. */
    @Test
    fun inOrderReplayDeliversContiguously() {
        val delivered = mutableListOf<WsMessage>()
        val outbound = mutableListOf<String>()
        val m = manager(delivered)
        m.restoreState(9, "srv") // expects seq 10 next
        m.handleIncomingText("""{"type":"server_hello","server_id":"srv"}""") { outbound.add(it); true }
        m.handleIncomingText("""{"type":"message","seq":10,"is_replay":true,"text":"ten","session":"s"}""") { outbound.add(it); true }
        m.handleIncomingText("""{"type":"message","seq":11,"is_replay":true,"text":"eleven","session":"s"}""") { outbound.add(it); true }

        assertEquals(listOf("ten", "eleven"), delivered.map { it.text })
        assertEquals(11, m.lastSeq)
        assertTrue("contiguous replay needs no resend", outbound.isEmpty())
    }

    /** Regression: when the missing seqs were evicted from the server buffer, the first replayed
     * message sits ABOVE the gap. The client must SKIP forward (deliver what's available) instead
     * of freezing forever waiting for messages that no longer exist. */
    @Test
    fun unfillableGapSkipsForwardInsteadOfFreezing() {
        val delivered = mutableListOf<WsMessage>()
        val outbound = mutableListOf<String>()
        val m = manager(delivered)
        m.restoreState(9, "srv") // expects seq 10 next
        m.handleIncomingText("""{"type":"server_hello","server_id":"srv"}""") { outbound.add(it); true }
        // seqs 10 & 11 evicted; the earliest the server still has is 12.
        m.handleIncomingText("""{"type":"message","seq":12,"is_replay":true,"text":"twelve","session":"s"}""") { outbound.add(it); true }

        assertEquals(listOf("twelve"), delivered.map { it.text })
        assertEquals(12, m.lastSeq)
        assertTrue("unfillable gap is skipped, not resent", outbound.isEmpty())

        // and the stream keeps flowing contiguously afterwards
        m.handleIncomingText("""{"type":"message","seq":13,"is_replay":true,"text":"thirteen","session":"s"}""") { outbound.add(it); true }
        assertEquals(listOf("twelve", "thirteen"), delivered.map { it.text })
        assertEquals(13, m.lastSeq)
    }

    /** A LIVE (non-replay) message ahead of the expected seq still triggers a resend request —
     * the server may still hold the gap; if it does, the gap fills and both deliver in order. */
    @Test
    fun liveGapRequestsResendThenFills() {
        val delivered = mutableListOf<WsMessage>()
        val outbound = mutableListOf<String>()
        val m = manager(delivered)
        m.restoreState(9, "srv") // expects seq 10 next
        m.handleIncomingText("""{"type":"server_hello","server_id":"srv"}""") { outbound.add(it); true }
        m.handleIncomingText("""{"type":"message","seq":11,"is_replay":false,"text":"eleven","session":"s"}""") { outbound.add(it); true }

        assertTrue("live gap holds delivery pending resend", delivered.isEmpty())
        assertEquals(listOf("""{"type":"resend","from_seq":10}"""), outbound)

        // Server still had 10 → resend fills the gap; both flush in order.
        m.handleIncomingText("""{"type":"message","seq":10,"is_replay":true,"text":"ten","session":"s"}""") { outbound.add(it); true }
        assertEquals(listOf("ten", "eleven"), delivered.map { it.text })
        assertEquals(11, m.lastSeq)
    }
}
