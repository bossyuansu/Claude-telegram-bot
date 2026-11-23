package com.claudebot.app.util

import org.junit.Assert.*
import org.junit.Test

class InlineImagesTest {
    private val qr = "https://api.qrserver.com/v1/create-qr-code/?size=320x320&data=demo%3A%2F%2Fdevice%3Fa%3D1%26b%3D2"

    @Test fun markdownImageSupportsExtensionlessEndpointAndPreservesQuery() {
        val images = parseMarkdown("Scan this:\n![Device QR]($qr)\nThen continue.")
        assertEquals(MessageSegment.Image(qr, "Device QR"), images.filterIsInstance<MessageSegment.Image>().single())
        assertEquals("Scan this:\n", (images.first() as MessageSegment.Text).annotated.text)
        assertEquals("\nThen continue.", (images.last() as MessageSegment.Text).annotated.text)
    }

    @Test fun existingQrLinkAndBareQrUrlPreviewWithoutPngSuffix() {
        assertEquals(MessageSegment.Image(qr, "Scan QR"), parseMarkdown("[Scan QR]($qr)").single())
        assertEquals(MessageSegment.Image(qr), parseMarkdown(qr).single())
    }

    @Test fun normalImagesSupportPlainAndLabelledLinks() {
        val url = "https://example.com/Photo.PNG?token=a%2Bb&size=400#preview"
        assertEquals(MessageSegment.Image(url), parseMarkdown("<$url>").single())
        assertEquals(MessageSegment.Image(url, "Preview"), parseMarkdown("[Preview]($url)").single())
        val result = parseMarkdown("Look: $url.")
        assertEquals(url, result.filterIsInstance<MessageSegment.Image>().single().url)
        assertEquals(".", (result.last() as MessageSegment.Text).annotated.text)
    }

    @Test fun balancedParenthesesAndMarkdownTitlesStayOutOfImageUrl() {
        val url = "https://example.com/photos/a_(b).png?signature=x%2Fy"
        assertEquals(MessageSegment.Image(url, "Photo"), parseMarkdown("![Photo]($url \"A title\")").single())
        assertEquals(MessageSegment.Image(url, "Photo"), parseMarkdown("![Photo](<$url> 'A title')").single())
    }

    @Test fun multipleImagesKeepTheirOrderAndCaptions() {
        val images = parseMarkdown("![First](https://example.com/1.jpg)\n![Second]($qr)")
            .filterIsInstance<MessageSegment.Image>()
        assertEquals(listOf("First", "Second"), images.map { it.altText })
    }

    @Test fun examplesInInlineAndFencedCodeDoNotLoadImages() {
        val source = "`![QR]($qr)`\n```markdown\n![QR]($qr)\n```"
        val parsed = parseMarkdown(source)
        assertTrue(parsed.none { it is MessageSegment.Image })
        assertTrue(parsed.filterIsInstance<MessageSegment.CodeBlock>().single().code.contains(qr))
        assertTrue(parseMessageImages(source).none { it is MessageSegment.Image })
    }

    @Test fun escapedImageSyntaxIsNotARequest() {
        assertTrue(parseMarkdown("\\![QR]($qr)").none { it is MessageSegment.Image })
    }

    @Test fun partialStreamingImageStaysTextUntilComplete() {
        val partial = "![QR]($qr"
        assertTrue(parseMarkdown(partial).none { it is MessageSegment.Image })
        assertEquals(MessageSegment.Image(qr, "QR"), parseMarkdown("$partial)").single())
    }

    @Test fun ordinaryWebLinksAndTheirEmbeddedUrlsRemainLinks() {
        for (value in listOf(
            "[Docs](https://example.com/docs?next=https://example.com/example.png)",
            "https://example.com/docs",
            "[Local image](/home/kafar/qr.png)",
        )) assertTrue(parseMarkdown(value).none { it is MessageSegment.Image })
        val text = (parseMarkdown("[Docs](https://example.com/docs)").single() as MessageSegment.Text).annotated
        assertEquals("https://example.com/docs", text.getStringAnnotations("URL", 0, text.length).single().item)
    }

    @Test fun explicitImagesDoNotEnableLocalOrNonHttpSchemes() {
        for (url in listOf("file:///sdcard/private.png", "content://media/1", "data:image/png;base64,AA", "javascript:alert(1)", "https:///missing-host")) {
            assertTrue(url, parseMarkdown("![Example]($url)").none { it is MessageSegment.Image })
        }
    }

    @Test fun userTextFormattingRemainsLiteralAroundImage() {
        val parsed = parseMessageImages("**literal**\n![QR]($qr)")
        val text = (parsed.first() as MessageSegment.Text).annotated
        assertEquals("**literal**\n", text.text)
        assertTrue(text.spanStyles.isEmpty())
        assertEquals(qr, parsed.filterIsInstance<MessageSegment.Image>().single().url)
    }
}
