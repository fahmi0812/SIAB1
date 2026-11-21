package id.siab1.kiosk.ui

import android.annotation.SuppressLint
import android.app.ActivityManager
import android.app.AlertDialog
import android.content.Intent
import android.graphics.BitmapFactory
import android.os.Build
import android.os.Bundle
import android.os.CountDownTimer
import android.view.KeyEvent
import android.view.View
import android.view.WindowManager
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebStorage
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.ImageView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import id.siab1.kiosk.AppConfig
import id.siab1.kiosk.R
import id.siab1.kiosk.databinding.ActivityExamBinding
import id.siab1.kiosk.kiosk.KioskController
import id.siab1.kiosk.kiosk.LockState
import id.siab1.kiosk.net.ApiClient
import id.siab1.kiosk.util.Prefs
import id.siab1.kiosk.util.SignatureUtil
import id.siab1.kiosk.web.Siab1Bridge
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.Executors

class ExamActivity : AppCompatActivity() {
    private companion object {
        const val EXAM_SUBMITTED_EXIT_DELAY_MS = 30_000L
        const val COUNTDOWN_TICK_MS = 1_000L
        const val BLANK_PAGE_URL = "about:blank"
    }

    private lateinit var binding: ActivityExamBinding
    private lateinit var kiosk: KioskController
    private val io = Executors.newSingleThreadExecutor()
    private var kioskWarningAcknowledged = false
    private var kioskWarningDialog: AlertDialog? = null
    private var examSubmittedDialog: AlertDialog? = null
    private var examExitTimer: CountDownTimer? = null
    private var lockActivationInProgress = false
    private var examClosed = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityExamBinding.inflate(layoutInflater)
        setContentView(binding.root)
        kiosk = KioskController(this)
        onBackPressedDispatcher.addCallback(
            this,
            object : OnBackPressedCallback(true) {
                override fun handleOnBackPressed() {
                    if (!kiosk.examActive) {
                        finish()
                    }
                }
            },
        )
        setupWebView()
        loadDashboard()
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun setupWebView() {
        val webView = binding.examWebView
        val settings = webView.settings
        settings.javaScriptEnabled = true
        settings.domStorageEnabled = true
        settings.databaseEnabled = true
        settings.allowFileAccess = false
        settings.allowContentAccess = false
        settings.mediaPlaybackRequiresUserGesture = false
        settings.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
        settings.cacheMode = WebSettings.LOAD_DEFAULT
        settings.userAgentString = ApiClient.USER_AGENT
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            settings.safeBrowsingEnabled = true
        }
        webView.addJavascriptInterface(
            Siab1Bridge { name, args -> runOnUiThread { dispatchHandler(name, args) } },
            "SIAB1Bridge",
        )
        val startScript = documentStartScript()
        if (WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
            WebViewCompat.addDocumentStartJavaScript(
                webView,
                startScript,
                setOf(AppConfig.trustedOrigin()),
            )
        }
        webView.webChromeClient = object : WebChromeClient() {
            override fun onProgressChanged(view: WebView?, newProgress: Int) {
                binding.examProgress.visibility = if (newProgress >= 100) View.GONE else View.VISIBLE
            }
        }
        webView.webViewClient = object : WebViewClient() {
            override fun onPageStarted(view: WebView?, url: String?, favicon: android.graphics.Bitmap?) {
                if (examClosed) {
                    // Let the post-submit purge blank the page. Anything else is
                    // still refused while the countdown is on screen.
                    if (url != BLANK_PAGE_URL) {
                        view?.stopLoading()
                    }
                    return
                }
                val mainUrl = view?.url
                val isMainFrame = url != null && mainUrl != null && urlsMatch(url, mainUrl)
                if (url != null && AppConfig.isTrustedUrl(url)) {
                    if (isMainFrame || mainUrl.isNullOrBlank() || AppConfig.isTrustedUrl(mainUrl)) {
                        view?.evaluateJavascript(startScript, null)
                    }
                    return
                }
                if (isMainFrame || mainUrl.isNullOrBlank() || !AppConfig.isTrustedUrl(mainUrl)) {
                    view?.stopLoading()
                }
            }

            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                if (examClosed) return true
                if (!request.isForMainFrame) return false
                var url = request.url.toString()
                if (AppConfig.forceHttps && url.startsWith("http://")) {
                    url = "https://" + url.removePrefix("http://")
                }
                if (!AppConfig.isTrustedUrl(url)) return true
                if (url != request.url.toString()) {
                    view.loadUrl(url, ApiClient.get().webViewHeaders())
                    return true
                }
                return false
            }
        }
    }

    private fun loadDashboard() {
        binding.examWebView.loadUrl(AppConfig.studentDashboardUrl(), ApiClient.get().webViewHeaders())
    }

    private fun documentStartScript(): String {
        val token = Prefs.token
        val userJson = Prefs.userJson
        val signature = SignatureUtil.sha256Fingerprint(this)
        val tokenJs = if (token.isNullOrBlank()) "null" else JSONObject.quote(token)
        val userJs = if (userJson.isNullOrBlank()) "null" else JSONObject.quote(userJson)
        val buildJs = JSONObject.quote(AppConfig.buildToken)
        val sigJs = if (signature == "ERROR") "null" else JSONObject.quote(signature)
        return """
            window.flutter_inappwebview = { callHandler: function(name) { var args = Array.prototype.slice.call(arguments,1); return window.SIAB1Bridge.post(name, JSON.stringify(args)); } };
            try {
              var token = $tokenJs;
              var userJson = $userJs;
              var buildToken = $buildJs;
              var appSig = $sigJs;
              if (token) { localStorage.setItem('access_token', token); }
              if (userJson) { localStorage.setItem('user', userJson); }
              localStorage.setItem('sxb_build_token', buildToken);
              if (appSig) { localStorage.setItem('sxb_app_signature', appSig); }
            } catch (e) {}
        """.trimIndent()
    }

    private fun dispatchHandler(name: String, args: JSONArray) {
        when (name) {
            "openImagePreview" -> openImagePreview(args)
            "securityHandler" -> { }
            "setSessionId" -> handleSetSessionId(args)
            "answerJournalEvent" -> handleAnswerJournal(args)
            "examStateUpdate" -> handleExamState(args)
            "timerSync" -> handleTimerSync(args)
            "examSubmitted" -> handleExamSubmitted()
            "logViolation" -> handleLogViolation(args)
            "userLogout" -> handleLogout()
            "forceKicked" -> handleForceKicked(args)
            "forceSubmit" -> handleForceSubmit(args)
            "examCancelled" -> handleExamCancelled(args)
        }
    }

    private fun handleSetSessionId(args: JSONArray) {
        if (args.length() == 0) return
        Prefs.sessionId = args.optString(0)
        if (args.length() > 1) {
            Prefs.examId = args.optString(1)
        }
        activateExamLock()
    }

    private fun activateExamLock() {
        lockActivationInProgress = true
        kiosk.startExamLock()
        binding.root.postDelayed({
            lockActivationInProgress = false
            if (kiosk.currentLockState() == LockState.NONE && !kioskWarningAcknowledged) {
                showKioskWarning()
            }
        }, 1200L)
    }

    private fun showKioskWarning() {
        if (isFinishing || kioskWarningDialog?.isShowing == true) return
        kioskWarningDialog = AlertDialog.Builder(this)
            .setTitle(R.string.kiosk_warning_title)
            .setMessage(R.string.kiosk_warning_body)
            .setCancelable(false)
            .setPositiveButton(R.string.retry) { _, _ -> activateExamLock() }
            .setNegativeButton(R.string.continue_limited_protection) { _, _ ->
                kioskWarningAcknowledged = true
                Toast.makeText(this, R.string.kiosk_limited_active, Toast.LENGTH_LONG).show()
            }
            .create()
        kioskWarningDialog?.show()
    }

    private fun handleAnswerJournal(args: JSONArray) {
        if (args.length() == 0 || Prefs.sessionId.isNullOrBlank()) return
        val payload = args.optJSONObject(0) ?: return
        val existing = Prefs.answerJournal
        val queue = try {
            if (existing.isNullOrBlank()) JSONArray() else JSONArray(existing)
        } catch (_: Exception) {
            JSONArray()
        }
        queue.put(payload)
        Prefs.answerJournal = queue.toString()
    }

    private fun handleExamState(args: JSONArray) {
        if (args.length() == 0) return
        val payload = args.optJSONObject(0) ?: return
        Prefs.examState = payload.toString()
    }

    private fun handleTimerSync(args: JSONArray) {
        if (args.length() == 0 || Prefs.sessionId.isNullOrBlank()) return
        val payload = args.optJSONObject(0) ?: return
        Prefs.timerSync = payload.toString()
    }

    private fun handleExamSubmitted() {
        if (examClosed || isFinishing || isDestroyed) return
        examClosed = true
        kiosk.stopExamLock()
        // stopExamLock() drops FLAG_SECURE. The countdown keeps this activity
        // visible for another 30s, so re-arm it here and only release it in
        // closeAfterExamSubmitted() to keep capture blocked the whole time.
        window.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        binding.examWebView.stopLoading()
        Prefs.clearAuth()
        showExamSubmittedCountdown()
    }

    private fun purgeExamWebContent() {
        // Prefs.clearAuth() only clears native storage. The rendered exam page
        // and its localStorage token stay in the WebView, so drop both before
        // the countdown dialog holds the activity open.
        val webView = binding.examWebView
        webView.loadUrl(BLANK_PAGE_URL)
        webView.clearHistory()
        webView.clearCache(true)
        WebStorage.getInstance().deleteAllData()
    }

    private fun showExamSubmittedCountdown() {
        purgeExamWebContent()
        val initialSeconds = (EXAM_SUBMITTED_EXIT_DELAY_MS / COUNTDOWN_TICK_MS).toInt()
        val dialog = AlertDialog.Builder(this)
            .setTitle(R.string.exam_submitted_title)
            .setMessage(
                resources.getQuantityString(
                    R.plurals.exam_submitted_countdown,
                    initialSeconds,
                    initialSeconds,
                ),
            )
            .setCancelable(false)
            .setPositiveButton(R.string.close_now) { _, _ -> closeAfterExamSubmitted() }
            .create()
        examSubmittedDialog = dialog
        dialog.show()

        examExitTimer = object : CountDownTimer(
            EXAM_SUBMITTED_EXIT_DELAY_MS,
            COUNTDOWN_TICK_MS,
        ) {
            override fun onTick(millisUntilFinished: Long) {
                val secondsRemaining = ((millisUntilFinished + 999L) / 1_000L).toInt()
                examSubmittedDialog?.setMessage(
                    resources.getQuantityString(
                        R.plurals.exam_submitted_countdown,
                        secondsRemaining,
                        secondsRemaining,
                    ),
                )
            }

            override fun onFinish() {
                closeAfterExamSubmitted()
            }
        }.start()
    }

    private fun closeAfterExamSubmitted() {
        examExitTimer?.cancel()
        examExitTimer = null
        examSubmittedDialog?.dismiss()
        examSubmittedDialog = null
        window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        window.clearFlags(WindowManager.LayoutParams.FLAG_SECURE)
        finishAndRemoveTask()
    }

    private fun handleLogViolation(args: JSONArray) {
        val sessionId = Prefs.sessionId ?: return
        if (args.length() < 2) return
        val type = args.optString(0)
        val count = args.optInt(1, 1)
        val details = if (args.length() > 2) args.optString(2) else null
        io.execute {
            ApiClient.get().logViolation(sessionId, type, count, details)
        }
    }

    private fun handleLogout() {
        kiosk.stopExamLock()
        Prefs.clearAuth()
        goLogin()
    }

    private fun handleForceKicked(args: JSONArray) {
        val reason = if (args.length() > 0) args.optString(0) else getString(R.string.force_kicked_body)
        kiosk.stopExamLock()
        Prefs.clearSession()
        showExitDialog(
            title = getString(R.string.force_kicked_title),
            reason = reason,
            body = getString(R.string.force_kicked_body),
            color = 0xFFEF4444.toInt(),
        )
    }

    private fun handleForceSubmit(args: JSONArray) {
        val reason = if (args.length() > 0) {
            args.optString(0)
        } else {
            "Dikumpulkan oleh pengawas"
        }
        Toast.makeText(this, "Ujian dikumpulkan: $reason", Toast.LENGTH_LONG).show()
    }

    private fun handleExamCancelled(args: JSONArray) {
        val reason = if (args.length() > 0) args.optString(0) else getString(R.string.exam_cancelled_body)
        kiosk.stopExamLock()
        Prefs.clearSession()
        showExitDialog(
            title = getString(R.string.exam_cancelled_title),
            reason = reason,
            body = getString(R.string.exam_cancelled_body),
            color = 0xFFF59E0B.toInt(),
        )
    }

    private fun showExitDialog(title: String, reason: String, body: String, color: Int) {
        val dialog = AlertDialog.Builder(this)
            .setTitle(title)
            .setMessage("$reason\n\n$body")
            .setCancelable(false)
            .setPositiveButton(R.string.back_home) { d, _ ->
                d.dismiss()
                Prefs.clearAuth()
                goLogin()
            }
            .create()
        dialog.setOnShowListener {
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setTextColor(color)
        }
        dialog.show()
    }

    private fun openImagePreview(args: JSONArray) {
        if (args.length() == 0) return
        val raw = args.optString(0).trim()
        if (raw.isEmpty()) return
        val title = if (args.length() > 1 && args.optString(1).isNotBlank()) {
            args.optString(1)
        } else {
            getString(R.string.image_preview)
        }
        val resolved = resolveUrl(raw) ?: return
        io.execute {
            val bytes = ApiClient.get().fetchBytes(resolved) ?: return@execute
            val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size) ?: return@execute
            runOnUiThread {
                val view = layoutInflater.inflate(R.layout.dialog_image_preview, null)
                view.findViewById<TextView>(R.id.previewTitle).text = title
                view.findViewById<ImageView>(R.id.previewImage).setImageBitmap(bitmap)
                AlertDialog.Builder(this)
                    .setView(view)
                    .setPositiveButton(android.R.string.ok, null)
                    .show()
            }
        }
    }

    private fun urlsMatch(left: String, right: String): Boolean {
        return try {
            val a = java.net.URI(left)
            val b = java.net.URI(right)
            a.scheme.equals(b.scheme, ignoreCase = true) &&
                a.host.equals(b.host, ignoreCase = true) &&
                a.path == b.path
        } catch (_: Exception) {
            left == right
        }
    }

    private fun resolveUrl(raw: String): String? {
        if (raw.startsWith("https://") || raw.startsWith("http://")) {
            return if (AppConfig.forceHttps && raw.startsWith("http://")) {
                "https://" + raw.removePrefix("http://")
            } else {
                raw
            }
        }
        val current = binding.examWebView.url
        if (!current.isNullOrBlank()) {
            return try {
                java.net.URI(current).resolve(raw).toString()
            } catch (_: Exception) {
                AppConfig.normalizedServerUrl() + raw.trimStart('/')
            }
        }
        return AppConfig.normalizedServerUrl() + raw.trimStart('/')
    }

    private fun goLogin() {
        val intent = Intent(this, LoginActivity::class.java)
        intent.flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
        startActivity(intent)
        finish()
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus && kiosk.examActive) {
            kiosk.applyImmersive()
        }
        if (!hasFocus && kiosk.examActive && !lockActivationInProgress) {
            try {
                val manager = getSystemService(ACTIVITY_SERVICE) as ActivityManager
                manager.moveTaskToFront(taskId, 0)
            } catch (_: Exception) {
            }
        }
    }

    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        if (kiosk.examActive) {
            return when (keyCode) {
                KeyEvent.KEYCODE_VOLUME_UP,
                KeyEvent.KEYCODE_VOLUME_DOWN,
                KeyEvent.KEYCODE_HOME,
                KeyEvent.KEYCODE_BACK,
                KeyEvent.KEYCODE_MENU,
                KeyEvent.KEYCODE_APP_SWITCH,
                -> true
                else -> super.onKeyDown(keyCode, event)
            }
        }
        return super.onKeyDown(keyCode, event)
    }

    override fun onKeyLongPress(keyCode: Int, event: KeyEvent?): Boolean {
        if (kiosk.examActive && keyCode == KeyEvent.KEYCODE_BACK) {
            return true
        }
        return super.onKeyLongPress(keyCode, event)
    }

    override fun onDestroy() {
        kioskWarningDialog?.dismiss()
        examExitTimer?.cancel()
        examExitTimer = null
        examSubmittedDialog?.dismiss()
        examSubmittedDialog = null
        window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        if (kiosk.examActive) {
            kiosk.stopExamLock()
        }
        binding.examWebView.apply {
            stopLoading()
            removeJavascriptInterface("SIAB1Bridge")
            destroy()
        }
        io.shutdownNow()
        super.onDestroy()
    }
}
