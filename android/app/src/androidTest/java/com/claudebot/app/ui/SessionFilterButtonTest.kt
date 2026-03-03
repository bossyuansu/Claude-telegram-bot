package com.claudebot.app.ui

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import com.claudebot.app.ui.screens.SessionFilterButton
import org.junit.Rule
import org.junit.Test

class SessionFilterButtonTest {

    @get:Rule
    val rule = createComposeRule()

    @Test
    fun filterButtonIsVisibleWithTestTag() {
        rule.setContent {
            SessionFilterButton(
                expanded = false,
                onToggle = {},
                onDismiss = {},
                sessions = listOf("session-a", "session-b"),
                activeFilter = null,
                onSelect = {}
            )
        }
        rule.onNodeWithTag("session_filter").assertExists()
    }

    @Test
    fun dropdownShowsAllSessionsWhenExpanded() {
        rule.setContent {
            SessionFilterButton(
                expanded = true,
                onToggle = {},
                onDismiss = {},
                sessions = listOf("alpha", "beta"),
                activeFilter = null,
                onSelect = {}
            )
        }
        rule.onNodeWithText("All sessions").assertExists()
        rule.onNodeWithText("alpha").assertExists()
        rule.onNodeWithText("beta").assertExists()
    }

    @Test
    fun checkmarkShownOnActiveFilter() {
        rule.setContent {
            SessionFilterButton(
                expanded = true,
                onToggle = {},
                onDismiss = {},
                sessions = listOf("alpha", "beta"),
                activeFilter = "alpha",
                onSelect = {}
            )
        }
        // The checkmark ✓ should appear for the active filter
        rule.onNodeWithText("\u2713").assertExists()
    }

    @Test
    fun selectingSessionCallsOnSelect() {
        var selected: String? = "initial"
        rule.setContent {
            SessionFilterButton(
                expanded = true,
                onToggle = {},
                onDismiss = {},
                sessions = listOf("alpha", "beta"),
                activeFilter = null,
                onSelect = { selected = it }
            )
        }
        rule.onNodeWithText("alpha").performClick()
        assert(selected == "alpha") { "Expected 'alpha' but got '$selected'" }
    }

    @Test
    fun selectingAllCallsOnSelectWithNull() {
        var selected: String? = "initial"
        rule.setContent {
            SessionFilterButton(
                expanded = true,
                onToggle = {},
                onDismiss = {},
                sessions = listOf("alpha"),
                activeFilter = "alpha",
                onSelect = { selected = it }
            )
        }
        rule.onNodeWithText("All sessions").performClick()
        assert(selected == null) { "Expected null but got '$selected'" }
    }
}
