package com.claudebot.app.ui.components

import androidx.compose.ui.text.AnnotatedString
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class MessageBubbleFilePathTest {
    @Test
    fun detectsChineseXlsxPathInText() {
        val path = "docs/investor/安守者三年运营成本测算.xlsx"
        val annotated = AnnotatedString("The file is at `$path`.").withFilePathAnnotations()

        assertTrue(annotated.hasTappableFilePath())
        assertEquals(
            path,
            annotated.getStringAnnotations(FILE_PATH_TAG, 0, annotated.length)
                .single()
                .item,
        )
    }

    @Test
    fun acceptsAbsoluteChineseXlsxPath() {
        val path = "/home/kafar/life-companion/docs/investor/安守者三年运营成本测算.xlsx"

        assertTrue(isLikelyFilePathTarget(path))
        assertEquals(path, normalizeFilePathForCommand("`$path`."))
    }

    @Test
    fun rejectsWebUrlsWithFileLikeLeaf() {
        assertFalse(isLikelyFilePathTarget("https://example.com/report.xlsx"))
    }

    @Test
    fun detectsPlainHttpsUrlInText() {
        val url = "https://example.com/docs/readme?tab=usage"
        val annotated = AnnotatedString("Read $url.").withFilePathAnnotations()

        assertTrue(annotated.hasTappableLinkTarget())
        assertEquals(
            url,
            annotated.getStringAnnotations(URL_TAG, 0, annotated.length)
                .single()
                .item,
        )
    }

    @Test
    fun keepsWebUrlOutOfFilePathAnnotations() {
        val annotated = AnnotatedString("Download https://example.com/report.xlsx").withFilePathAnnotations()

        assertTrue(annotated.hasTappableLinkTarget())
        assertTrue(annotated.getStringAnnotations(FILE_PATH_TAG, 0, annotated.length).isEmpty())
        assertEquals(
            "https://example.com/report.xlsx",
            annotated.getStringAnnotations(URL_TAG, 0, annotated.length)
                .single()
                .item,
        )
    }

    @Test
    fun normalizesWwwUrlForOpen() {
        assertTrue(isWebUrlTarget("www.example.com/report.xlsx"))
        assertEquals("https://www.example.com/report.xlsx", normalizeUrlForOpen("www.example.com/report.xlsx,"))
    }
}
