package com.jarvis.hud

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.view.Gravity
import android.view.WindowManager
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.PermissionRequest
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast

class MainActivity : Activity() {

    companion object {
        const val PREFS_NAME = "jarvis_hud_prefs"
        const val KEY_SERVER_URL = "server_url"
        const val KEY_ACCESS_KEY = "access_key"
        const val EXTRA_WAKE_VOICE = "extra_wake_voice"
        const val EXTRA_WAKE_CMD = "extra_wake_cmd"
        private const val REQ_PERMISSIONS = 1001
    }

    private lateinit var rootLayout: FrameLayout
    private var webView: WebView? = null
    private var setupOverlay: LinearLayout? = null
    private var pendingWakeVoice: Boolean = true
    private var pendingWakeCmd: String = ""

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Allow waking over lock screen and keeping screen bright
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        }
        @Suppress("DEPRECATION")
        window.addFlags(
            WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON or
                WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
                WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
        )

        rootLayout = FrameLayout(this).apply {
            setBackgroundColor(Color.parseColor("#04060A"))
        }
        setContentView(rootLayout)

        handleIncomingIntent(intent, isColdStart = true)
        requestNecessaryPermissions()
    }

    override fun onNewIntent(intent: Intent?) {
        super.onNewIntent(intent)
        if (intent != null) {
            setIntent(intent)
            handleIncomingIntent(intent, isColdStart = false)
        }
    }

    private fun handleIncomingIntent(intent: Intent, isColdStart: Boolean) {
        // Cancel any wake alert notification
        try {
            val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            nm.cancel(WakeWordService.ALERT_NOTIF_ID)
        } catch (_: Exception) {
        }

        // Check for jarvis://connect?url=...&key=... deep-link pairing from browser
        val data: Uri? = intent.data
        if (data != null && data.scheme == "jarvis" && data.host == "connect") {
            val urlParam = data.getQueryParameter("url")?.trim()?.trimEnd('/') ?: ""
            val keyParam = data.getQueryParameter("key")?.trim() ?: "JARVIS"
            if (urlParam.startsWith("http://") || urlParam.startsWith("https://")) {
                saveConfig(urlParam, keyParam.ifEmpty { "JARVIS" })
                Toast.makeText(
                    this,
                    "JARVIS Uplink Paired: $urlParam",
                    Toast.LENGTH_SHORT
                ).show()
            }
        }

        val isAssistIntent = intent.action == Intent.ACTION_ASSIST ||
            intent.action == Intent.ACTION_VOICE_COMMAND ||
            intent.action == "android.speech.action.WEB_SEARCH"

        val wantWake = intent.getBooleanExtra(EXTRA_WAKE_VOICE, false) || isAssistIntent || isColdStart
        val cmd = intent.getStringExtra(EXTRA_WAKE_CMD) ?: ""

        pendingWakeVoice = wantWake
        pendingWakeCmd = cmd

        val savedUrl = getSavedServerUrl()
        if (savedUrl.isEmpty()) {
            showSetupOverlay()
        } else {
            hideSetupOverlay()
            ensureWebViewLoaded(savedUrl, getSavedAccessKey(), wantWake, cmd, isColdStart)
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun ensureWebViewLoaded(
        baseUrl: String,
        accessKey: String,
        activateVoice: Boolean,
        command: String,
        isColdStart: Boolean
    ) {
        // Pause background wake recognizer before WebView claims microphone
        notifyServiceMicBusy(true)

        if (webView == null) {
            val wv = WebView(this).apply {
                setBackgroundColor(Color.parseColor("#04060A"))
                settings.apply {
                    javaScriptEnabled = true
                    domStorageEnabled = true
                    databaseEnabled = true
                    mediaPlaybackRequiresUserGesture = false
                    allowFileAccess = true
                    allowContentAccess = true
                    cacheMode = WebSettings.LOAD_DEFAULT
                    mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
                    userAgentString = "$userAgentString JarvisAndroidAPK/48.1"
                }

                CookieManager.getInstance().setAcceptCookie(true)
                CookieManager.getInstance().setAcceptThirdPartyCookies(this, true)

                addJavascriptInterface(JarvisJsBridge(), "JarvisAndroid")

                webChromeClient = object : WebChromeClient() {
                    override fun onPermissionRequest(request: PermissionRequest?) {
                        request?.grant(request.resources)
                    }
                }

                webViewClient = object : WebViewClient() {
                    override fun shouldOverrideUrlLoading(
                        view: WebView?,
                        request: WebResourceRequest?
                    ): Boolean {
                        val u = request?.url ?: return false
                        if (u.scheme == "jarvis") {
                            return true
                        }
                        return false
                    }

                    override fun onPageFinished(view: WebView?, url: String?) {
                        super.onPageFinished(view, url)
                        // Inject hook so doMic() and _stopVoice() automatically coordinate with WakeWordService
                        val bridgeScript = """
                            (function() {
                              if (window.__jarvisApkHooked) return;
                              window.__jarvisApkHooked = true;
                              setInterval(function() {
                                try {
                                  var busy = Boolean(window._voiceWs);
                                  if (window.__lastApkMicBusy !== busy) {
                                    window.__lastApkMicBusy = busy;
                                    if (window.JarvisAndroid && window.JarvisAndroid.setMicBusy) {
                                      window.JarvisAndroid.setMicBusy(busy);
                                    }
                                  }
                                } catch(e) {}
                              }, 500);
                            })();
                        """.trimIndent()
                        view?.evaluateJavascript(bridgeScript, null)

                        if (pendingWakeVoice) {
                            pendingWakeVoice = false
                            val safeCmd = pendingWakeCmd.replace("'", "\\'")
                            pendingWakeCmd = ""
                            view?.postDelayed({
                                view.evaluateJavascript(
                                    "if (typeof _triggerWakeActivation === 'function') { _triggerWakeActivation('$safeCmd'); } else if (typeof doMic === 'function' && !window._voiceWs) { doMic(); }",
                                    null
                                )
                            }, 450L)
                        }
                    }
                }
            }

            webView = wv
            rootLayout.addView(
                wv,
                FrameLayout.LayoutParams(
                    FrameLayout.LayoutParams.MATCH_PARENT,
                    FrameLayout.LayoutParams.MATCH_PARENT
                )
            )

            val launchUrl = buildAutoLoginUrl(baseUrl, accessKey, activateVoice, command)
            wv.loadUrl(launchUrl)
        } else if (!isColdStart && activateVoice) {
            val safeCmd = command.replace("'", "\\'")
            webView?.postDelayed({
                webView?.evaluateJavascript(
                    "if (typeof _triggerWakeActivation === 'function') { _triggerWakeActivation('$safeCmd'); } else if (typeof doMic === 'function' && !window._voiceWs) { doMic(); }",
                    null
                )
            }, 250L)
        }
    }

    private fun buildAutoLoginUrl(
        baseUrl: String,
        accessKey: String,
        activateVoice: Boolean,
        command: String
    ): String {
        val cleanBase = baseUrl.trim().trimEnd('/')
        val uriBuilder = Uri.parse("$cleanBase/auto-login").buildUpon()
            .appendQueryParameter("key", accessKey.ifEmpty { "JARVIS" })
        if (activateVoice) {
            uriBuilder.appendQueryParameter("voice", "1")
        }
        if (command.isNotBlank()) {
            uriBuilder.appendQueryParameter("cmd", command)
        }
        return uriBuilder.build().toString()
    }

    private fun showSetupOverlay() {
        if (setupOverlay != null) return

        val pad = (28 * resources.displayMetrics.density).toInt()
        val container = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setBackgroundColor(Color.parseColor("#04060A"))
            setPadding(pad, pad, pad, pad)
        }

        val title = TextView(this).apply {
            text = "J.A.R.V.I.S // MK-XLVIII"
            textSize = 24f
            setTextColor(Color.parseColor("#00F0FF"))
            gravity = Gravity.CENTER
        }

        val subtitle = TextView(this).apply {
            text = "NATIVE ANDROID WAKE SENTINEL — UPLINK SETUP"
            textSize = 11f
            setTextColor(Color.parseColor("#FF2A3C"))
            gravity = Gravity.CENTER
            setPadding(0, 8, 0, 36)
        }

        val desc = TextView(this).apply {
            text = "Enter your Dokploy / Cloud JARVIS Server URL once. The Always-On Wake Sentinel will run in the background and launch this HUD whenever you say \"Jarvis wake up\"."
            textSize = 13f
            setTextColor(Color.parseColor("#93A8C1"))
            gravity = Gravity.CENTER
            setPadding(0, 0, 0, 28)
        }

        val urlInput = EditText(this).apply {
            hint = "https://your-jarvis-domain.com"
            setHintTextColor(Color.parseColor("#546880"))
            setTextColor(Color.parseColor("#F4F8FC"))
            setBackgroundColor(Color.parseColor("#0A121E"))
            setPadding(32, 28, 32, 28)
            setSingleLine(true)
            setText(getSavedServerUrl())
        }

        val keyInput = EditText(this).apply {
            hint = "Access Key (default: JARVIS)"
            setHintTextColor(Color.parseColor("#546880"))
            setTextColor(Color.parseColor("#F4F8FC"))
            setBackgroundColor(Color.parseColor("#0A121E"))
            setPadding(32, 28, 32, 28)
            setSingleLine(true)
            setText(getSavedAccessKey().ifEmpty { "JARVIS" })
        }

        val spacer = TextView(this).apply { height = 24 }
        val spacer2 = TextView(this).apply { height = 32 }

        val connectBtn = Button(this).apply {
            text = "ARM SENTINEL & CONNECT HUD"
            setBackgroundColor(Color.parseColor("#00F0FF"))
            setTextColor(Color.parseColor("#04060A"))
            setPadding(32, 28, 32, 28)
            setOnClickListener {
                var rawUrl = urlInput.text.toString().trim().trimEnd('/')
                val rawKey = keyInput.text.toString().trim().ifEmpty { "JARVIS" }
                if (rawUrl.isEmpty()) {
                    Toast.makeText(
                        this@MainActivity,
                        "Please enter your Dokploy Server URL",
                        Toast.LENGTH_SHORT
                    ).show()
                    return@setOnClickListener
                }
                if (!rawUrl.startsWith("http://") && !rawUrl.startsWith("https://")) {
                    rawUrl = "https://$rawUrl"
                }
                saveConfig(rawUrl, rawKey)
                hideSetupOverlay()
                startWakeSentinelService()
                ensureWebViewLoaded(rawUrl, rawKey, activateVoice = true, command = "", isColdStart = true)
            }
        }

        container.addView(title)
        container.addView(subtitle)
        container.addView(desc)
        container.addView(urlInput)
        container.addView(spacer)
        container.addView(keyInput)
        container.addView(spacer2)
        container.addView(connectBtn)

        setupOverlay = container
        rootLayout.addView(
            container,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT
            )
        )
    }

    private fun hideSetupOverlay() {
        setupOverlay?.let {
            rootLayout.removeView(it)
            setupOverlay = null
        }
    }

    private fun getSavedServerUrl(): String {
        val prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        return prefs.getString(KEY_SERVER_URL, "") ?: ""
    }

    private fun getSavedAccessKey(): String {
        val prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        return prefs.getString(KEY_ACCESS_KEY, "JARVIS") ?: "JARVIS"
    }

    private fun saveConfig(url: String, key: String) {
        getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_SERVER_URL, url)
            .putString(KEY_ACCESS_KEY, key)
            .apply()
    }

    private fun requestNecessaryPermissions() {
        val needed = mutableListOf(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            needed.add(Manifest.permission.POST_NOTIFICATIONS)
        }
        val missing = needed.filter {
            checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isNotEmpty()) {
            requestPermissions(missing.toTypedArray(), REQ_PERMISSIONS)
        } else {
            startWakeSentinelService()
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQ_PERMISSIONS) {
            startWakeSentinelService()
        }
    }

    private fun startWakeSentinelService() {
        try {
            val svcIntent = Intent(this, WakeWordService::class.java).apply {
                action = WakeWordService.ACTION_START
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                startForegroundService(svcIntent)
            } else {
                startService(svcIntent)
            }
        } catch (_: Exception) {
        }
    }

    private fun notifyServiceMicBusy(busy: Boolean) {
        try {
            val svcIntent = Intent(this, WakeWordService::class.java).apply {
                action = if (busy) WakeWordService.ACTION_MIC_BUSY else WakeWordService.ACTION_MIC_FREE
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                startForegroundService(svcIntent)
            } else {
                startService(svcIntent)
            }
        } catch (_: Exception) {
        }
    }

    override fun onStop() {
        super.onStop()
        // When user minimizes app or locks screen, let the background WakeWordService listen for "Jarvis wake up"
        webView?.evaluateJavascript(
            "if (typeof _stopVoice === 'function' && window._voiceWs) { _stopVoice(); }",
            null
        )
        notifyServiceMicBusy(false)
    }

    inner class JarvisJsBridge {
        @JavascriptInterface
        fun setMicBusy(busy: Boolean) {
            runOnUiThread {
                notifyServiceMicBusy(busy)
            }
        }

        @JavascriptInterface
        fun openServerSetup() {
            runOnUiThread {
                showSetupOverlay()
            }
        }
    }
}
