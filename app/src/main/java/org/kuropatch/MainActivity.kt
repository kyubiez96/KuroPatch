package org.kuropatch

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.OpenableColumns
import android.text.method.ScrollingMovementMethod
import android.view.View
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.CheckBox
import android.widget.ProgressBar
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.FileProvider
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import com.google.android.material.textfield.TextInputEditText
import com.google.android.material.textfield.TextInputLayout
import java.io.File
import kotlin.concurrent.thread

/**
 * The real KuroPatch UI.
 *
 * 1. "Load .jar" picks a game archive through the Storage Access Framework and
 *    stages it in private storage (Chaquopy/Python needs a real file path,
 *    not a content:// URI).
 * 2. Options: source/target language, translation provider, optional API key.
 * 3. "Start patching" runs android_bridge.run_patch() (the repo-root Python
 *    engine: patcher.py + translator.py + main.py) on a worker thread via
 *    Chaquopy, with progress, log and output surfaced back in the UI.
 * 4. The patched archive lands in <app-external-files>/KuroPatch/ and can be
 *    shared through FileProvider.
 *
 * .apk files are refused with an explanation: the full APK pipeline needs
 * apktool, which requires a real JVM and cannot run on Android. For APKs the
 * PC CLI (python apk.py) is the way.
 */
class MainActivity : AppCompatActivity() {

    // -- views -----------------------------------------------------------------
    private lateinit var btnPick: Button
    private lateinit var tvFile: TextView
    private lateinit var spinnerSource: Spinner
    private lateinit var spinnerTarget: Spinner
    private lateinit var spinnerProvider: Spinner
    private lateinit var apiKeyLayout: TextInputLayout
    private lateinit var etApiKey: TextInputEditText
    private lateinit var cbTranslateAll: CheckBox
    private lateinit var btnStart: Button
    private lateinit var tvStatus: TextView
    private lateinit var progressBar: ProgressBar
    private lateinit var tvLog: TextView
    private lateinit var tvOutput: TextView
    private lateinit var btnShare: Button

    // -- state -----------------------------------------------------------------
    private var inputFile: File? = null
    private var outputFile: File? = null
    private var running = false

    private val languages = listOf(
        "English" to "en",
        "Indonesian" to "id",
        "Malay" to "ms",
        "Javanese" to "jv",
        "Sundanese" to "su",
        "Chinese (Simplified)" to "zh",
        "Japanese" to "ja",
        "Korean" to "ko",
        "Thai" to "th",
        "Vietnamese" to "vi",
        "Arabic" to "ar",
        "Spanish" to "es",
        "French" to "fr",
        "German" to "de",
        "Portuguese" to "pt",
        "Russian" to "ru",
        "Hindi" to "hi",
        "Turkish" to "tr",
    )
    private val providers = listOf(
        "Google Translate web (free, no key)" to "google-web",
        "Google Cloud Translation v2 (API key)" to "google-v2",
        "LibreTranslate" to "libretranslate",
        "Dry run — list strings only" to "none",
    )

