package com.claudebot.app.data

import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import kotlinx.coroutines.test.runTest
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Room integration tests for MessageDao running against an in-memory SQLite database.
 * Covers getPageBySession, countBySession, getDistinctSessions with mixed sessions,
 * pagination, and ordering.
 */
@RunWith(AndroidJUnit4::class)
class MessageDaoTest {

    private lateinit var db: AppDatabase
    private lateinit var dao: MessageDao

    @Before
    fun setUp() {
        db = Room.inMemoryDatabaseBuilder(
            ApplicationProvider.getApplicationContext(),
            AppDatabase::class.java
        ).allowMainThreadQueries().build()
        dao = db.messageDao()
    }

    @After
    fun tearDown() {
        db.close()
    }

    // --- Helper ---
    private fun msg(
        messageId: Int,
        text: String,
        session: String,
        timestamp: Long = System.currentTimeMillis(),
        isFromBot: Boolean = true
    ) = MessageEntity(
        messageId = messageId,
        text = text,
        isFromBot = isFromBot,
        session = session,
        timestamp = timestamp
    )

    // ==================== getPageBySession ====================

    @Test
    fun getPageBySession_returnsOnlyMatchingSession() = runTest {
        dao.insert(msg(1, "hello", "alpha", timestamp = 1000))
        dao.insert(msg(2, "world", "beta", timestamp = 2000))
        dao.insert(msg(3, "foo", "alpha", timestamp = 3000))

        val alphaPage = dao.getPageBySession("alpha", limit = 50, offset = 0)
        assertEquals(2, alphaPage.size)
        assertTrue(alphaPage.all { it.session == "alpha" })

        val betaPage = dao.getPageBySession("beta", limit = 50, offset = 0)
        assertEquals(1, betaPage.size)
        assertEquals("world", betaPage[0].text)
    }

    @Test
    fun getPageBySession_ordersNewestFirst() = runTest {
        dao.insert(msg(1, "old", "s1", timestamp = 1000))
        dao.insert(msg(2, "mid", "s1", timestamp = 2000))
        dao.insert(msg(3, "new", "s1", timestamp = 3000))

        val page = dao.getPageBySession("s1", limit = 50, offset = 0)
        assertEquals(listOf("new", "mid", "old"), page.map { it.text })
    }

    @Test
    fun getPageBySession_pagination() = runTest {
        // Insert 5 messages into session "s1" with increasing timestamps
        for (i in 1..5) {
            dao.insert(msg(i, "msg$i", "s1", timestamp = i * 1000L))
        }

        // Page 1: newest 2
        val page1 = dao.getPageBySession("s1", limit = 2, offset = 0)
        assertEquals(2, page1.size)
        assertEquals("msg5", page1[0].text)
        assertEquals("msg4", page1[1].text)

        // Page 2: next 2
        val page2 = dao.getPageBySession("s1", limit = 2, offset = 2)
        assertEquals(2, page2.size)
        assertEquals("msg3", page2[0].text)
        assertEquals("msg2", page2[1].text)

        // Page 3: remaining 1
        val page3 = dao.getPageBySession("s1", limit = 2, offset = 4)
        assertEquals(1, page3.size)
        assertEquals("msg1", page3[0].text)

        // Past end: empty
        val page4 = dao.getPageBySession("s1", limit = 2, offset = 5)
        assertTrue(page4.isEmpty())
    }

    @Test
    fun getPageBySession_emptyForNonexistentSession() = runTest {
        dao.insert(msg(1, "hello", "alpha", timestamp = 1000))
        val result = dao.getPageBySession("nonexistent", limit = 50, offset = 0)
        assertTrue(result.isEmpty())
    }

    @Test
    fun getPageBySession_sameTimestampOrdersByIdDesc() = runTest {
        // Two messages with identical timestamp — order by id DESC
        dao.insert(msg(1, "first-inserted", "s1", timestamp = 1000))
        dao.insert(msg(2, "second-inserted", "s1", timestamp = 1000))

        val page = dao.getPageBySession("s1", limit = 50, offset = 0)
        assertEquals("second-inserted", page[0].text) // higher id first
        assertEquals("first-inserted", page[1].text)
    }

    // ==================== countBySession ====================

