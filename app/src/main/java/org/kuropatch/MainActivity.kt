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
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import org.kuropatch.databinding.ActivityMainBinding
import java.io.File
import kotlin.concurrent.thread

/**
 * KuroPatch v2 — premium game localisation patcher.
 *
 * Flow: pick APK → extract strings → auto-translate (Agnes BYOK) → preview → patch.
 * Python engine (core/ + translate/) runs on-device via Chaquopy.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private var apkUri: Uri? = null
    private var apkName: String = ""
    private var extractedStrings: Map<String, String> = emptyMap()

    private val pickApk = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode != Activity.RESULT_OK) return@registerForActivityResult
        val uri = result.data?.data ?: return@registerForActivityResult
        apkUri = uri
        apkName = queryName(uri) ?: "game.apk"
        binding.tvApkName.text = apkName
        binding.btnExtract.isEnabled = true
        binding.cardApk.visibility = View.VISIBLE
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        initPython()
        setupLanguageSpinner()
        setupProviderSpinner()

        binding.btnPickApk.setOnClickListener { launchPicker() }
        binding.btnExtract.setOnClickListener { extractStrings() }
        binding.btnTranslate.setOnClickListener { translateStrings() }
        binding.btnPatch.setOnClickListener { patchApk() }
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
            type = "application/vnd.android.package-archive"
            putExtra(Intent.EXTRA_MIME_TYPES,
                arrayOf("application/vnd.android.package-archive",
                    "application/octet-stream"))
        }
        pickApk.launch(intent)
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

    private fun extractStrings() {
        val uri = apkUri ?: return
        setBusy(true, "Extracting strings…")
        thread {
            try {
                val py = Python.getInstance()
                val core = py.getModule("core.apk")
                val stringsMod = py.getModule("core.strings")
                val tmp = File(cacheDir, "extract").apply { mkdirs() }
                copyToCache(uri, File(tmp, apkName))
                val info = core.callAttr("extract_apk",
                    File(tmp, apkName).absolutePath,
                    tmp.absolutePath)
                @Suppress("UNCHECKED_CAST")
                extractedStrings = stringsMod.callAttr(
                    "discover_strings", tmp.absolutePath)
                    .asList().associate { it.toString() to it.toString() }
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
        thread {
            try {
                val py = Python.getInstance()
                val engine = py.getModule("translate.engine")
                val providers = py.getModule("translate.providers")
                val provider = if (providerName() == "agnes") {
                    val cls = providers.get("AgnesProvider")
                    cls.callAttr("__call__")
                } else {
                    providers.get("PassthroughProvider").callAttr("__call__")
                }
                val results = engine.callAttr("translate_strings",
                    extractedStrings, langCode(), "en", provider)
                val done = engine.callAttr("results_to_dict", results)
                @Suppress("UNCHECKED_CAST")
                val translated = done.asMap().mapValues { it.value.toString() }
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

    private fun patchApk() {
        if (translatedStrings.isEmpty()) return
        setBusy(true, "Patching APK…")
        thread {
            try {
                val py = Python.getInstance()
                val patcher = py.getModule("core.patcher")
                val outDir = File(cacheDir, "output").apply { mkdirs() }
                val outApk = File(outDir, apkName.replace(".apk", "-id.apk"))
                patcher.callAttr("patch_apk",
                    File(File(cacheDir, "extract"), apkName).absolutePath,
                    translatedStrings, langCode(), outApk.absolutePath)
                runOnUiThread {
                    setBusy(false)
                    binding.tvStatus.text = "Done: ${outApk.name}"
                    Toast.makeText(this, "Patched APK ready",
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
        binding.btnPickApk.isEnabled = !busy
        binding.btnExtract.isEnabled = !busy && apkUri != null
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