    // -- file picker -------------------------------------------------------------
    private val pickJar = registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri: Uri? ->
        if (uri == null) return@registerForActivityResult
        try {
            val name = displayNameOf(uri) ?: "game.jar"
            if (name.endsWith(".apk", ignoreCase = true)) {
                showApkNotice()
                return@registerForActivityResult
            }
            val dest = File(File(filesDir, "input").apply { mkdirs() }, sanitize(name))
            contentResolver.openInputStream(uri)?.use { src ->
                dest.outputStream().use { dst -> src.copyTo(dst) }
            } ?: throw IllegalStateException("empty stream")
            // Persist the read grant across process restarts.
            try {
                contentResolver.takePersistableUriPermission(
                    uri, Intent.FLAG_GRANT_READ_URI_PERMISSION
                )
            } catch (_: SecurityException) { /* best effort */ }
            inputFile = dest
            tvFile.text = "${dest.name} (${dest.length() / 1024} KB)"
            btnStart.isEnabled = true
            appendLog("[*] Loaded ${dest.name}")
        } catch (e: Exception) {
            Toast.makeText(this, getString(R.string.err_copy), Toast.LENGTH_LONG).show()
            appendLog("[!] ${getString(R.string.err_copy)}: ${e.message}")
        }
    }

    // -- lifecycle ----------------------------------------------------------------
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        btnPick = findViewById(R.id.btnPick)
        tvFile = findViewById(R.id.tvFile)
        spinnerSource = findViewById(R.id.spinnerSource)
        spinnerTarget = findViewById(R.id.spinnerTarget)
        spinnerProvider = findViewById(R.id.spinnerProvider)
        apiKeyLayout = findViewById(R.id.apiKeyLayout)
        etApiKey = findViewById(R.id.etApiKey)
        cbTranslateAll = findViewById(R.id.cbTranslateAll)
        btnStart = findViewById(R.id.btnStart)
        tvStatus = findViewById(R.id.tvStatus)
        progressBar = findViewById(R.id.progressBar)
        tvLog = findViewById(R.id.tvLog)
        tvOutput = findViewById(R.id.tvOutput)
        btnShare = findViewById(R.id.btnShare)

        tvLog.movementMethod = ScrollingMovementMethod()

        spinnerSource.adapter = langAdapter()
        spinnerTarget.adapter = langAdapter()
        spinnerTarget.setSelection(languages.indexOfFirst { it.second == "id" }.coerceAtLeast(0))
        spinnerProvider.adapter = ArrayAdapter(
            this, android.R.layout.simple_spinner_dropdown_item, providers.map { it.first }
        )
        spinnerProvider.onItemSelectedListener = object : SimpleItemSelectedListener() {
            override fun onItemSelected(position: Int) {
                val id = providers[position].second
                apiKeyLayout.visibility =
                    if (id == "google-v2" || id == "libretranslate") View.VISIBLE else View.GONE
            }
        }

        btnPick.setOnClickListener {
            pickJar.launch(arrayOf("application/java-archive", "application/octet-stream", "*/*"))
        }
        btnStart.setOnClickListener { startPatching() }
        btnShare.setOnClickListener { shareOutput() }
    }

    // -- patching ------------------------------------------------------------------
    private fun startPatching() {
        val input = inputFile
        if (input == null || !input.isFile) {
            Toast.makeText(this, getString(R.string.err_no_input), Toast.LENGTH_SHORT).show()
            return
        }
        if (running) return
        running = true
        btnStart.isEnabled = false
        btnPick.isEnabled = false
        btnShare.isEnabled = false
        tvLog.text = ""
        progressBar.progress = 0
        tvOutput.text = getString(R.string.no_output)
        outputFile = null

        val source = languages[spinnerSource.selectedItemPosition].second
        val target = languages[spinnerTarget.selectedItemPosition].second
        val provider = providers[spinnerProvider.selectedItemPosition].second
        val apiKey = etApiKey.text?.toString().orEmpty().ifBlank { null }
        val translateAll = cbTranslateAll.isChecked
        val dryRun = provider == "none"

        val outDir = (getExternalFilesDir(null) ?: filesDir).resolve("KuroPatch").apply { mkdirs() }
        val output = outDir.resolve("${input.nameWithoutExtension}_${target.uppercase()}.jar")

        appendLog("[*] Input : ${input.absolutePath}")
        appendLog("[*] Output: ${output.absolutePath}")
        appendLog("[*] $source -> $target via $provider")

        thread(isDaemon = true, name = "kuropatch") {
            try {
                if (!Python.isStarted()) {
                    Python.start(AndroidPlatform(this@MainActivity))
                }
                val bridge = Python.getInstance().getModule("android_bridge")
                bridge.callAttr(
                    "run_patch",
                    input.absolutePath,          // input_path
                    output.absolutePath,         // output_path
                    outDir.absolutePath,         // report_dir
                    source,                      // source
                    target,                      // target
                    provider,                    // provider
                    apiKey,                      // api_key (None when blank)
                    translateAll,                // translate_all
                    dryRun,                      // dry_run
                    File(cacheDir, "kuropatch-work").absolutePath, // workspace_dir
                    bridgeListener,              // listener
                )
            } catch (e: Exception) {
                runOnUiThread {
                    onDoneInternal(false, "", "Failed: ${e.message}")
                }
            }
        }
    }

    /**
     * Called from Python (android_bridge) on the worker thread. Method names
     * are snake_case to match the bridge's listener protocol exactly.
     */
    @Suppress("unused")
    private val bridgeListener = object {
        fun on_log(line: String) = runOnUiThread { appendLog(line) }

        fun on_status(status: String, message: String) = runOnUiThread {
            tvStatus.text = message.ifBlank { status }
        }

        fun on_progress(done: Int, total: Int) = runOnUiThread {
            progressBar.max = 100
            progressBar.progress = if (total > 0) (done * 100 / total) else 0
            tvStatus.text = "Translating $done / $total"
        }

        fun on_done(success: Boolean, outputPath: String, message: String) = runOnUiThread {
            onDoneInternal(success, outputPath, message)
        }
    }

    private fun onDoneInternal(success: Boolean, outputPath: String, message: String) {
        running = false
        btnStart.isEnabled = true
        btnPick.isEnabled = true
        appendLog(if (success) "[✓] $message" else "[!] $message")
        tvStatus.text = message
        if (success && outputPath.isNotBlank()) {
            val file = File(outputPath)
            if (file.isFile) {
                outputFile = file
                tvOutput.text = outputPath
                btnShare.isEnabled = true
                progressBar.progress = 100
            }
        } else if (success) {
            // Dry run: the "output" is the report directory.
            tvOutput.text = message
        }
    }

    // -- sharing --------------------------------------------------------------------
    private fun shareOutput() {
        val file = outputFile
        if (file == null || !file.isFile) return
        val uri = FileProvider.getUriForFile(this, "${packageName}.fileprovider", file)
        val intent = Intent(Intent.ACTION_SEND).apply {
            type = "application/java-archive"
            putExtra(Intent.EXTRA_STREAM, uri)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        startActivity(Intent.createChooser(intent, getString(R.string.share_title)))
    }

    // -- helpers ----------------------------------------------------------------------
    private fun showApkNotice() {
        AlertDialog.Builder(this)
            .setTitle(R.string.apk_not_supported_title)
            .setMessage(R.string.apk_not_supported)
            .setPositiveButton(R.string.dialog_ok, null)
            .show()
    }

    private fun appendLog(line: String) {
        tvLog.append(line + "\n")
        // Auto-scroll to the bottom.
        val layout = tvLog.layout
        if (layout != null) {
            val scrollAmount = layout.getLineTop(tvLog.lineCount) - tvLog.height
            tvLog.scrollTo(0, scrollAmount.coerceAtLeast(0))
        }
    }

    private fun displayNameOf(uri: Uri): String? {
        contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)
            ?.use { cursor ->
                if (cursor.moveToFirst()) {
                    return cursor.getString(0)
                }
            }
        return uri.lastPathSegment?.substringAfterLast('/')
    }

    private fun sanitize(name: String): String =
        name.replace(Regex("[/\\\\]"), "_").take(120).ifBlank { "game.jar" }

    private fun langAdapter() = ArrayAdapter(
        this, android.R.layout.simple_spinner_dropdown_item, languages.map { it.first }
    )

    private abstract class SimpleItemSelectedListener :
        android.widget.AdapterView.OnItemSelectedListener {
        override fun onNothingSelected(parent: android.widget.AdapterView<*>?) {}
        override fun onItemSelected(
            parent: android.widget.AdapterView<*>?, view: View?, position: Int, id: Long
        ) = onItemSelected(position)

        abstract fun onItemSelected(position: Int)
    }
}
