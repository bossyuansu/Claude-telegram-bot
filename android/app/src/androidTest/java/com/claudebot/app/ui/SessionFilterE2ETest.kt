package com.claudebot.app.ui

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.claudebot.app.ui.screens.SessionFilterButton
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * End-to-end Compose test that renders a chat-like screen with session filter dropdown,
 * switches filters, and verifies the rendered message list changes accordingly.
 */
@RunWith(AndroidJUnit4::class)
class SessionFilterE2ETest {

    @get:Rule
    val rule = createComposeRule()

    /** Simple message data for testing. */
    private data class TestMessage(val id: Int, val text: String, val session: String)

    private val allMessages = listOf(
        TestMessage(1, "Hello from alpha", "alpha"),
        TestMessage(2, "Alpha update", "alpha"),
        TestMessage(3, "Beta says hi", "beta"),
        TestMessage(4, "Beta report", "beta"),
        TestMessage(5, "Gamma note", "gamma"),
    )

    /**
     * Composable that mimics the chat screen with a session filter dropdown and message list.
     * Filtering is done in-memory for test simplicity.
     */
    @Composable
    private fun TestChatScreen() {
        var activeFilter by remember { mutableStateOf<String?>(null) }
        var expanded by remember { mutableStateOf(false) }
        val sessions = remember { allMessages.map { it.session }.distinct().sorted() }
        val displayed = remember(activeFilter) {
            if (activeFilter == null) allMessages
            else allMessages.filter { it.session == activeFilter }
        }

        Column(modifier = Modifier.fillMaxSize()) {
            // Top bar with filter
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(8.dp),
                horizontalArrangement = Arrangement.End
            ) {
                SessionFilterButton(
                    expanded = expanded,
                    onToggle = { expanded = !expanded },
                    onDismiss = { expanded = false },
                    sessions = sessions,
                    activeFilter = activeFilter,
                    onSelect = { activeFilter = it; expanded = false }
                )
            }

            // Message list
            LazyColumn(modifier = Modifier.testTag("message_list")) {
                items(displayed, key = { it.id }) { msg ->
                    Text(
                        text = msg.text,
                        fontSize = 14.sp,
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(8.dp)
                            .testTag("msg_${msg.id}")
                    )
                }
            }
        }
    }

    @Test
    fun initialState_showsAllMessages() {
        rule.setContent { TestChatScreen() }
        // All 5 messages should be visible
        for (msg in allMessages) {
            rule.onNodeWithTag("msg_${msg.id}").assertExists()
        }
    }

    @Test
    fun openDropdown_showsAllSessions() {
        rule.setContent { TestChatScreen() }

        // Click the filter button to open dropdown
        rule.onNodeWithTag("session_filter").performClick()
        rule.waitForIdle()

        // Check dropdown items
        rule.onNodeWithText("All sessions").assertExists()
        rule.onNodeWithText("alpha").assertExists()
        rule.onNodeWithText("beta").assertExists()
        rule.onNodeWithText("gamma").assertExists()
    }

    @Test
    fun selectAlpha_showsOnlyAlphaMessages() {
        rule.setContent { TestChatScreen() }

        // Open dropdown
        rule.onNodeWithTag("session_filter").performClick()
        rule.waitForIdle()

        // Select alpha
        rule.onNodeWithText("alpha").performClick()
        rule.waitForIdle()

        // Alpha messages should be visible
        rule.onNodeWithTag("msg_1").assertExists()
        rule.onNodeWithTag("msg_2").assertExists()

        // Others should not exist
        rule.onNodeWithTag("msg_3").assertDoesNotExist()
        rule.onNodeWithTag("msg_4").assertDoesNotExist()
        rule.onNodeWithTag("msg_5").assertDoesNotExist()
    }

    @Test
    fun selectBeta_showsOnlyBetaMessages() {
        rule.setContent { TestChatScreen() }

        // Open and select beta
        rule.onNodeWithTag("session_filter").performClick()
        rule.waitForIdle()
        rule.onNodeWithText("beta").performClick()
        rule.waitForIdle()

        // Beta messages visible
        rule.onNodeWithTag("msg_3").assertExists()
        rule.onNodeWithTag("msg_4").assertExists()

        // Others gone
        rule.onNodeWithTag("msg_1").assertDoesNotExist()
        rule.onNodeWithTag("msg_2").assertDoesNotExist()
        rule.onNodeWithTag("msg_5").assertDoesNotExist()
    }

    @Test
    fun switchFilter_thenBackToAll_restoresAllMessages() {
        rule.setContent { TestChatScreen() }

        // Filter to gamma (1 message)
        rule.onNodeWithTag("session_filter").performClick()
        rule.waitForIdle()
        rule.onNodeWithText("gamma").performClick()
        rule.waitForIdle()

        rule.onNodeWithTag("msg_5").assertExists()
        rule.onNodeWithTag("msg_1").assertDoesNotExist()

        // Switch back to all
        rule.onNodeWithTag("session_filter").performClick()
        rule.waitForIdle()
        rule.onNodeWithText("All sessions").performClick()
        rule.waitForIdle()

        // All messages restored
        for (msg in allMessages) {
            rule.onNodeWithTag("msg_${msg.id}").assertExists()
        }
    }

    @Test
    fun switchBetweenFilters_updatesListEachTime() {
        rule.setContent { TestChatScreen() }

        // alpha -> beta -> gamma -> all
        val expectations = listOf(
            "alpha" to setOf(1, 2),
            "beta" to setOf(3, 4),
            "gamma" to setOf(5),
        )

        for ((session, expectedIds) in expectations) {
            rule.onNodeWithTag("session_filter").performClick()
            rule.waitForIdle()
            rule.onNodeWithText(session).performClick()
            rule.waitForIdle()

            for (id in 1..5) {
                if (id in expectedIds) {
                    rule.onNodeWithTag("msg_$id").assertExists()
                } else {
                    rule.onNodeWithTag("msg_$id").assertDoesNotExist()
                }
            }
        }

        // Back to all
        rule.onNodeWithTag("session_filter").performClick()
        rule.waitForIdle()
        rule.onNodeWithText("All sessions").performClick()
        rule.waitForIdle()
        for (msg in allMessages) {
            rule.onNodeWithTag("msg_${msg.id}").assertExists()
        }
    }

    @Test
    fun activeFilterIcon_changesWhenFilterIsActive() {
        rule.setContent { TestChatScreen() }

        // Initially no filter — should show empty circle ◎
        rule.onNodeWithText("\u25CE").assertExists()

        // Select a filter
        rule.onNodeWithTag("session_filter").performClick()
        rule.waitForIdle()
        rule.onNodeWithText("alpha").performClick()
        rule.waitForIdle()

        // Active filter — should show filled circle ◉
        rule.onNodeWithText("\u25C9").assertExists()

        // Clear filter
        rule.onNodeWithTag("session_filter").performClick()
        rule.waitForIdle()
        rule.onNodeWithText("All sessions").performClick()
        rule.waitForIdle()

        // Back to empty circle
        rule.onNodeWithText("\u25CE").assertExists()
    }

    @Test
    fun checkmarkAppearsOnSelectedSession() {
        rule.setContent { TestChatScreen() }

        // Select alpha
        rule.onNodeWithTag("session_filter").performClick()
        rule.waitForIdle()
        rule.onNodeWithText("alpha").performClick()
        rule.waitForIdle()

        // Reopen dropdown and verify checkmark
        rule.onNodeWithTag("session_filter").performClick()
        rule.waitForIdle()
        rule.onNodeWithText("\u2713").assertExists() // ✓ checkmark
    }
}
