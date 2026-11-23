package com.claudebot.app.ui

import android.graphics.Bitmap
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import com.claudebot.app.data.ChatMessage
import com.claudebot.app.ui.components.MessageBubble
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okio.Buffer
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.util.concurrent.TimeUnit

class InlineImageTest {
    @get:Rule val rule = createComposeRule()
    private lateinit var server: MockWebServer

    @Before fun startServer() { server = MockWebServer(); server.start() }
    @After fun stopServer() { server.shutdown() }

    @Test fun extensionlessPngLoadsFromHttpAndOpensInFullScreen() {
        server.enqueue(pngResponse())
        server.enqueue(pngResponse()) // Larger fullscreen request can use a different decode size.
        val url = server.url("/render?size=320&data=a%2Fb%26c").toString()
        rule.setContent { MessageBubble(ChatMessage(1, "![Device QR]($url)", true)) }
        awaitImage()
        val request = server.takeRequest(2, TimeUnit.SECONDS)!!
        assertEquals("/render?size=320&data=a%2Fb%26c", request.path)
        rule.onNodeWithTag("inline-image-loaded").performClick()
        rule.onNodeWithTag("image-viewer").assertExists()
        rule.onNodeWithText("Close").performClick()
        rule.onNodeWithTag("image-viewer").assertDoesNotExist()
    }

    @Test fun userImageLinksAlsoLoadInChat() {
        server.enqueue(pngResponse())
        val url = server.url("/photo.png").toString()
        rule.setContent { MessageBubble(ChatMessage(1, "Photo: $url", false)) }
        awaitImage()
    }

    @Test fun svgResponseLoadsWithoutFilenameExtension() {
        server.enqueue(MockResponse().setHeader("Content-Type", "image/svg+xml").setBody(
            """<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect width="64" height="64" fill="white"/><rect x="8" y="8" width="48" height="48" fill="black"/></svg>"""
        ))
        val url = server.url("/vector").toString()
        rule.setContent { MessageBubble(ChatMessage(1, "![Diagram]($url)", true)) }
        awaitImage()
    }

    @Test fun failedImageKeepsWorkingOriginalUrlAction() {
        server.enqueue(MockResponse().setResponseCode(404))
        val url = server.url("/missing").toString()
        var opened: String? = null
        rule.setContent { MessageBubble(ChatMessage(1, "![QR]($url)", true), onUrlClick = { opened = it }) }
        rule.waitUntil(10_000) { rule.onAllNodesWithTag("inline-image-error").fetchSemanticsNodes().isNotEmpty() }
        rule.onNodeWithText("Couldn't load image").assertExists()
        rule.onNodeWithText("Open original").performClick()
        rule.runOnIdle { assertEquals(url, opened) }
    }

    private fun awaitImage() {
        rule.waitUntil(15_000) { rule.onAllNodesWithTag("inline-image-loaded").fetchSemanticsNodes().isNotEmpty() }
    }

    private fun pngResponse(): MockResponse {
        val image = Bitmap.createBitmap(64, 64, Bitmap.Config.ARGB_8888)
        image.eraseColor(android.graphics.Color.WHITE)
        for (x in 8..55) for (y in 8..55) {
            if ((x / 8 + y / 8) % 2 == 0) image.setPixel(x, y, android.graphics.Color.BLACK)
        }
        val output = ByteArrayOutputStream()
        image.compress(Bitmap.CompressFormat.PNG, 100, output)
        image.recycle()
        return MockResponse().setHeader("Content-Type", "image/png")
            .setHeader("Cache-Control", "public, max-age=3600")
            .setBody(Buffer().write(output.toByteArray()))
    }
}
