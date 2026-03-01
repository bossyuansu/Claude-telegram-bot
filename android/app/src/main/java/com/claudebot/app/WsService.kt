package com.claudebot.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.IBinder

class WsService : Service() {

    companion object {
        const val CHANNEL_ID = "ws_service"
        const val NOTIFICATION_ID = 1
        const val EXTRA_STATUS = "status"
    }

    override fun onCreate() {
        super.onCreate()
        val channel = NotificationChannel(
            CHANNEL_ID,
            "Bot Activity",
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "Shown while the bot is working on a task"
            setShowBadge(false)
        }
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val status = intent?.getStringExtra(EXTRA_STATUS) ?: "Working..."
        startForeground(NOTIFICATION_ID, buildNotification(status))
        return START_NOT_STICKY  // Don't restart if killed — will restart when bot becomes busy again
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun buildNotification(status: String): Notification {
        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("Claude Bot")
            .setContentText(status)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setOngoing(true)
            .build()
    }
}
