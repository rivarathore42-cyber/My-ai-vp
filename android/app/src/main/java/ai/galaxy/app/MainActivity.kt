package ai.galaxy.app

import android.Manifest
import android.annotation.SuppressLint
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.webkit.GeolocationPermissions
import android.webkit.PermissionRequest
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat

/*
 * Opens the Galaxy AI page in a WebView. The page asks for the microphone, location and camera in the browser way;
 * this activity answers those requests by asking Android for the matching runtime permission first, and grants the
 * page only what the person allowed. Files come from the system picker, which needs no storage permission.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var web: WebView
    private var afterPermissions: (() -> Unit)? = null
    private var pendingFiles: ValueCallback<Array<Uri>>? = null

    private val askPermissions = registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
        val next = afterPermissions
        afterPermissions = null
        next?.invoke()
    }

    private val pickFiles = registerForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
        pendingFiles?.onReceiveValue(if (uris.isEmpty()) null else uris.toTypedArray())
        pendingFiles = null
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        web = WebView(this)
        setContentView(web)

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (web.canGoBack()) web.goBack() else finish()
            }
        })

        if (BuildConfig.GALAXY_URL.isBlank()) {
            web.loadData(
                "<p style='font-family:sans-serif;padding:24px'>Build se pehle galaxyUrl set karein (android/gradle.properties).<br>" +
                    "Set galaxyUrl in android/gradle.properties before building.</p>",
                "text/html", "utf-8"
            )
            return
        }

        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true            // the page keeps its voice settings in localStorage
            allowFileAccess = false
            allowContentAccess = false
            setGeolocationEnabled(true)
        }

        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val target = request.url
                val home = Uri.parse(BuildConfig.GALAXY_URL).host
                if (target.scheme == "https" && target.host == home) return false
                try {
                    startActivity(Intent(Intent.ACTION_VIEW, target))
                } catch (_: ActivityNotFoundException) {
                    // no app can open this link; the page stays where it is
                }
                return true
            }

            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (!request.isForMainFrame) return
                view.loadData(
                    "<p style='font-family:sans-serif;padding:24px'>Server tak nahi pahunch paya. Internet check karein. " +
                        "Free server so raha ho sakta hai: 1 minute baad dobara kholein.<br>" +
                        "Could not reach the server. Check the connection; a free server may be waking up, try again in a minute.</p>",
                    "text/html", "utf-8"
                )
            }
        }

        web.webChromeClient = object : WebChromeClient() {
            override fun onPermissionRequest(request: PermissionRequest) {
                val wanted = request.resources
                val supported = wanted.all {
                    it == PermissionRequest.RESOURCE_AUDIO_CAPTURE || it == PermissionRequest.RESOURCE_VIDEO_CAPTURE
                }
                val needed = mutableListOf<String>()
                if (PermissionRequest.RESOURCE_AUDIO_CAPTURE in wanted) needed += Manifest.permission.RECORD_AUDIO
                if (PermissionRequest.RESOURCE_VIDEO_CAPTURE in wanted) needed += Manifest.permission.CAMERA
                if (!supported || needed.isEmpty()) {
                    request.deny()
                    return
                }
                askIfMissing(needed) {
                    val allowed = wanted.filter { resource ->
                        when (resource) {
                            PermissionRequest.RESOURCE_AUDIO_CAPTURE -> hasPermission(Manifest.permission.RECORD_AUDIO)
                            PermissionRequest.RESOURCE_VIDEO_CAPTURE -> hasPermission(Manifest.permission.CAMERA)
                            else -> false
                        }
                    }
                    if (allowed.isEmpty()) request.deny() else request.grant(allowed.toTypedArray())
                }
            }

            override fun onGeolocationPermissionsShowPrompt(origin: String, callback: GeolocationPermissions.Callback) {
                val location = listOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION)
                askIfMissing(location) {
                    val granted = hasPermission(Manifest.permission.ACCESS_FINE_LOCATION) ||
                        hasPermission(Manifest.permission.ACCESS_COARSE_LOCATION)
                    callback.invoke(origin, granted, false)
                }
            }

            override fun onShowFileChooser(
                webView: WebView,
                callback: ValueCallback<Array<Uri>>,
                params: FileChooserParams
            ): Boolean {
                pendingFiles?.onReceiveValue(null)
                pendingFiles = callback
                pickFiles.launch(arrayOf("image/*", "application/pdf"))
                return true
            }
        }

        web.loadUrl(BuildConfig.GALAXY_URL)
    }

    override fun onPause() {
        web.onPause()
        super.onPause()
    }

    override fun onResume() {
        super.onResume()
        web.onResume()
    }

    private fun hasPermission(permission: String): Boolean =
        ContextCompat.checkSelfPermission(this, permission) == PackageManager.PERMISSION_GRANTED

    // Asks Android only for what is still missing; runs the continuation straight away when all are granted.
    private fun askIfMissing(permissions: List<String>, then: () -> Unit) {
        val missing = permissions.filter { !hasPermission(it) }
        if (missing.isEmpty()) {
            then()
            return
        }
        afterPermissions = then
        askPermissions.launch(missing.toTypedArray())
    }
}
