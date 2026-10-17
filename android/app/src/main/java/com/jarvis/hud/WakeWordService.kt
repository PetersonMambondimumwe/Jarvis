package com.jarvis.hud

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.media.AudioManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import java.util.Locale

class WakeWordService : Service() {

    companion object {
        const val CHANNEL_ID = "jarvis_wake_sentinel_channel"
        const val ALERT_CHANNEL_ID = "jarvis_wake_alert_channel"
        const val NOTIF_ID = 4801
        const val ALERT_NOTIF_ID = 4802

        const val ACTION_START = "com.jarvis.hud.ACTION_START"
        const val ACTION_STOP = "com.jarvis.hud.ACTION_STOP"
        const val ACTION_TOGGLE_PAUSE = "com.jarvis.hud.ACTION_TOGGLE_PAUSE"
        const val ACTION_MIC_BUSY = "com.jarvis.hud.ACTION_MIC_BUSY"
        const val ACTION_MIC_FREE = "com.jarvis.hud.ACTION_MIC_FREE"
        const val ACTION_WAKE_NOW = "com.jarvis.hud.ACTION_WAKE_NOW"

        @Volatile
        var isMicBusy: Boolean = false

        @Volatile
        var isPausedByUser: Boolean = false
    }

    private val mainHandler = Handler(Looper.getMainLooper())
    private var speechRecognizer: SpeechRecognizer? = null
    private var isListening: Boolean = false
    private var cpuWakeLock: PowerManager.WakeLock? = null
    private var lastTriggerTime: Long = 0L

    private val restartRunnable = Runnable {
        startListeningLoop()
    }

