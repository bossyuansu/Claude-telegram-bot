package com.claudebot.app

import android.app.Application
import com.claudebot.app.data.SettingsRepository
import java.net.HttpURLConnection
import java.net.URL

class App : Application() {
    override fun onCreate() {
        super.onCreate()
        val settings = SettingsRepository(this)
        val defaultHandler = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, throwable ->
            val trace = throwable.stackTraceToString()
            try {
                val crashUrl = "http://${settings.host}:${settings.port}/api/crash"
                val conn = URL(crashUrl).openConnection() as HttpURLConnection
                conn.requestMethod = "POST"
                conn.setRequestProperty("Content-Type", "text/plain")
                conn.doOutput = true
                conn.connectTimeout = 3000
                conn.readTimeout = 3000
                conn.outputStream.use { it.write(trace.toByteArray()) }
                conn.responseCode
            } catch (_: Exception) {}
            defaultHandler?.uncaughtException(thread, throwable)
        }
    }
}
