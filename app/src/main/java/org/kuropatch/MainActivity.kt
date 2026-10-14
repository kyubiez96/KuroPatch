package org.kuropatch

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.OpenableColumns
import android.view.View
import android.widget.ArrayAdapter
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.FileProvider
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import org.kuropatch.databinding.ActivityMainBinding
import java.io.File
import kotlin.concurrent.thread

/**
 * KuroPatch v2 — premium game localisation patcher.
 *
 * On-device target is .JAR ONLY (Java game resources: .properties +
 * smali const-string + .class constants + string packs).
 * .apk files are REFUSED: full APK patching needs apktool, which
 * requires a real JVM and cannot run on Android. For APKs use the
 * PC CLI.
 *
 * Flow: pick .jar → extract strings → auto-translate (Agnes BYOK)
 * → patch .jar → save as <name>_<target>.jar.
 * Python engine (core/ + translate/) runs on-device via Chaquopy.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private var jarUri: Uri? = null
    private var jarName: String = ""
    private var extractedStrings: Map<String, String> = emptyMap()
    /** Last patched .jar. Lives in app-specific external files (NOT
     * cacheDir) so it survives cache clears and app kills; the user
     * exports it via Save / Share. Restored on launch (see
     * restoreLastOutput) so a process death never orphans it. */
    private var patchedJar: File? = null

    /**
     * System save dialog (SAF CreateDocument). The user picks where the
     * patched .jar goes (Downloads, Documents, ...). Works under
     * Android 14+ scoped storage, no permissions needed.
     * Cancel = early return: the file stays in the output dir and
     * Save stays enabled, so the user can retry any time.
     */
    /**
     * Post UI work only if the activity is still alive. Background
     * threads (extract / translate / patch / save) outlive the
     * activity: on Back-press or rotation the old instance is
     * destroyed, and showing a dialog on it throws
     * WindowManager$BadTokenException (a crash). Every UI post
     * from a background thread must go through here.
     */
    private fun safeUi(block: () -> Unit) {
        if (isFinishing || isDestroyed) return
        runOnUiThread {
            if (isFinishing || isDestroyed) return@runOnUiThread
            block()
        }
    }

    private val savePatched = registerForActivityResult(
        ActivityResultContracts.CreateDocument("application/java-archive")
    ) { uri ->
        val src = patchedJar
        if (uri == null || src == null || !src.exists()) {
            if (uri != null) showError("Save failed", "Patched .jar is gone — patch again.")
            return@registerForActivityResult
        }
        thread {
            try {
                contentResolver.openOutputStream(uri)?.use { out ->
                    src.inputStream().use { it.copyTo(out) }
                } ?: throw IllegalStateException("cannot open output stream")
                safeUi {
                    binding.tvStatus.text =
                        "Saved: ${src.name} (${src.length() / 1024} KB)"
                    Toast.makeText(this, "Saved", Toast.LENGTH_SHORT).show()
                }
            } catch (e: Exception) {
                safeUi {
                    showError("Save failed", e.message ?: "unknown error")
                }
            }
        }
    }

    private val pickJar = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode != Activity.RESULT_OK) return@registerForActivityResult
        val uri = result.data?.data ?: return@registerForActivityResult
        val name = queryName(uri) ?: "game.jar"
        // Refuse .apk: apktool needs a real JVM, cannot run on Android.
        if (name.endsWith(".apk", ignoreCase = true)) {
            showApkNotice()
            return@registerForActivityResult
        }
        jarUri = uri
        jarName = name
        // Fresh pick = fresh state: a previous jar's extracted /
        // translated strings must NEVER leak into this jar's flow
        // (old code left Patch enabled with jar A's translations,
        // which would have patched jar B with jar A's strings).
        extractedStrings = emptyMap()
        translatedStrings = emptyMap()
        // A new pick also invalidates the previous PATCH output:
        // Save / Share must not keep serving jar A's file after the
        // user has moved on to jar B (stale-output bug).
        patchedJar = null
        binding.btnSave.isEnabled = false
        binding.btnShare.isEnabled = false
        binding.tvJarName.text = jarName
        binding.tvStatus.text = "Picked: $jarName — extract to start"
        binding.btnExtract.isEnabled = true
        binding.btnTranslate.isEnabled = false
        binding.btnPatch.isEnabled = false
        binding.cardJar.visibility = View.VISIBLE
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        initPython()
        setupLanguageSpinner()
        setupProviderSpinner()

        binding.btnPickJar.setOnClickListener { launchPicker() }
        binding.btnExtract.setOnClickListener { extractStrings() }
        binding.btnTranslate.setOnClickListener { translateStrings() }
        binding.btnPatch.setOnClickListener { patchJar() }
        binding.btnSave.setOnClickListener { launchSaveDialog() }
        binding.btnShare.setOnClickListener { shareOutput() }
        restoreLastOutput()
    }

    private fun initPython() {
        try {
            if (!Python.isStarted()) Python.start(AndroidPlatform(this))
        } catch (e: Exception) {
            // Chaquopy init failure = nothing works. Say so with a
            // dialog instead of crashing out of onCreate.
            android.util.Log.e("KuroPatch", "Python.start failed", e)
            showError("Python failed to start",
                "KuroPatch cannot run without the Python engine: " +
                    (e.message ?: "unknown error"))
            binding.btnPickJar.isEnabled = false
        }
    }

    private fun setupLanguageSpinner() {
        val langs = arrayOf("Indonesian (id)", "English (en)", "Malay (ms)",
            "Japanese (ja)", "Korean (ko)", "Chinese Simplified (zh)")
        binding.spinnerLang.adapter = ArrayAdapter(
            this, android.R.layout.simple_spinner_dropdown_item, langs)
    }

    private fun setupProviderSpinner() {
        val providers = arrayOf("Agnes (BYOK — fast & free)", "Passthrough (no-op)")
        binding.spinnerProvider.adapter = ArrayAdapter(
            this, android.R.layout.simple_spinner_dropdown_item, providers)
    }

    private fun launchPicker() {
        val intent = Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = "*/*"
            putExtra(Intent.EXTRA_MIME_TYPES,
                // application/zip: many file managers label .jar files as
                // ZIP -- without it those files are hidden by the picker.
                arrayOf("application/java-archive", "application/zip", "application/octet-stream"))
        }
        pickJar.launch(intent)
    }

    private fun queryName(uri: Uri): String? {
        contentResolver.query(uri, null, null, null, null)?.use { c ->
            if (c.moveToFirst()) {
                val idx = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                if (idx >= 0) {
                    val name = c.getString(idx)
                    // An empty DISPLAY_NAME is as useless as a null one:
                    // File(cacheDir, "") is the dir itself, and
                    // copyToCache would fail on it.
                    if (!name.isNullOrBlank()) return name
                }
            }
        }
        return null
    }

    private fun langCode(): String =
        binding.spinnerLang.selectedItem.toString()
            .substringAfter("(").substringBefore(")")

    private fun providerName(): String =
        if (binding.spinnerProvider.selectedItemPosition == 0) "agnes" else "passthrough"

    /**
     * Convert a Kotlin Map to a REAL Python dict before passing it to
     * Python via Chaquopy. Chaquopy passes Kotlin Maps as Java Map
     * proxies (LinkedHashMap) which Python code cannot iterate with
     * .items() or a for-loop -- belt + suspenders with the Python-side
     * _coerce_str_dict(): either side alone is sufficient.
     */
    private fun toPyDict(py: Python, map: Map<String, String>): PyObject {
        val pyDict = py.getModule("builtins").callAttr("dict")
        for ((k, v) in map) {
            pyDict.callAttr("__setitem__", k, v)
        }
        return pyDict
    }

    private fun showApkNotice() {
        MaterialAlertDialogBuilder(this)
            .setTitle(R.string.apk_not_supported_title)
            .setMessage(R.string.apk_not_supported)
            .setPositiveButton(R.string.dialog_ok, null)
            .show()
    }

    private fun extractStrings() {
        val uri = jarUri ?: return
        // A fresh extraction invalidates translations from any
        // previous jar -- they must not survive to the patch step.
        translatedStrings = emptyMap()
        binding.btnPatch.isEnabled = false
        setBusy(true, "Extracting strings…")
        thread {
            try {
                val py = Python.getInstance()
                val jarMod = py.getModule("core.jar")
                val tmp = File(cacheDir, "extract").apply { mkdirs() }
                val jarFile = File(tmp, jarName)
                copyToCache(uri, jarFile)
                // collect_strings -> {"strings": {value: [origins]}, ...}
                val result = jarMod.callAttr("collect_strings", jarFile.absolutePath)
                val stringsMap = result.asMap()
                    .entries.firstOrNull { it.key.toString() == "strings" }
                    ?.value?.asMap() ?: emptyMap()
                // translate engine takes {key: source}; for .jar the
                // collected value IS the key (matched by value on patch).
                extractedStrings = stringsMap.keys
                    .associate { it.toString() to it.toString() }
                safeUi {
                    setBusy(false)
                    binding.tvStatus.text =
                        "${extractedStrings.size} strings found"
                    binding.btnTranslate.isEnabled = extractedStrings.isNotEmpty()
                    binding.cardStrings.visibility = View.VISIBLE
                }
            } catch (e: Exception) {
                safeUi {
                    setBusy(false)
                    showError("Extract failed", e.message ?: "unknown error")
                }
            }
        }
    }

    private fun translateStrings() {
        if (extractedStrings.isEmpty()) return
        if (providerName() == "agnes" && getAgnesKey().isBlank()) {
            promptAgnesKey()
            return
        }
        setBusy(true, "Translating…")
        // export BYOK key so Chaquopy Python (os.environ) can read it
        try {
            android.system.Os.setenv("AGNES_API_KEY", getAgnesKey(), true)
        } catch (e: Exception) {
            // Don't swallow this: without the env export the provider
            // sees no key and translate fails with a confusing error.
            android.util.Log.w("KuroPatch", "Os.setenv failed", e)
        }
        thread {
            try {
                val py = Python.getInstance()
                val engine = py.getModule("translate.engine")
                val providers = py.getModule("translate.providers")
                val provider = if (providerName() == "agnes") {
                    providers.get("AgnesProvider")!!.callAttr("__call__")
                } else {
                    providers.get("PassthroughProvider")!!.callAttr("__call__")
                }
                val results = engine.callAttr("translate_strings",
                    toPyDict(py, extractedStrings), langCode(), "en", provider)
                val done = engine.callAttr("results_to_dict", results)
                val translated: Map<String, String> = done.asMap()
                    .map { it.key.toString() to it.value.toString() }.toMap()
                safeUi {
                    setBusy(false)
                    val n = translated.size
                    binding.tvStatus.text = "$n strings translated"
                    binding.btnPatch.isEnabled = true
                    // stash for patch step
                    translatedStrings = translated
                }
            } catch (e: Exception) {
                safeUi {
                    setBusy(false)
                    showError("Translate failed", e.message ?: "unknown error")
                }
            }
        }
    }

    private var translatedStrings: Map<String, String> = emptyMap()

    private fun patchJar() {
        if (translatedStrings.isEmpty()) return
        setBusy(true, "Patching .jar…")
        thread {
            try {
                val py = Python.getInstance()
                val jarMod = py.getModule("core.jar")
                // App-specific external files, NOT cacheDir: survives
                // "Clear cache" and OS cache eviction, no permission
                // needed, and matches the FileProvider paths so Share
                // can serve it. Same deterministic name every run, so
                // re-patching cleanly overwrites the previous output.
                val outDir = outputDir()
                val jarFile = File(File(cacheDir, "extract"), jarName)
                // Strip a trailing ".jar" if present; otherwise the
                // input name is used as-is -- either way the output
                // always ends with ".jar" so restore/save can find it.
                val base = if (jarName.endsWith(".jar", ignoreCase = true))
                    jarName.dropLast(4) else jarName
                val outJar = File(outDir, "${base}_${langCode()}.jar")
                jarMod.callAttr("patch_jar",
                    jarFile.absolutePath, toPyDict(py, translatedStrings),
                    outJar.absolutePath)
                safeUi {
                    setBusy(false)
                    patchedJar = outJar
                    binding.tvStatus.text =
                        "Done: ${outJar.name} (${outJar.length() / 1024} KB) — pick where to save it"
                    binding.btnSave.isEnabled = true
                    binding.btnShare.isEnabled = true
                    // Open the save dialog right away: this IS the
                    // deliverable. Guarded: the activity may have been
                    // backgrounded/destroyed while patching ran.
                    if (!isFinishing && !isDestroyed) launchSaveDialog()
                }
            } catch (e: Exception) {
                safeUi {
                    setBusy(false)
                    showError("Patch failed", e.message ?: "unknown error")
                }
            }
        }
    }

    /** Open the system save dialog for the last patched .jar. */
    private fun launchSaveDialog() {
        val src = patchedJar
        if (src == null || !src.exists()) {
            showError("Nothing to save", "Patch a .jar first.")
            return
        }
        savePatched.launch(src.name)
    }

    /** App-specific external output dir (v1 pattern). Falls back to
     * internal files if external storage is unavailable. */
    private fun outputDir(): File =
        (getExternalFilesDir(null) ?: filesDir).resolve("KuroPatch")
            .apply { mkdirs() }

    /**
     * Process-death recovery: if the app was killed after a patch
     * (user switched to Files, OS reclaimed memory), the output file
     * still sits in the output dir. Restore the newest one so Save /
     * Share keep working without forcing a re-patch (re-translate).
     */
    private fun restoreLastOutput() {
        val dir = (getExternalFilesDir(null) ?: filesDir).resolve("KuroPatch")
        val newest = dir.listFiles { f ->
            f.isFile && f.name.endsWith(".jar", ignoreCase = true)
        }?.maxByOrNull { it.lastModified() }
        if (newest != null) {
            patchedJar = newest
            binding.tvStatus.text =
                "Last output: ${newest.name} (${newest.length() / 1024} KB)"
            binding.btnSave.isEnabled = true
            binding.btnShare.isEnabled = true
        }
    }

    /** Share the last patched .jar via FileProvider (streamed — an
     * 88 MB jar is never loaded into memory). Cancel / no handler =
     * nothing happens; the file stays put. */
    private fun shareOutput() {
        val file = patchedJar
        if (file == null || !file.isFile) {
            showError("Nothing to share", "Patch a .jar first.")
            return
        }
        try {
            val uri = FileProvider.getUriForFile(
                this, "${packageName}.fileprovider", file)
            val intent = Intent(Intent.ACTION_SEND).apply {
                type = "application/java-archive"
                putExtra(Intent.EXTRA_STREAM, uri)
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
            startActivity(Intent.createChooser(intent, "Share patched .jar"))
        } catch (e: Exception) {
            showError("Share failed", e.message ?: "unknown error")
        }
    }

    private fun getAgnesKey(): String =
        getSharedPreferences("kuropatch", MODE_PRIVATE)
            .getString("agnes_key", "") ?: ""

    private fun promptAgnesKey() {
        val input = android.widget.EditText(this).apply {
            hint = "Paste your Agnes API key"
            // Password variation: the key must not sit visible on
            // screen (screenshots / shoulder surfing).
            inputType = android.text.InputType.TYPE_CLASS_TEXT or
                android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        MaterialAlertDialogBuilder(this)
            .setTitle("Agnes API key (BYOK)")
            .setMessage("Your key stays on this device only.")
            .setView(input)
            .setPositiveButton("Save") { _, _ ->
                getSharedPreferences("kuropatch", MODE_PRIVATE).edit()
                    .putString("agnes_key",
                        input.text.toString().trim()).apply()
                // export for Chaquopy
                translateStrings()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun copyToCache(uri: Uri, dest: File) {
        // A null stream must FAIL LOUDLY: silently skipping would leave
        // a stale file from a previous extract in place and collect
        // the WRONG jar's strings.
        val input = contentResolver.openInputStream(uri)
            ?: throw IllegalStateException("cannot open picked file")
        input.use {
            dest.outputStream().use { output -> it.copyTo(output) }
        }
    }

    private fun setBusy(busy: Boolean, msg: String = "") {
        binding.progressBar.visibility = if (busy) View.VISIBLE else View.GONE
        if (msg.isNotEmpty()) binding.tvStatus.text = msg
        binding.btnPickJar.isEnabled = !busy
        binding.btnExtract.isEnabled = !busy && jarUri != null
        binding.btnTranslate.isEnabled = !busy && extractedStrings.isNotEmpty()
        binding.btnPatch.isEnabled = !busy && translatedStrings.isNotEmpty()
        val hasOutput = patchedJar?.exists() == true
        binding.btnSave.isEnabled = !busy && hasOutput
        binding.btnShare.isEnabled = !busy && hasOutput
    }

    private fun showError(title: String, msg: String) {
        MaterialAlertDialogBuilder(this)
            .setTitle(title)
            .setMessage(msg)
            .setPositiveButton("OK", null)
            .show()
    }
}