    override fun onCreate() {
        super.onCreate()
        createNotificationChannels()
        acquireCpuWakeLock()
        startForeground(NOTIF_ID, buildStatusNotification("ARMED — Say \"Jarvis wake up\""))
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopListeningLoop()
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_TOGGLE_PAUSE -> {
                isPausedByUser = !isPausedByUser
                if (isPausedByUser) {
                    stopListeningLoop()
                    updateNotification("PAUSED — Tap Resume to arm sentinel")
                } else {
                    updateNotification("ARMED — Say \"Jarvis wake up\"")
                    scheduleRestart(300L)
                }
            }
            ACTION_MIC_BUSY -> {
                isMicBusy = true
                stopListeningLoop()
                updateNotification("LIVE VOICE ACTIVE — Sentinel standing by")
            }
            ACTION_MIC_FREE -> {
                isMicBusy = false
                if (!isPausedByUser) {
                    updateNotification("ARMED — Say \"Jarvis wake up\"")
                    scheduleRestart(600L)
                }
            }
            ACTION_WAKE_NOW -> {
                triggerJarvisLaunch("")
            }
            else -> {
                if (!isMicBusy && !isPausedByUser) {
                    scheduleRestart(400L)
                }
            }
        }
        return START_STICKY
    }

    private fun acquireCpuWakeLock() {
        try {
            val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
            cpuWakeLock = pm.newWakeLock(
                PowerManager.PARTIAL_WAKE_LOCK,
                "JarvisHUD::WakeSentinelLock"
            ).apply {
                setReferenceCounted(false)
                acquire()
            }
        } catch (_: Exception) {
        }
    }

    private fun scheduleRestart(delayMs: Long) {
        mainHandler.removeCallbacks(restartRunnable)
        if (isMicBusy || isPausedByUser) return
        mainHandler.postDelayed(restartRunnable, delayMs)
    }

    private fun startListeningLoop() {
        if (isMicBusy || isPausedByUser || isListening) return
        if (!SpeechRecognizer.isRecognitionAvailable(this)) {
            updateNotification("Speech Recognition Service Unavailable")
            return
        }

        try {
            if (speechRecognizer == null) {
                speechRecognizer = SpeechRecognizer.createSpeechRecognizer(this).apply {
                    setRecognitionListener(recognitionListener)
                }
            }

            val recIntent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
                putExtra(
                    RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                    RecognizerIntent.LANGUAGE_MODEL_FREE_FORM
                )
                putExtra(RecognizerIntent.EXTRA_LANGUAGE, Locale.getDefault())
                putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
                putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 3)
                putExtra(
                    RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS,
                    8000L
                )
                putExtra(
                    RecognizerIntent.EXTRA_SPEECH_INPUT_POSSIBLY_COMPLETE_SILENCE_LENGTH_MILLIS,
                    5000L
                )
            }

            isListening = true
            speechRecognizer?.startListening(recIntent)
        } catch (_: Exception) {
            isListening = false
            destroyRecognizer()
            scheduleRestart(1500L)
        }
    }

    private fun stopListeningLoop() {
        mainHandler.removeCallbacks(restartRunnable)
        isListening = false
        destroyRecognizer()
    }

    private fun destroyRecognizer() {
        try {
            speechRecognizer?.cancel()
            speechRecognizer?.destroy()
        } catch (_: Exception) {
        } finally {
            speechRecognizer = null
        }
    }

    private val recognitionListener = object : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) {}
        override fun onBeginningOfSpeech() {}
        override fun onRmsChanged(rmsdB: Float) {}
        override fun onBufferReceived(buffer: ByteArray?) {}

        override fun onEndOfSpeech() {
            isListening = false
        }

        override fun onError(error: Int) {
            isListening = false
            if (error == SpeechRecognizer.ERROR_RECOGNIZER_BUSY ||
                error == SpeechRecognizer.ERROR_CLIENT
            ) {
                destroyRecognizer()
            }
            val delay = when (error) {
                SpeechRecognizer.ERROR_NO_MATCH,
                SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> 250L
                SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> 1200L
                else -> 800L
            }
            scheduleRestart(delay)
        }

        override fun onResults(results: Bundle?) {
            isListening = false
            val matches = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
            if (!matches.isNullOrEmpty()) {
                for (phrase in matches) {
                    if (inspectTranscriptForWakeWord(phrase)) {
                        return
                    }
                }
            }
            scheduleRestart(200L)
        }

        override fun onPartialResults(partialResults: Bundle?) {
            val matches = partialResults?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
            if (!matches.isNullOrEmpty()) {
                for (phrase in matches) {
                    if (inspectTranscriptForWakeWord(phrase)) {
                        return
                    }
                }
            }
        }

        override fun onEvent(eventType: Int, params: Bundle?) {}
    }

    private fun inspectTranscriptForWakeWord(raw: String): Boolean {
        val text = raw.lowercase(Locale.ROOT).trim()
        if (text.isEmpty()) return false

        val wakePatterns = listOf(
            "jarvis wake up",
            "wake up jarvis",
            "hey jarvis",
            "hi jarvis",
            "ok jarvis",
            "okay jarvis",
            "yo jarvis",
            "jarvis online",
            "jarvis activate"
        )

        var matchedWake: String? = null
        for (pattern in wakePatterns) {
            if (text.contains(pattern)) {
                matchedWake = pattern
                break
            }
        }

        if (matchedWake == null && (text == "jarvis" || text.startsWith("jarvis "))) {
            matchedWake = "jarvis"
        }

        if (matchedWake != null) {
            val now = System.currentTimeMillis()
            if (now - lastTriggerTime < 3000L) return true
            lastTriggerTime = now

            val idx = text.indexOf(matchedWake)
            val trailingCmd = if (idx >= 0) {
                raw.substring(idx + matchedWake.length).trim().trimStart(',', '.', '!', '?')
            } else {
                ""
            }

            triggerJarvisLaunch(trailingCmd)
            return true
        }
        return false
    }

    private fun triggerJarvisLaunch(trailingCommand: String) {
        // Immediately release mic hardware so the WebView can claim it
        isMicBusy = true
        stopListeningLoop()
        updateNotification("WAKE DETECTED — Engaging Live HUD")

        // Wake the screen if locked/off
        try {
            val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
            @Suppress("DEPRECATION")
            val screenLock = pm.newWakeLock(
                PowerManager.SCREEN_BRIGHT_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP,
                "JarvisHUD::ScreenWake"
            )
            screenLock.acquire(6000L)
        } catch (_: Exception) {
        }

        val launchIntent = Intent(this, MainActivity::class.java).apply {
            addFlags(
                Intent.FLAG_ACTIVITY_NEW_TASK or
                    Intent.FLAG_ACTIVITY_SINGLE_TOP or
                    Intent.FLAG_ACTIVITY_CLEAR_TOP or
                    Intent.FLAG_ACTIVITY_REORDER_TO_FRONT
            )
            putExtra(MainActivity.EXTRA_WAKE_VOICE, true)
            if (trailingCommand.isNotBlank()) {
                putExtra(MainActivity.EXTRA_WAKE_CMD, trailingCommand)
            }
        }

        // Fire full-screen alert notification for Android 14/15 background launch compliance
        try {
            val fullScreenPi = PendingIntent.getActivity(
                this,
                99,
                launchIntent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            val alertNotif = Notification.Builder(this, ALERT_CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_jarvis_core)
                .setContentTitle("J.A.R.V.I.S. ONLINE")
                .setContentText(
                    if (trailingCommand.isNotBlank()) "Executing: $trailingCommand"
                    else "Live Voice Channel Open — Speak Now"
                )
                .setCategory(Notification.CATEGORY_CALL)
                .setFullScreenIntent(fullScreenPi, true)
                .setContentIntent(fullScreenPi)
                .setAutoCancel(true)
                .build()

            val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            nm.notify(ALERT_NOTIF_ID, alertNotif)
        } catch (_: Exception) {
        }

        try {
            startActivity(launchIntent)
        } catch (_: Exception) {
        }
    }

    private fun createNotificationChannels() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

            val statusChannel = NotificationChannel(
                CHANNEL_ID,
                "JARVIS Wake Sentinel",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Always-on background wake-word sentinel for JARVIS"
                setShowBadge(false)
            }
            nm.createNotificationChannel(statusChannel)

            val alertChannel = NotificationChannel(
                ALERT_CHANNEL_ID,
                "JARVIS Voice Activation",
                NotificationManager.IMPORTANCE_HIGH
            ).apply {
                description = "Wakes screen and opens JARVIS HUD when wake word is heard"
                enableVibration(true)
            }
            nm.createNotificationChannel(alertChannel)
        }
    }

    private fun buildStatusNotification(statusText: String): Notification {
        val openIntent = Intent(this, MainActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            putExtra(MainActivity.EXTRA_WAKE_VOICE, true)
        }
        val openPi = PendingIntent.getActivity(
            this,
            1,
            openIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val toggleIntent = Intent(this, WakeWordService::class.java).apply {
            action = ACTION_TOGGLE_PAUSE
        }
        val togglePi = PendingIntent.getService(
            this,
            2,
            toggleIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val wakeNowIntent = Intent(this, WakeWordService::class.java).apply {
            action = ACTION_WAKE_NOW
        }
        val wakeNowPi = PendingIntent.getService(
            this,
            3,
            wakeNowIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val pauseLabel = if (isPausedByUser) "RESUME SENTINEL" else "PAUSE SENTINEL"

        return Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_jarvis_core)
            .setContentTitle("JARVIS MK-XLVIII // WAKE SENTINEL")
            .setContentText(statusText)
            .setOngoing(true)
            .setContentIntent(openPi)
            .addAction(
                Notification.Action.Builder(
                    R.drawable.ic_jarvis_core,
                    "WAKE JARVIS",
                    wakeNowPi
                ).build()
            )
            .addAction(
                Notification.Action.Builder(
                    R.drawable.ic_jarvis_core,
                    pauseLabel,
                    togglePi
                ).build()
            )
            .build()
    }

    private fun updateNotification(statusText: String) {
        try {
            val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            nm.notify(NOTIF_ID, buildStatusNotification(statusText))
        } catch (_: Exception) {
        }
    }

    override fun onDestroy() {
        stopListeningLoop()
        try {
            cpuWakeLock?.let {
                if (it.isHeld) it.release()
            }
        } catch (_: Exception) {
        }
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