    @Test
    fun countBySession_countsCorrectly() = runTest {
        dao.insert(msg(1, "a", "alpha", timestamp = 1000))
        dao.insert(msg(2, "b", "beta", timestamp = 2000))
        dao.insert(msg(3, "c", "alpha", timestamp = 3000))
        dao.insert(msg(4, "d", "alpha", timestamp = 4000))

        assertEquals(3, dao.countBySession("alpha"))
        assertEquals(1, dao.countBySession("beta"))
        assertEquals(0, dao.countBySession("gamma"))
    }

    @Test
    fun countBySession_zeroOnEmptyDb() = runTest {
        assertEquals(0, dao.countBySession("any"))
    }

    // ==================== getDistinctSessions ====================

    @Test
    fun getDistinctSessions_returnsUniqueNonEmpty() = runTest {
        dao.insert(msg(1, "a", "beta", timestamp = 1000))
        dao.insert(msg(2, "b", "alpha", timestamp = 2000))
        dao.insert(msg(3, "c", "beta", timestamp = 3000))  // duplicate session
        dao.insert(msg(4, "d", "", timestamp = 4000))       // empty session excluded
        dao.insert(msg(5, "e", "gamma", timestamp = 5000))

        val sessions = dao.getDistinctSessions()
        assertEquals(listOf("alpha", "beta", "gamma"), sessions) // sorted ASC
    }

    @Test
    fun getDistinctSessions_emptyWhenAllBlank() = runTest {
        dao.insert(msg(1, "a", "", timestamp = 1000))
        dao.insert(msg(2, "b", "", timestamp = 2000))

        assertTrue(dao.getDistinctSessions().isEmpty())
    }

    @Test
    fun getDistinctSessions_emptyOnEmptyDb() = runTest {
        assertTrue(dao.getDistinctSessions().isEmpty())
    }

    // ==================== Cross-cutting: mixed session operations ====================

    @Test
    fun mixedSessionsPaginationAndCounting() = runTest {
        // Insert interleaved messages across 3 sessions
        dao.insert(msg(1, "a1", "A", timestamp = 100))
        dao.insert(msg(2, "b1", "B", timestamp = 200))
        dao.insert(msg(3, "a2", "A", timestamp = 300))
        dao.insert(msg(4, "c1", "C", timestamp = 400))
        dao.insert(msg(5, "b2", "B", timestamp = 500))
        dao.insert(msg(6, "a3", "A", timestamp = 600))
        dao.insert(msg(7, "c2", "C", timestamp = 700))
        dao.insert(msg(8, "b3", "B", timestamp = 800))

        // Counts
        assertEquals(3, dao.countBySession("A"))
        assertEquals(3, dao.countBySession("B"))
        assertEquals(2, dao.countBySession("C"))
        assertEquals(8, dao.count())

        // Session A, paginated by 2
        val a1 = dao.getPageBySession("A", 2, 0)
        assertEquals(listOf("a3", "a2"), a1.map { it.text })
        val a2 = dao.getPageBySession("A", 2, 2)
        assertEquals(listOf("a1"), a2.map { it.text })

        // All sessions sorted ASC
        assertEquals(listOf("A", "B", "C"), dao.getDistinctSessions())

        // Global page still works
        val globalPage = dao.getPage(3, 0)
        assertEquals(3, globalPage.size)
        assertEquals(listOf("b3", "c2", "a3"), globalPage.map { it.text })
    }

    // ==================== upsertByMessageId ====================

    @Test
    fun upsertByMessageId_updatesExisting() = runTest {
        dao.insert(msg(42, "original", "s1", timestamp = 1000))

        val updated = msg(42, "updated-text", "s1-new", timestamp = 2000)
        dao.upsertByMessageId(updated)

        val result = dao.findByMessageId(42)
        assertNotNull(result)
        assertEquals("updated-text", result!!.text)
        assertEquals("s1-new", result.session)
    }

    @Test
    fun upsertByMessageId_insertsWhenNew() = runTest {
        val newMsg = msg(99, "brand-new", "s1", timestamp = 1000)
        dao.upsertByMessageId(newMsg)

        val result = dao.findByMessageId(99)
        assertNotNull(result)
        assertEquals("brand-new", result!!.text)
    }

    // ==================== deleteAll ====================

    @Test
    fun deleteAll_clearsEverything() = runTest {
        dao.insert(msg(1, "a", "s1", timestamp = 1000))
        dao.insert(msg(2, "b", "s2", timestamp = 2000))
        assertEquals(2, dao.count())

        dao.deleteAll()
        assertEquals(0, dao.count())
        assertTrue(dao.getDistinctSessions().isEmpty())
    }
}
