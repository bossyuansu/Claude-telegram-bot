package com.claudebot.app.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import com.claudebot.app.ui.components.MicButton
import org.junit.Rule
import org.junit.Test

class MicPulseTest {

    @get:Rule
    val composeTestRule = createComposeRule()

    @Test
    fun micIdleTagWhenNotListening() {
        composeTestRule.setContent {
            MaterialTheme {
                MicButton(isListening = false, onClick = {})
            }
        }
        composeTestRule.onNodeWithTag("mic_idle").assertExists()
        composeTestRule.onNodeWithTag("mic_listening").assertDoesNotExist()
    }

    @Test
    fun micListeningTagWhenListening() {
        // Disable auto-advance: MicButton has infinite animation when listening
        composeTestRule.mainClock.autoAdvance = false
        composeTestRule.setContent {
            MaterialTheme {
                MicButton(isListening = true, onClick = {})
            }
        }
        composeTestRule.mainClock.advanceTimeBy(100)
        composeTestRule.onNodeWithTag("mic_listening").assertExists()
        composeTestRule.onNodeWithTag("mic_idle").assertDoesNotExist()
    }

    @Test
    fun micTransitionsFromIdleToListeningAndBack() {
        composeTestRule.mainClock.autoAdvance = false
        val isListening = mutableStateOf(false)
        composeTestRule.setContent {
            MaterialTheme {
                MicButton(isListening = isListening.value, onClick = {})
            }
        }

        // Initially idle
        composeTestRule.mainClock.advanceTimeBy(100)
        composeTestRule.onNodeWithTag("mic_idle").assertExists()
        composeTestRule.onNodeWithTag("mic_listening").assertDoesNotExist()

        // Transition to listening
        isListening.value = true
        composeTestRule.mainClock.advanceTimeBy(100)
        composeTestRule.onNodeWithTag("mic_listening").assertExists()
        composeTestRule.onNodeWithTag("mic_idle").assertDoesNotExist()

        // Transition back to idle
        isListening.value = false
        composeTestRule.mainClock.advanceTimeBy(100)
        composeTestRule.onNodeWithTag("mic_idle").assertExists()
        composeTestRule.onNodeWithTag("mic_listening").assertDoesNotExist()
    }
}
