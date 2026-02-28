package com.claudebot.app.data

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.Query
import androidx.room.Update

@Dao
interface MessageDao {
    /** Load a page of messages, newest first. offset=0 gets the most recent [limit] messages. */
    @Query("SELECT * FROM messages ORDER BY timestamp DESC, id DESC LIMIT :limit OFFSET :offset")
    suspend fun getPage(limit: Int, offset: Int): List<MessageEntity>

    @Query("SELECT COUNT(*) FROM messages")
    suspend fun count(): Int

    @Insert
    suspend fun insert(msg: MessageEntity): Long

    @Query("UPDATE messages SET text = :text, session = :session, buttons = :buttons WHERE messageId = :messageId AND messageId != 0")
    suspend fun updateByMessageId(messageId: Int, text: String, session: String, buttons: String)

    @Query("SELECT * FROM messages WHERE messageId = :messageId AND messageId != 0 ORDER BY id DESC LIMIT 1")
    suspend fun findByMessageId(messageId: Int): MessageEntity?

    @Query("SELECT * FROM messages WHERE text LIKE '%' || :query || '%' ORDER BY timestamp DESC, id DESC LIMIT :limit OFFSET :offset")
    suspend fun search(query: String, limit: Int, offset: Int): List<MessageEntity>

    @Query("SELECT COUNT(*) FROM messages WHERE text LIKE '%' || :query || '%'")
    suspend fun searchCount(query: String): Int

    @Query("DELETE FROM messages")
    suspend fun deleteAll()
}
