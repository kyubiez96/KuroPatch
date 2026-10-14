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
        binding.tvJarName.text = jarName
        binding.btnExtract.isEnabled = true
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
    }

    private fun initPython() {
        if (!Python.isStarted()) Python.start(AndroidPlatform(this))
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
                arrayOf("application/java-archive", "application/octet-stream"))
        }
        pickJar.launch(intent)
    }

    private fun queryName(uri: Uri): String? {
        contentResolver.query(uri, null, null, null, null)?.use { c ->
            if (c.moveToFirst()) {
                val idx = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                if (idx >= 0) return c.getString(idx)
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
                runOnUiThread {
                    setBusy(false)
                    binding.tvStatus.text =
                        "${extractedStrings.size} strings found"
                    binding.btnTranslate.isEnabled = extractedStrings.isNotEmpty()
                    binding.cardStrings.visibility = View.VISIBLE
                }
            } catch (e: Exception) {
                runOnUiThread {
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
        } catch (_: Exception) { }
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
                runOnUiThread {
                    setBusy(false)
                    val n = translated.size
                    binding.tvStatus.text = "$n strings translated"
                    binding.btnPatch.isEnabled = true
                    // stash for patch step
                    translatedStrings = translated
                }
            } catch (e: Exception) {
                runOnUiThread {
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
                val outDir = File(cacheDir, "output").apply { mkdirs() }
                val jarFile = File(File(cacheDir, "extract"), jarName)
                val outJar = File(outDir,
                    jarName.replace(".jar", "_${langCode()}.jar", ignoreCase = true))
                jarMod.callAttr("patch_jar",
                    jarFile.absolutePath, toPyDict(py, translatedStrings),
                    outJar.absolutePath)
                runOnUiThread {
                    setBusy(false)
                    binding.tvStatus.text = "Done: ${outJar.name}"
                    Toast.makeText(this, "Patched .jar ready",
                        Toast.LENGTH_LONG).show()
                }
            } catch (e: Exception) {
                runOnUiThread {
                    setBusy(false)
                    showError("Patch failed", e.message ?: "unknown error")
                }
            }
        }
    }

    private fun getAgnesKey(): String =
        getSharedPreferences("kuropatch", MODE_PRIVATE)
            .getString("agnes_key", "") ?: ""

    private fun promptAgnesKey() {
        val input = android.widget.EditText(this).apply {
            hint = "Paste your Agnes API key"
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
        contentResolver.openInputStream(uri)?.use { input ->
            dest.outputStream().use { output -> input.copyTo(output) }
        }
    }

    private fun setBusy(busy: Boolean, msg: String = "") {
        binding.progressBar.visibility = if (busy) View.VISIBLE else View.GONE
        if (msg.isNotEmpty()) binding.tvStatus.text = msg
        binding.btnPickJar.isEnabled = !busy
        binding.btnExtract.isEnabled = !busy && jarUri != null
        binding.btnTranslate.isEnabled = !busy && extractedStrings.isNotEmpty()
        binding.btnPatch.isEnabled = !busy && translatedStrings.isNotEmpty()
    }

    private fun showError(title: String, msg: String) {
        MaterialAlertDialogBuilder(this)
            .setTitle(title)
            .setMessage(msg)
            .setPositiveButton("OK", null)
            .show()
    }
}
