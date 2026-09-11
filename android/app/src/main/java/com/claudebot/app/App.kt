package com.claudebot.app

import android.app.Application
import android.os.Handler
import android.os.Looper
import com.claudebot.app.data.SettingsRepository
import java.net.HttpURLConnection
import java.net.URL
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.atomic.AtomicBoolean

class App : Application() {

    companion object {
        /** How long the main thread may be unresponsive before we call it a stall. */
        private const val STALL_THRESHOLD_MS = 5_000L
        /** Don't spam while a long stall persists — one report per window. */
        private const val REPORT_COOLDOWN_MS = 60_000L
    }

    override fun onCreate() {
        super.onCreate()
        val settings = SettingsRepository(this)

        val defaultHandler = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, throwable ->
            report(settings, "CRASH", throwable.stackTraceToString())
            defaultHandler?.uncaughtException(thread, throwable)
        }

        startMainThreadWatchdog(settings)
    }

    /**
     * Report a stalled UI thread.
     *
     * A freeze is invisible to the crash handler — an ANR never throws, so nothing was reported and
     * the cause had to be guessed at twice. /data/anr and dumpsys are both permission-denied to
     * Termux, and adbd is off, so the app is the only thing that can see this. A daemon thread
     * pings the main looper and, when the ping does not come back within STALL_THRESHOLD_MS, posts
     * the main thread's stack — which names whatever is blocking it.
     */
    private fun startMainThreadWatchdog(settings: SettingsRepository) {
        val mainHandler = Handler(Looper.getMainLooper())
        val watchdog = Thread {
            var lastReported = 0L
            while (!Thread.currentThread().isInterrupted) {
                try {
                    val responded = AtomicBoolean(false)
                    mainHandler.post { responded.set(true) }
                    Thread.sleep(STALL_THRESHOLD_MS)
                    if (responded.get()) continue

                    val now = System.currentTimeMillis()
                    if (now - lastReported < REPORT_COOLDOWN_MS) continue
                    lastReported = now

                    val mainThread = Looper.getMainLooper().thread
                    val stack = mainThread.stackTrace.joinToString("\n") { "        at $it" }
                    report(
                        settings, "ANR",
                        "Main thread unresponsive for >${STALL_THRESHOLD_MS}ms\n" +
                            "state=${mainThread.state}\n$stack"
                    )
                } catch (_: InterruptedException) {
                    return@Thread
                } catch (_: Throwable) {
                    // A diagnostic must never be the thing that breaks the app.
                }
            }
        }
        watchdog.name = "main-thread-watchdog"
        watchdog.isDaemon = true
        watchdog.priority = Thread.MIN_PRIORITY
        watchdog.start()
    }

    /** POST a diagnostic to the bot, tagged with which build produced it. */
    private fun report(settings: SettingsRepository, kind: String, detail: String) {
        Thread {
            try {
                val conn = URL("http://${settings.host}:${settings.port}/api/crash")
                    .openConnection() as HttpURLConnection
                conn.requestMethod = "POST"
                conn.setRequestProperty("Content-Type", "text/plain")
                conn.doOutput = true
                conn.connectTimeout = 3000
                conn.readTimeout = 3000
                conn.outputStream.use { it.write("[$kind] ${buildInfo()}\n$detail".toByteArray()) }
                conn.responseCode
            } catch (_: Throwable) {
            }
        }.apply { isDaemon = true }.start()
    }

    /**
     * Identifies the running build.
     *
     * versionName is a fixed "1.0", so it cannot distinguish one debug build from the next — which
     * is why "is the fix even installed?" kept being unanswerable. lastUpdateTime changes on every
     * install and does answer it.
     */
    private fun buildInfo(): String = try {
        val pkg = packageManager.getPackageInfo(packageName, 0)
        val installed = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US)
            .format(Date(pkg.lastUpdateTime))
        "build installed=$installed versionName=${pkg.versionName}"
    } catch (_: Throwable) {
        "build unknown"
    }
}
