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
import org.kuropatch.cloud.CloudApiError
import org.kuropatch.cloud.CloudRepository
import org.kuropatch.cloud.CloudResult
import org.kuropatch.cloud.JobPollResponse
import org.kuropatch.cloud.LibraryEntryDto
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
        // DISPLAY_NAME comes from the document provider: a hostile or
        // buggy provider could return "../evil.jar" or "a/b.jar", and
        // File(cacheDir, jarName) would then escape the extract dir.
        // Keep only the last path segment; "." / ".." / blank fall back.
        val name = (queryName(uri) ?: "game.jar")
            .substringAfterLast('/').substringAfterLast('\\')
            .takeIf { it.isNotBlank() && it != "." && it != ".." } ?: "game.jar"
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
        translatedLangCode = ""
        translatedProvider = ""
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
        // A new pick invalidates any in-flight cloud job for the old jar:
        // the poller would otherwise download jar A's result after the
        // user moved on to jar B.
        if (cloudPolling) cancelCloudJob()
        updateCloudUi()
        if (isCloudMode()) binding.btnTranslate.isEnabled = true
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
        // Cloud mode reroutes Translate to the server flow; the local
        // Chaquopy pipeline is untouched. While a cloud job polls, the
        // same button becomes Cancel.
        binding.btnTranslate.setOnClickListener {
            if (cloudPolling) cancelCloudJob()
            else if (isCloudMode()) cloudTranslate()
            else translateStrings()
        }
        // Change-key reuses the first-time prompt: after Save the
        // prompt hands the new key to Python and continues into
        // translateStrings() (a no-op when nothing is extracted).
        binding.btnChangeKey.setOnClickListener { promptAgnesKey() }
        binding.btnPatch.setOnClickListener { patchJar() }
        binding.btnSave.setOnClickListener { launchSaveDialog() }
        binding.btnShare.setOnClickListener { shareOutput() }
        // Cloud mode controls (default OFF). isPressed guard: programmatic
        // setChecked from updateCloudUi() must not re-trigger the handler.
        binding.switchCloud.setOnCheckedChangeListener { button, checked ->
            if (!button.isPressed) return@setOnCheckedChangeListener
            prefs().edit().putBoolean("cloud_mode", checked).apply()
            if (!checked && cloudPolling) cancelCloudJob()
            updateCloudUi()
        }
        binding.btnCloudKey.setOnClickListener { promptCloudLlmKey() }
        binding.btnCloudLibrary.setOnClickListener { showCloudLibrary() }
        updateCloudUi()
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
        translatedLangCode = ""
        binding.btnPatch.isEnabled = false
        // Fast operation: keep the bar indeterminate.
        binding.progressBar.isIndeterminate = true
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
                // collect_strings may warn (e.g. a TRAD_english.loc
                // table that exists but is not Castlevania-format):
                // the user must know story translation was dropped,
                // not silently see fewer strings. Best-effort: a
                // failure reading warnings must not fail extraction.
                val collectWarnings = try {
                    result.asMap()
                        .entries.firstOrNull { it.key.toString() == "warnings" }
                        ?.value?.asList()?.map { it.toString() }.orEmpty()
                } catch (e: Exception) {
                    android.util.Log.w("KuroPatch", "collect warnings unreadable", e)
                    emptyList()
                }
                safeUi {
                    setBusy(false)
                    binding.tvStatus.text =
                        "${extractedStrings.size} strings found" +
                        if (collectWarnings.isNotEmpty())
                            " — " + collectWarnings.joinToString("; ")
                        else ""
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
        // Determinate progress for translate: the bar shows REAL
        // per-string % (engine on_progress callback), not the
        // indeterminate animation -- which showed fake movement.
        binding.progressBar.isIndeterminate = false
        binding.progressBar.progress = 0
        setBusy(true, "Translating…")
        exportAgnesKey()
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
                // Determinate progress: a plain object with a PUBLIC
                // onProgress(done, total). Python tries a direct call
                // first, then .onProgress(done, total). Invoked on
                // this background thread: NEVER touch views here --
                // post via safeUi (activity may be destroyed).
                // Percentage is REAL per-string completion only.
                val progressCallback = object {
                    fun onProgress(done: Int, total: Int) {
                        safeUi {
                            if (total > 0) {
                                binding.progressBar.progress =
                                    (done * 100 / total).coerceIn(0, 100)
                            }
                        }
                    }
                }
                // Positional args through on_progress: strings,
                // target_lang, source_lang, provider, cache=None,
                // glossary=None, max_retries=1, on_progress=callback.
                // All other args identical to the previous call.
                val results = engine.callAttr("translate_strings",
                    toPyDict(py, extractedStrings), langCode(), "en", provider,
                    null, null, 1, progressCallback)
                val done = engine.callAttr("results_to_dict", results)
                val translated: Map<String, String> = done.asMap()
                    .map { it.key.toString() to it.value.toString() }.toMap()
                // results_summary is REQUIRED, not optional: the dict
                // above cannot distinguish "provider returned this
                // verbatim" from "provider FAILED and source was kept".
                // Without it a dead provider shows as "0 translated,
                // N unchanged" with no error at all (Sims 3 on-device).
                val summaryMap = engine.callAttr("results_summary", results).asMap()
                    .map { it.key.toString() to it.value.toString() }.toMap()
                val failed = summaryMap["failed"]?.toIntOrNull() ?: 0
                val firstError = summaryMap["first_error"] ?: ""
                safeUi {
                    setBusy(false)
                    // "Translated" counts only strings whose text actually
                    // changed: skipped/glossary-verbatim entries are not
                    // translations, and reporting them as such ("97
                    // strings translated" when 72 are verbatim) misleads.
                    val n = translated.size
                    val changed = translated.count { (k, v) -> k != v }
                    binding.tvStatus.text =
                        "$changed strings translated, ${n - changed - failed} unchanged" +
                        if (failed > 0) ", $failed failed" else ""
                    // Only stash when something actually CHANGED.
                    // changed == 0 covers BOTH failure modes with zero
                    // translations: total provider failure (failed > 0)
                    // and all-skipped / verbatim (failed == 0, e.g.
                    // Sims 3 with a working provider). In either case
                    // the map holds zero translations -- patching with
                    // it would produce an untranslated jar labelled
                    // _id, and it would also DESTROY a previous good
                    // translation (e.g. a failed re-translate after
                    // switching language). Keep the previous
                    // translations (if any) and leave Patch in its
                    // setBusy-computed state.
                    if (changed > 0) {
                        binding.btnPatch.isEnabled = true
                        // stash for patch step
                        translatedStrings = translated
                        translatedLangCode = langCode()
                        translatedProvider = providerName()
                    }
                    // Stale stash: a failed / no-op re-translate
                    // (changed == 0) for a DIFFERENT language or
                    // provider must not leave Patch serving the
                    // previous translations -- e.g. translate to
                    // Indonesian, switch provider to Passthrough,
                    // tap Translate ("0 strings translated"), tap
                    // Patch, and get the OLD Indonesian jar with no
                    // hint anything was stale. Drop the stash and
                    // say so. A same-language/provider failure
                    // keeps the stash: retrying a dead provider
                    // must not destroy a good translation.
                    if (changed == 0 && translatedStrings.isNotEmpty() &&
                        (langCode() != translatedLangCode ||
                            providerName() != translatedProvider)) {
                        translatedStrings = emptyMap()
                        translatedLangCode = ""
                        translatedProvider = ""
                        binding.btnPatch.isEnabled = false
                        binding.tvStatus.text =
                            binding.tvStatus.text.toString() +
                            " — previous translations discarded " +
                            "(different language/provider)."
                    }
                    // ANY failure must be LOUD, not just total failure:
                    // v2.0.11 Castlevania showed "26 translated, 327
                    // failed" with NO reason -- the dialog only fired
                    // when changed == 0. Partial translations stay
                    // available for patching; the user still learns
                    // WHY the rest failed.
                    if (failed > 0) {
                        val detail = if (firstError.isNotEmpty()) firstError
                            else "Provider failed — check your API key and connection."
                        if (changed == 0) {
                            showError("Translate failed", detail)
                        } else {
                            showError("Translate partially failed",
                                "$failed string(s) failed: $detail " +
                                "Partial translations are still available for patching.")
                        }
                    }
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
    /** Language the current translations were made for. The output
     * filename must use THIS, not the spinner's current value: if the
     * user switches language after translating, patching with the
     * spinner value would mislabel e.g. Indonesian output as _en. */
    private var translatedLangCode: String = ""
    /** Provider the current translations were made with. Paired
     * with translatedLangCode for the stale-stash check below:
     * a failed re-translate for a DIFFERENT language/provider must
     * not leave Patch serving the previous translations. */
    private var translatedProvider: String = ""

    // ================= Cloud mode (kuro.eksi.biz.id) =================
    // ADDITIVE section: the local pipeline above is not modified. When
    // the cloud toggle is ON, Translate uploads the .jar to the server
    // (extract -> translate -> repack all server-side) and downloads
    // the verified result. Default OFF.
    private val cloudRepo by lazy {
        CloudRepository(
            ownerToken = { BuildConfig.KURO_OWNER_TOKEN },
            llmApiKey = { getCloudLlmKey() },
            llmModel = { getCloudLlmModel() },
        )
    }
    @Volatile private var cloudPolling = false
    private var cloudJobId: String? = null
    private var cloudNetFailures = 0

    private fun prefs() = getSharedPreferences("kuropatch", MODE_PRIVATE)
    private fun isCloudMode(): Boolean = prefs().getBoolean("cloud_mode", false)
    private fun getCloudLlmKey(): String =
        prefs().getString("cloud_llm_key", "") ?: ""
    private fun getCloudLlmModel(): String =
        prefs().getString("cloud_llm_model", "")?.takeIf { it.isNotBlank() }
            ?: CloudRepository.DEFAULT_CLOUD_MODEL

    /** Sync button visibility/enabled state with the cloud toggle. */
    private fun updateCloudUi() {
        val cloud = isCloudMode()
        binding.switchCloud.isChecked = cloud
        binding.btnCloudKey.visibility = if (cloud) View.VISIBLE else View.GONE
        // The Agnes key button belongs to the local pipeline; in cloud
        // mode the cloud key button takes its place.
        binding.btnChangeKey.visibility = if (cloud) View.GONE else View.VISIBLE
        if (cloudPolling) return // poll loop owns the buttons right now
        if (cloud) {
            // Cloud flow bypasses local extract/patch entirely.
            binding.btnExtract.isEnabled = false
            binding.btnPatch.isEnabled = false
        } else {
            binding.btnExtract.isEnabled = jarUri != null
            binding.btnPatch.isEnabled = translatedStrings.isNotEmpty()
        }
    }

    /** Cloud-flow busy state. Separate from setBusy() (local flow). */
    private fun setCloudBusy(busy: Boolean, msg: String = "") {
        binding.progressBar.visibility = if (busy) View.VISIBLE else View.GONE
        if (msg.isNotEmpty()) binding.tvStatus.text = msg
        val idle = !busy
        binding.btnPickJar.isEnabled = idle
        binding.btnExtract.isEnabled = false // cloud never uses local extract
        binding.btnPatch.isEnabled = false   // ...nor local patch
        // The Translate button doubles as Cancel while polling.
        binding.btnTranslate.isEnabled = idle || cloudPolling
        binding.btnTranslate.text =
            if (cloudPolling) "Cancel cloud job" else "Auto-translate"
        binding.btnChangeKey.isEnabled = idle
        binding.btnCloudKey.isEnabled = idle
        binding.btnCloudLibrary.isEnabled = idle
        binding.switchCloud.isEnabled = idle
        val hasOutput = patchedJar?.exists() == true
        binding.btnSave.isEnabled = idle && hasOutput
        binding.btnShare.isEnabled = idle && hasOutput
    }

    /** Entry point for Translate in cloud mode. */
    private fun cloudTranslate() {
        if (cloudPolling) return // click router sends this to cancel instead
        val uri = jarUri
        if (uri == null) {
            showError("Pick a .jar first", "Cloud translate needs a picked .jar.")
            return
        }
        // Fail-closed: a public build without the injected owner token
        // must never send an unauthenticated request.
        if (BuildConfig.KURO_OWNER_TOKEN.isBlank()) {
            showError("Cloud not configured",
                "This build has no cloud owner token. Cloud mode needs a " +
                    "build with KURO_OWNER_TOKEN set.")
            return
        }
        // Cloud v1 is EN->ID only; the backend 400s anything else.
        // Fail LOUD rather than silently switching the user's language.
        if (langCode() != "id") {
            showError("Cloud v1: English → Indonesian only",
                "Pick Indonesian as the target language for cloud translate, " +
                    "or turn cloud mode off for other languages.")
            return
        }
        if (getCloudLlmKey().isBlank()) {
            promptCloudLlmKey()
            return
        }
        binding.progressBar.isIndeterminate = true
        binding.progressBar.progress = 0
        setCloudBusy(true, "Preparing cloud upload…")
        thread(name = "cloud-submit") {
            try {
                // by-hash + upload need a local file: copy via SAF.
                val tmp = File(cacheDir, "cloud").apply { mkdirs() }
                val jarFile = File(tmp, jarName)
                copyToCache(uri, jarFile)
                val sha = CloudRepository.sha256Hex(jarFile)
                safeUi { binding.tvStatus.text = "Checking cloud cache…" }
                when (val h = cloudRepo.byHash(sha)) {
                    is CloudResult.Ok -> {
                        val v = h.value
                        if (v.found && v.downloadUrl != null) {
                            // Cache hit: straight to verified download.
                            downloadCloudPath(v.downloadUrl)
                            return@thread
                        }
                        if (v.found && v.jobId != null) {
                            // Same-params job already running server-side:
                            // re-attach the poller, do NOT re-upload.
                            attachCloudPoll(v.jobId)
                            return@thread
                        }
                        uploadAndPoll(jarFile)
                    }
                    is CloudResult.ApiError ->
                        safeUi {
                            setCloudBusy(false)
                            showCloudErrorWithRetry("Cloud lookup failed", h.error) {
                                cloudTranslate()
                            }
                        }
                    is CloudResult.NetworkError ->
                        safeUi {
                            setCloudBusy(false)
                            showCloudErrorWithRetry("Cloud lookup failed",
                                CloudApiError("NETWORK", h.message, true, 0)) {
                                cloudTranslate()
                            }
                        }
                }
            } catch (e: Exception) {
                safeUi {
                    setCloudBusy(false)
                    showError("Cloud failed", e.message ?: "unknown error")
                }
            }
        }
    }

    /** Multipart upload, then attach the poller to the new job. */
    private fun uploadAndPoll(jarFile: File) {
        safeUi {
            binding.progressBar.isIndeterminate = true
            binding.tvStatus.text = "Uploading to cloud…"
        }
        val deviceId = android.provider.Settings.Secure.getString(
            contentResolver, android.provider.Settings.Secure.ANDROID_ID)
        when (val s = cloudRepo.submit(jarFile, deviceId)) {
            is CloudResult.Ok -> attachCloudPoll(s.value.jobId)
            is CloudResult.ApiError ->
                safeUi {
                    setCloudBusy(false)
                    showCloudErrorWithRetry("Cloud upload failed", s.error) {
                        cloudTranslate()
                    }
                }
            is CloudResult.NetworkError ->
                safeUi {
                    setCloudBusy(false)
                    showCloudErrorWithRetry("Cloud upload failed",
                        CloudApiError("NETWORK", s.message, true, 0)) {
                        cloudTranslate()
                    }
                }
        }
    }

    /** Poll GET /jobs/{id} every 2s; drive the determinate bar. */
    private fun attachCloudPoll(jobId: String) {
        cloudJobId = jobId
        cloudPolling = true
        cloudNetFailures = 0
        safeUi {
            binding.progressBar.isIndeterminate = false
            binding.progressBar.progress = 0
            binding.tvStatus.text = "Cloud job queued…"
            setCloudBusy(true)
        }
        thread(name = "cloud-poll") {
            while (cloudPolling) {
                when (val p = cloudRepo.poll(jobId)) {
                    is CloudResult.Ok -> {
                        cloudNetFailures = 0
                        val v = p.value
                        safeUi { updateCloudProgress(v) }
                        when (v.status) {
                            "COMPLETED" -> {
                                cloudPolling = false
                                downloadCloudJob(jobId)
                                return@thread
                            }
                            "FAILED" -> {
                                cloudPolling = false
                                val msg = v.errorMsg ?: "unknown error"
                                safeUi {
                                    setCloudBusy(false)
                                    showError("Cloud job failed", msg)
                                }
                                return@thread
                            }
                            // QUEUED / EXTRACTING / TRANSLATING / REPACKING:
                            // keep polling.
                        }
                    }
                    is CloudResult.ApiError -> {
                        cloudPolling = false
                        safeUi {
                            setCloudBusy(false)
                            if (p.error.httpCode == 404) {
                                showError("Cloud job gone",
                                    "The job expired or was deleted server-side.")
                            } else {
                                showCloudErrorWithRetry("Cloud poll failed", p.error) {
                                    cloudTranslate()
                                }
                            }
                        }
                        return@thread
                    }
                    is CloudResult.NetworkError -> {
                        // Transient blips must not kill a long job:
                        // tolerate a few, then fail with a retry option.
                        cloudNetFailures++
                        if (cloudNetFailures >= 5) {
                            cloudPolling = false
                            safeUi {
                                setCloudBusy(false)
                                showCloudErrorWithRetry("Cloud poll failed",
                                    CloudApiError("NETWORK", p.message, true, 0)) {
                                    cloudTranslate()
                                }
                            }
                            return@thread
                        }
                    }
                }
                try {
                    Thread.sleep(2000)
                } catch (ie: InterruptedException) {
                    return@thread
                }
            }
        }
    }

    /** Determinate progress from server counts (v2.0.13 bar). */
    private fun updateCloudProgress(v: JobPollResponse) {
        val pr = v.progress
        binding.progressBar.isIndeterminate = false
        binding.progressBar.progress = pr.percent.toInt().coerceIn(0, 100)
        binding.tvStatus.text =
            "Cloud ${v.status.lowercase()}: ${pr.percent.toInt()}% " +
                "(${pr.done}/${pr.total} strings, ${pr.cached} cached)"
        // Refresh the Cancel label while polling.
        setCloudBusy(true)
    }

    /** Cancel: stop the poller, best-effort DELETE server-side. */
    private fun cancelCloudJob() {
        val id = cloudJobId
        cloudPolling = false
        cloudJobId = null
        if (id != null) thread(name = "cloud-cancel") {
            try {
                cloudRepo.deleteJob(id)
            } catch (e: Exception) {
                android.util.Log.w("KuroPatch", "cloud cancel failed", e)
            }
        }
        setCloudBusy(false)
        binding.tvStatus.text = "Cloud job cancelled"
    }

    /** Download a COMPLETED job result (SHA-256 verified). */
    private fun downloadCloudJob(jobId: String) {
        safeUi {
            binding.progressBar.isIndeterminate = true
            binding.tvStatus.text = "Downloading result…"
        }
        when (val d = cloudRepo.downloadJob(jobId, cloudOutputFile())) {
            is CloudResult.Ok -> safeUi { onCloudDownloaded(d.value.file) }
            is CloudResult.ApiError ->
                safeUi {
                    setCloudBusy(false)
                    // Retry must hop off the UI thread: downloadCloudJob
                    // does blocking network I/O.
                    showCloudErrorWithRetry("Cloud download failed", d.error) {
                        thread(name = "cloud-dl-retry") { downloadCloudJob(jobId) }
                    }
                }
            is CloudResult.NetworkError ->
                safeUi {
                    setCloudBusy(false)
                    showCloudErrorWithRetry("Cloud download failed",
                        CloudApiError("NETWORK", d.message, true, 0)) {
                        thread(name = "cloud-dl-retry") { downloadCloudJob(jobId) }
                    }
                }
        }
    }

    /** Download via a by-hash hit's relative download_url (cache-hit path). */
    private fun downloadCloudPath(relativeUrl: String) {
        safeUi {
            binding.progressBar.isIndeterminate = true
            binding.tvStatus.text = "Cloud cache hit — downloading…"
        }
        when (val d = cloudRepo.downloadFromPath(relativeUrl, cloudOutputFile())) {
            is CloudResult.Ok -> safeUi { onCloudDownloaded(d.value.file) }
            is CloudResult.ApiError ->
                safeUi {
                    setCloudBusy(false)
                    showCloudErrorWithRetry("Cloud download failed", d.error) {
                        cloudTranslate()
                    }
                }
            is CloudResult.NetworkError ->
                safeUi {
                    setCloudBusy(false)
                    showCloudErrorWithRetry("Cloud download failed",
                        CloudApiError("NETWORK", d.message, true, 0)) {
                        cloudTranslate()
                    }
                }
        }
    }

    private fun cloudOutputFile(): File {
        val base = if (jarName.endsWith(".jar", ignoreCase = true))
            jarName.dropLast(4) else jarName.ifBlank { "game" }
        return File(outputDir(), "${base}_id_cloud.jar")
    }

    /** A verified cloud result reuses the local Save/Share flow. */
    private fun onCloudDownloaded(file: File) {
        setCloudBusy(false)
        cloudJobId = null
        patchedJar = file
        binding.tvStatus.text =
            "Done: ${file.name} (${file.length() / 1024} KB) — pick where to save it"
        binding.btnSave.isEnabled = true
        binding.btnShare.isEnabled = true
        if (!isFinishing && !isDestroyed) launchSaveDialog()
    }

    /** Error dialog; retryable errors offer Retry (restarts the flow). */
    private fun showCloudErrorWithRetry(
        title: String,
        error: CloudApiError,
        retry: () -> Unit,
    ) {
        val b = MaterialAlertDialogBuilder(this)
            .setTitle(title)
            .setMessage("${error.code}: ${error.message}")
            .setPositiveButton("OK", null)
        if (error.retryable) {
            b.setNeutralButton("Retry") { _, _ -> retry() }
        }
        b.show()
    }

    /** Cloud LLM key dialog (ai.eksi.biz.id gateway key + model). */
    private fun promptCloudLlmKey() {
        val keyInput = android.widget.EditText(this).apply {
            hint = "ai.eksi.biz.id API key"
            inputType = android.text.InputType.TYPE_CLASS_TEXT or
                android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD
            setText(getCloudLlmKey())
        }
        val modelInput = android.widget.EditText(this).apply {
            hint = "Model"
            val saved = prefs().getString("cloud_llm_model", "").orEmpty()
            setText(saved.ifBlank { CloudRepository.DEFAULT_CLOUD_MODEL })
        }
        val layout = android.widget.LinearLayout(this).apply {
            orientation = android.widget.LinearLayout.VERTICAL
            setPadding(48, 16, 48, 0)
            addView(keyInput)
            addView(modelInput)
        }
        MaterialAlertDialogBuilder(this)
            .setTitle("Cloud LLM key")
            .setMessage("API key for the ai.eksi.biz.id gateway, used ONLY for " +
                "cloud translate. This is SEPARATE from your Agnes key: the " +
                "cloud server forwards it to the gateway and cannot use " +
                "Agnes keys.")
            .setView(layout)
            .setPositiveButton("Save") { _, _ ->
                val key = keyInput.text.toString().trim()
                if (key.isEmpty()) {
                    Toast.makeText(this, "API key cannot be empty",
                        Toast.LENGTH_SHORT).show()
                    return@setPositiveButton
                }
                val model = modelInput.text.toString().trim()
                    .ifEmpty { CloudRepository.DEFAULT_CLOUD_MODEL }
                prefs().edit()
                    .putString("cloud_llm_key", key)
                    .putString("cloud_llm_model", model)
                    .apply()
                Toast.makeText(this, "Cloud key saved", Toast.LENGTH_SHORT).show()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    /** List server-side library entries; tap to download one. */
    private fun showCloudLibrary() {
        if (BuildConfig.KURO_OWNER_TOKEN.isBlank()) {
            showError("Cloud not configured", "This build has no cloud owner token.")
            return
        }
        setCloudBusy(true, "Loading cloud library…")
        thread(name = "cloud-library") {
            when (val r = cloudRepo.listLibrary()) {
                is CloudResult.Ok ->
                    safeUi {
                        setCloudBusy(false)
                        showLibraryDialog(r.value)
                    }
                is CloudResult.ApiError ->
                    safeUi {
                        setCloudBusy(false)
                        showCloudErrorWithRetry("Cloud library failed", r.error) {
                            showCloudLibrary()
                        }
                    }
                is CloudResult.NetworkError ->
                    safeUi {
                        setCloudBusy(false)
                        showError("Cloud library failed", r.message)
                    }
            }
        }
    }

    private fun showLibraryDialog(entries: List<LibraryEntryDto>) {
        if (entries.isEmpty()) {
            showError("Cloud library", "No cloud results yet.")
            return
        }
        val names = entries.map { e ->
            "${e.jarName.ifBlank { "result.jar" }} — " +
                "${e.stringCount} strings, ${e.sizeBytes / 1024} KB"
        }.toTypedArray()
        MaterialAlertDialogBuilder(this)
            .setTitle("Cloud library")
            .setItems(names) { _, which -> downloadLibraryEntry(entries[which]) }
            .setNegativeButton("Close", null)
            .show()
    }

    private fun downloadLibraryEntry(entry: LibraryEntryDto) {
        setCloudBusy(true, "Downloading ${entry.jarName}…")
        thread(name = "cloud-lib-dl") {
            val dest = File(outputDir(),
                entry.jarName.ifBlank { "cloud_result.jar" })
            when (val d = cloudRepo.downloadLibraryEntry(entry, dest)) {
                is CloudResult.Ok -> safeUi { onCloudDownloaded(d.value.file) }
                is CloudResult.ApiError ->
                    safeUi {
                        setCloudBusy(false)
                        showCloudErrorWithRetry("Download failed", d.error) {
                            downloadLibraryEntry(entry)
                        }
                    }
                is CloudResult.NetworkError ->
                    safeUi {
                        setCloudBusy(false)
                        showError("Download failed", d.message)
                    }
            }
        }
    }
    // =============== end Cloud mode ===============

    private fun patchJar() {
        if (translatedStrings.isEmpty()) return
        // Fast operation: keep the bar indeterminate.
        binding.progressBar.isIndeterminate = true
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
                // Filename uses the TRANSLATE-time language, not the
                // spinner's current value (see translatedLangCode).
                val outJar = File(outDir, "${base}_${translatedLangCode}.jar")
                val statsResult = jarMod.callAttr("patch_jar",
                    jarFile.absolutePath, toPyDict(py, translatedStrings),
                    outJar.absolutePath)
                // patch_jar's stats must NOT be discarded: a pack
                // rebuild failure or .loc build failure still
                // returns "success" (the file is left untouched,
                // fail-safe) but the user would otherwise see a
                // bare "Done" with no idea story / pack coverage
                // was dropped. Read the stats and surface them;
                // any failure reading stats must not fail the
                // patch itself, so this is fully best-effort.
                val statsNote = try {
                    val stats = statsResult.asMap()
                        .map { it.key.toString() to it.value }.toMap()
                    val notes = mutableListOf<String>()
                    val packsSkipped = stats["packs_skipped"]?.toInt() ?: 0
                    if (packsSkipped > 0) {
                        notes += "packs skipped: $packsSkipped " +
                            "(rebuild failed, left untranslated)"
                    }
                    val locError = stats["loc_error"]?.toString().orEmpty()
                    if (locError.isNotEmpty()) {
                        notes += ".loc error: $locError"
                    }
                    val warnings = stats["warnings"]?.asList()
                        ?.map { it.toString() }.orEmpty()
                    if (warnings.isNotEmpty()) {
                        notes += warnings
                    }
                    if (notes.isNotEmpty()) " — " + notes.joinToString("; ") else ""
                } catch (e: Exception) {
                    android.util.Log.w("KuroPatch", "patch stats unreadable", e)
                    ""
                }
                safeUi {
                    setBusy(false)
                    patchedJar = outJar
                    binding.tvStatus.text =
                        "Done: ${outJar.name} (${outJar.length() / 1024} KB) — pick where to save it" +
                        statsNote
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
            // The file may have been deleted externally after a
            // successful patch -- "patch again" is the true remedy,
            // not "patch a .jar first" (they already did).
            showError("Nothing to save", "Patched .jar is gone — patch again.")
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

    // Hand the BYOK key to Chaquopy Python explicitly.
    //
    // Os.setenv ALONE IS NOT ENOUGH (proven on-device bug, v2.0.9):
    // CPython's os.environ is a snapshot from interpreter startup, so
    // a libc setenv() from Kotlin never reaches Python's os.environ
    // dict -- AgnesProvider._api_key() always saw "" and translate
    // failed even with a saved key. The explicit
    // translate.providers.set_api_key() call is the real handoff;
    // Os.setenv stays as belt-and-braces (harmless).
    // Called before every translate AND right after a new key is
    // saved, so the provider always has the current key.
    private fun exportAgnesKey() {
        try {
            android.system.Os.setenv("AGNES_API_KEY", getAgnesKey(), true)
        } catch (e: Exception) {
            android.util.Log.w("KuroPatch", "Os.setenv failed", e)
        }
        try {
            Python.getInstance().getModule("translate.providers")
                .callAttr("set_api_key", getAgnesKey())
        } catch (e: Exception) {
            android.util.Log.w("KuroPatch", "set_api_key failed", e)
        }
    }

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
            .setMessage("Paste your Agnes API key. Get a free key from " +
                "your Agnes AI account (apihub.agnes-ai.com). " +
                "Your key stays on this device only.")
            .setView(input)
            .setPositiveButton("Save") { _, _ ->
                val key = input.text.toString().trim()
                // Blank Save must NOT save "" and recurse into
                // translateStrings() -> promptAgnesKey() again with no
                // explanation (an infinite no-feedback loop). Tell the
                // user, stay put; tapping Translate re-opens the prompt.
                if (key.isEmpty()) {
                    Toast.makeText(this, "API key cannot be empty",
                        Toast.LENGTH_SHORT).show()
                    return@setPositiveButton
                }
                getSharedPreferences("kuropatch", MODE_PRIVATE).edit()
                    .putString("agnes_key", key).apply()
                // Hand the new key to Python NOW (not just at the
                // next translate call) so the provider has it.
                exportAgnesKey()
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
        binding.btnChangeKey.isEnabled = !busy
        binding.btnPatch.isEnabled = !busy && translatedStrings.isNotEmpty()
        val hasOutput = patchedJar?.exists() == true
        binding.btnSave.isEnabled = !busy && hasOutput
        binding.btnShare.isEnabled = !busy && hasOutput
        // Cloud controls are separate UI but share the activity: lock them
        // during local work so the mode can't flip mid-translate (the
        // local thread's setBusy(false) would otherwise leave buttons in
        // the wrong mode's state).
        binding.btnCloudKey.isEnabled = !busy
        binding.btnCloudLibrary.isEnabled = !busy
        binding.switchCloud.isEnabled = !busy
    }

    private fun showError(title: String, msg: String) {
        MaterialAlertDialogBuilder(this)
            .setTitle(title)
            .setMessage(msg)
            .setPositiveButton("OK", null)
            .show()
    }
}
