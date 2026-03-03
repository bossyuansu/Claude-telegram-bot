package com.claudebot.app.data

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.Query
import androidx.room.Transaction

@Dao
abstract class MessageDao {
    /** Load a page of messages, newest first. offset=0 gets the most recent [limit] messages. */
    @Query("SELECT * FROM messages ORDER BY timestamp DESC, id DESC LIMIT :limit OFFSET :offset")
    abstract suspend fun getPage(limit: Int, offset: Int): List<MessageEntity>

    @Query("SELECT COUNT(*) FROM messages")
    abstract suspend fun count(): Int

    @Query("SELECT * FROM messages WHERE session = :session ORDER BY timestamp DESC, id DESC LIMIT :limit OFFSET :offset")
    abstract suspend fun getPageBySession(session: String, limit: Int, offset: Int): List<MessageEntity>

    @Query("SELECT COUNT(*) FROM messages WHERE session = :session")
    abstract suspend fun countBySession(session: String): Int

    @Query("SELECT DISTINCT session FROM messages WHERE session != '' ORDER BY session ASC")
    abstract suspend fun getDistinctSessions(): List<String>

    @Query(
        """
        DELETE FROM messages
        WHERE messageId > 0
          AND id NOT IN (
              SELECT MAX(id) FROM messages
              WHERE messageId > 0
              GROUP BY messageId
          )
        """
    )
    abstract suspend fun deleteDuplicateMessageIds()

    @Insert
    abstract suspend fun insert(msg: MessageEntity): Long

    /** Insert or update by messageId. For bot messages that may be replayed via WS. */
    @Transaction
    open suspend fun upsertByMessageId(msg: MessageEntity) {
        if (msg.messageId > 0) {
            val existing = findByMessageId(msg.messageId)
            if (existing != null) {
                updateByMessageId(msg.messageId, msg.text, msg.session, msg.buttons)
                return
            }
        }
        insert(msg)
    }

    @Query("UPDATE messages SET text = :text, session = :session, buttons = :buttons WHERE messageId = :messageId AND messageId > 0")
    abstract suspend fun updateByMessageId(messageId: Int, text: String, session: String, buttons: String)

    @Query("SELECT * FROM messages WHERE messageId = :messageId AND messageId > 0 ORDER BY id DESC LIMIT 1")
    abstract suspend fun findByMessageId(messageId: Int): MessageEntity?

    @Query("SELECT DISTINCT messageId FROM messages WHERE messageId > 0")
    abstract suspend fun getPersistedMessageIds(): List<Int>

    @Query("SELECT DISTINCT messageId FROM messages WHERE messageId > 0 AND text LIKE '%' || :finalMarker || '%'")
    abstract suspend fun getFinalizedMessageIds(finalMarker: String): List<Int>

    @Query("SELECT * FROM messages WHERE text LIKE '%' || :query || '%' ORDER BY timestamp DESC, id DESC LIMIT :limit OFFSET :offset")
    abstract suspend fun search(query: String, limit: Int, offset: Int): List<MessageEntity>

    @Query("SELECT COUNT(*) FROM messages WHERE text LIKE '%' || :query || '%'")
    abstract suspend fun searchCount(query: String): Int

    @Query("DELETE FROM messages")
    abstract suspend fun deleteAll()
}
