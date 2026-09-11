package com.claudebot.app

import android.app.Application
import android.os.Handler
import android.os.Looper
import com.claudebot.app.data.SettingsRepository
import java.io.File
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
        private const val PENDING_DIR = "pending-reports"
    }

    override fun onCreate() {
        super.onCreate()
        val settings = SettingsRepository(this)

        val defaultHandler = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, throwable ->
            report(settings, "CRASH", throwable.stackTraceToString())
            defaultHandler?.uncaughtException(thread, throwable)
        }

        // Anything a previous run could not send — because the device was wedged, the network was
        // down, or the process was killed — goes out now.
        flushPendingReports(settings)

        // One line per launch. Without it there is no way to tell WHICH build is running, and a
        // diagnosis built on the wrong build is worthless. Frequent HELLOs also mean the app is
        // being killed and restarted, which is itself the symptom.
        report(settings, "HELLO", "app started")

        startMainThreadWatchdog(settings)
    }

    /**
     * Report a stalled UI thread.
     *
     * A freeze never throws, so the crash handler cannot see it, and /data/anr and dumpsys are
     * permission-denied to Termux while adbd is off — the app is the only thing that can observe
     * this. A daemon pings the main looper; if the ping does not return within STALL_THRESHOLD_MS
     * it captures the main thread's stack, which names the blocker.
     *
     * The trace is written to disk FIRST. A device-wide freeze can starve this thread and kill
     * networking, which is exactly when a POST-only report is lost — and losing it is how the last
     * lock-up produced no evidence at all. On disk it survives to the next launch.
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

    /** Persist to disk, then try to send. Disk first so nothing is lost if the send cannot run. */
    private fun report(settings: SettingsRepository, kind: String, detail: String) {
        val body = "[$kind] ${buildInfo()} at ${timestamp()}\n$detail"
        val file = try {
            File(filesDir, PENDING_DIR).apply { mkdirs() }
                .resolve("${System.currentTimeMillis()}-$kind.txt")
                .apply { writeText(body) }
        } catch (_: Throwable) {
            null
        }
        Thread {
            if (post(settings, body)) file?.delete()
        }.apply { isDaemon = true }.start()
    }

    /** Send reports a previous run wrote but could not deliver. */
    private fun flushPendingReports(settings: SettingsRepository) {
        Thread {
            try {
                val dir = File(filesDir, PENDING_DIR)
                dir.listFiles()?.sortedBy { it.name }?.forEach { f ->
                    // Don't let a stuck backlog grow without bound.
                    if (System.currentTimeMillis() - f.lastModified() > 7L * 24 * 3600 * 1000) {
                        f.delete()
                    } else if (post(settings, f.readText())) {
                        f.delete()
                    }
                }
            } catch (_: Throwable) {
            }
        }.apply { isDaemon = true }.start()
    }

    private fun post(settings: SettingsRepository, body: String): Boolean = try {
        val conn = URL("http://${settings.host}:${settings.port}/api/crash")
            .openConnection() as HttpURLConnection
        conn.requestMethod = "POST"
        conn.setRequestProperty("Content-Type", "text/plain")
        conn.doOutput = true
        conn.connectTimeout = 3000
        conn.readTimeout = 3000
        conn.outputStream.use { it.write(body.toByteArray()) }
        conn.responseCode in 200..299
    } catch (_: Throwable) {
        false
    }

    private fun timestamp(): String =
        SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(Date())

    /**
     * Identifies the running build. versionName is a fixed "1.0" and cannot tell one debug build
     * from the next; lastUpdateTime changes on every install and can.
     */
    private fun buildInfo(): String = try {
        val pkg = packageManager.getPackageInfo(packageName, 0)
        "build=" + SimpleDateFormat("MM-dd HH:mm", Locale.US).format(Date(pkg.lastUpdateTime))
    } catch (_: Throwable) {
        "build=unknown"
    }
}
