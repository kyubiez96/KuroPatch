package org.kuropatch

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.DocumentsContract
import android.provider.OpenableColumns
import android.text.method.ScrollingMovementMethod
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
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
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import com.google.android.material.textfield.TextInputEditText
import com.google.android.material.textfield.TextInputLayout
import java.io.File
import java.text.DateFormat
import java.util.Date
import kotlin.concurrent.thread

/**
 * KuroPatch UI: batch queue of .jar files, per-string preview/review,
 * and one-tap re-patch from history — all on-device via Chaquopy.
 *
 * .apk files are refused with an explanation: the full APK pipeline needs
 * apktool, which requires a real JVM and cannot run on Android. For APKs the
 * PC CLI (python apk.py) is the way.
 */
class MainActivity : AppCompatActivity() {

    companion object {
        private const val PREFS_NAME = "kuropatch_prefs"
        private const val KEY_OUT_TREE = "output_tree_uri"
        private const val KEY_OUT_NAME = "output_tree_name"
    }

    data class QueueItem(val file: File, var status: String = "queued")

    // -- views -----------------------------------------------------------------
    private lateinit var btnPick: Button
    private lateinit var recyclerQueue: RecyclerView
    private lateinit var btnPreview: Button
    private lateinit var spinnerSource: Spinner
    private lateinit var spinnerTarget: Spinner
    private lateinit var spinnerProvider: Spinner
    private lateinit var apiKeyLayout: TextInputLayout
    private lateinit var etApiKey: TextInputEditText
    private lateinit var cbTranslateAll: CheckBox
    private lateinit var btnStart: Button
    private lateinit var btnStop: Button
    private lateinit var btnHistory: Button
    private lateinit var tvStatus: TextView
    private lateinit var progressBar: ProgressBar
    private lateinit var tvLog: TextView
    private lateinit var tvOutput: TextView
    private lateinit var btnShare: Button
    private lateinit var tvOutFolder: TextView
    private lateinit var btnChangeFolder: Button
    private lateinit var btnResetFolder: Button

    // -- state -----------------------------------------------------------------
    private val queue = mutableListOf<QueueItem>()
    private lateinit var queueAdapter: QueueAdapter
    private var outputFile: File? = null
    private var running = false
    private var cancelled = false
    private var overridesJson: String? = null

    // Per-file result stashed by the bridge listener (called on worker thread).
    @Volatile private var lastSuccess = false
    @Volatile private var lastOutput = ""
    @Volatile private var lastMessage = ""

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

    // -- output folder (SAF tree, user-configurable) -------------------------------
    private val prefs by lazy { getSharedPreferences(PREFS_NAME, MODE_PRIVATE) }

    private fun outputTreeUri(): Uri? =
        prefs.getString(KEY_OUT_TREE, null)?.let { Uri.parse(it) }

    private fun treeDisplayName(treeUri: Uri): String? = try {
        val docId = DocumentsContract.getTreeDocumentId(treeUri)
        val docUri = DocumentsContract.buildDocumentUriUsingTree(treeUri, docId)
        contentResolver.query(
            docUri,
            arrayOf(DocumentsContract.Document.COLUMN_DISPLAY_NAME),
            null, null, null,
        )?.use { cursor -> if (cursor.moveToFirst()) cursor.getString(0) else null }
    } catch (e: Exception) {
        null
    }

    private fun refreshOutputFolder() {
        val custom = prefs.getString(KEY_OUT_NAME, null)
        tvOutFolder.text = custom ?: getString(R.string.output_folder_default)
        btnResetFolder.isEnabled = custom != null
    }

    private fun resetOutputFolder() {
        outputTreeUri()?.let { uri ->
            try {
                contentResolver.releasePersistableUriPermission(
                    uri,
                    Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION,
                )
            } catch (e: Exception) {
                // Permission already gone; nothing to release.
            }
        }
        prefs.edit().remove(KEY_OUT_TREE).remove(KEY_OUT_NAME).apply()
        refreshOutputFolder()
        appendLog("[*] Output folder reset to default")
    }

    /**
     * Copy a finished output file into the user-chosen folder (SAF tree).
     * Worker-thread safe: touches no views. Returns the display label, or
     * null when no custom folder is set or the copy failed (the caller then
     * keeps the app-folder copy).
     */
    private fun copyToCustomFolder(src: File): String? {
        val treeUri = outputTreeUri() ?: return null
        return try {
            val docId = DocumentsContract.getTreeDocumentId(treeUri)
            val dirUri = DocumentsContract.buildDocumentUriUsingTree(treeUri, docId)
            val newUri = DocumentsContract.createDocument(
                contentResolver, dirUri, "application/java-archive", src.name,
            ) ?: throw IllegalStateException("createDocument returned null")
            contentResolver.openOutputStream(newUri)?.use { out ->
                src.inputStream().use { inp -> inp.copyTo(out) }
            } ?: throw IllegalStateException("cannot open output stream")
            val folder = prefs.getString(KEY_OUT_NAME, null) ?: "folder"
            "$folder/${src.name}"
        } catch (e: Exception) {
            null
        }
    }

    // -- pickers -----------------------------------------------------------------
    private val pickOutputTree =
        registerForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri: Uri? ->
            if (uri == null) return@registerForActivityResult
            try {
                contentResolver.takePersistableUriPermission(
                    uri,
                    Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION,
                )
                val name = treeDisplayName(uri) ?: uri.lastPathSegment.orEmpty()
                prefs.edit()
                    .putString(KEY_OUT_TREE, uri.toString())
                    .putString(KEY_OUT_NAME, name)
                    .apply()
                appendLog("[*] Output folder: $name")
            } catch (e: Exception) {
                Toast.makeText(this, "Cannot use that folder: ${e.message}", Toast.LENGTH_SHORT).show()
            }
            refreshOutputFolder()
        }

    private val pickJars = registerForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris: List<Uri> ->
        if (uris.isEmpty()) return@registerForActivityResult
        var added = 0
        for (uri in uris) {
            try {
                val name = displayNameOf(uri) ?: "game.jar"
                if (name.endsWith(".apk", ignoreCase = true)) {
                    showApkNotice()
                    continue
                }
                val dest = File(File(filesDir, "input").apply { mkdirs() }, sanitize(name))
                contentResolver.openInputStream(uri)?.use { src ->
                    dest.outputStream().use { dst -> src.copyTo(dst) }
                } ?: throw IllegalStateException("empty stream")
                try {
                    contentResolver.takePersistableUriPermission(
                        uri, Intent.FLAG_GRANT_READ_URI_PERMISSION
                    )
                } catch (_: SecurityException) { /* best effort */ }
                if (queue.none { it.file.absolutePath == dest.absolutePath }) {
                    queue.add(QueueItem(dest))
                    added++
                }
            } catch (e: Exception) {
                appendLog("[!] ${getString(R.string.err_copy)}: ${e.message}")
            }
        }
        if (added > 0) {
            appendLog("[*] Queued $added file(s)")
            queueAdapter.notifyDataSetChanged()
            refreshButtons()
        }
    }

    private val previewLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK) {
            overridesJson = result.data?.getStringExtra(PreviewActivity.EXTRA_OVERRIDES_JSON)
            appendLog("[*] Preview overrides captured")
            Toast.makeText(this, getString(R.string.preview_ready), Toast.LENGTH_SHORT).show()
        }
    }

    // -- lifecycle ----------------------------------------------------------------
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        btnPick = findViewById(R.id.btnPick)
        recyclerQueue = findViewById(R.id.recyclerQueue)
        btnPreview = findViewById(R.id.btnPreview)
        spinnerSource = findViewById(R.id.spinnerSource)
        spinnerTarget = findViewById(R.id.spinnerTarget)
        spinnerProvider = findViewById(R.id.spinnerProvider)
        apiKeyLayout = findViewById(R.id.apiKeyLayout)
        etApiKey = findViewById(R.id.etApiKey)
        cbTranslateAll = findViewById(R.id.cbTranslateAll)
        btnStart = findViewById(R.id.btnStart)
        btnStop = findViewById(R.id.btnStop)
        btnHistory = findViewById(R.id.btnHistory)
        tvStatus = findViewById(R.id.tvStatus)
        progressBar = findViewById(R.id.progressBar)
        tvLog = findViewById(R.id.tvLog)
        tvOutput = findViewById(R.id.tvOutput)
        btnShare = findViewById(R.id.btnShare)
        tvOutFolder = findViewById(R.id.tvOutFolder)
        btnChangeFolder = findViewById(R.id.btnChangeFolder)
        btnResetFolder = findViewById(R.id.btnResetFolder)
        refreshOutputFolder()

        tvLog.movementMethod = ScrollingMovementMethod()

        queueAdapter = QueueAdapter(queue) { position -> removeFromQueue(position) }
        recyclerQueue.layoutManager = LinearLayoutManager(this)
        recyclerQueue.adapter = queueAdapter

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
            pickJars.launch(arrayOf("application/java-archive", "application/octet-stream", "*/*"))
        }
        btnPreview.setOnClickListener { openPreview() }
        btnStart.setOnClickListener { startPatching() }
        btnStop.setOnClickListener { cancelled = true }
        btnHistory.setOnClickListener { showHistory() }
        btnShare.setOnClickListener { shareOutput() }
        btnChangeFolder.setOnClickListener { pickOutputTree.launch(null) }
        btnResetFolder.setOnClickListener { resetOutputFolder() }
        refreshButtons()
    }

    // -- queue ----------------------------------------------------------------------
    private fun removeFromQueue(position: Int) {
        if (running) return
        if (position in queue.indices) {
            val removed = queue.removeAt(position)
            appendLog("[*] Removed ${removed.file.name}")
            queueAdapter.notifyItemRemoved(position)
            if (queue.size != 1) overridesJson = null
            refreshButtons()
        }
    }

    private fun openPreview() {
        if (queue.size != 1) {
            Toast.makeText(this, getString(R.string.preview_single_only), Toast.LENGTH_SHORT).show()
            return
        }
        val intent = Intent(this, PreviewActivity::class.java)
            .putExtra(PreviewActivity.EXTRA_INPUT_PATH, queue[0].file.absolutePath)
            .putExtra(PreviewActivity.EXTRA_TRANSLATE_ALL, cbTranslateAll.isChecked)
        previewLauncher.launch(intent)
    }

    private fun refreshButtons() {
        val idle = !running
        btnStart.isEnabled = idle && queue.isNotEmpty()
        btnPick.isEnabled = idle
        btnPreview.isEnabled = idle && queue.size == 1
        btnHistory.isEnabled = idle
        btnStop.isEnabled = running
        queueAdapter.notifyDataSetChanged()
    }

    // -- patching ------------------------------------------------------------------
    private fun startPatching() {
        if (running || queue.isEmpty()) return
        running = true
        cancelled = false
        btnShare.isEnabled = false
        tvLog.text = ""
        tvOutput.text = getString(R.string.no_output)
        outputFile = null
        refreshButtons()

        val source = languages[spinnerSource.selectedItemPosition].second
        val target = languages[spinnerTarget.selectedItemPosition].second
        val provider = providers[spinnerProvider.selectedItemPosition].second
        val apiKey = etApiKey.text?.toString().orEmpty().ifBlank { null }
        val translateAll = cbTranslateAll.isChecked
        val dryRun = provider == "none"
        val overrides = overridesJson // snapshot; cleared after the run

        val outDir = (getExternalFilesDir(null) ?: filesDir).resolve("KuroPatch").apply { mkdirs() }

        thread(isDaemon = true, name = "kuropatch") {
            try {
                if (!Python.isStarted()) {
                    Python.start(AndroidPlatform(this@MainActivity))
                }
                val bridge = Python.getInstance().getModule("android_bridge")
                var okCount = 0
                for ((index, item) in queue.withIndex()) {
                    if (cancelled) {
                        runOnUiThread { item.status = "cancelled" }
                        break
                    }
                    val input = item.file
                    val output = outDir.resolve("${input.nameWithoutExtension}_${target.uppercase()}.jar")
                    runOnUiThread {
                        item.status = "running"
                        queueAdapter.notifyItemChanged(index)
                        tvStatus.text = getString(R.string.batch_progress, index + 1, queue.size, input.name)
                        progressBar.progress = 0
                        appendLog("[*] [${index + 1}/${queue.size}] ${input.name}")
                    }
                    lastSuccess = false
                    lastOutput = ""
                    lastMessage = ""
                    try {
                        bridge.callAttr(
                            "run_patch",
                            input.absolutePath,
                            output.absolutePath,
                            outDir.absolutePath,
                            source,
                            target,
                            provider,
                            apiKey,
                            translateAll,
                            dryRun,
                            File(cacheDir, "kuropatch-work").absolutePath,
                            bridgeListener,
                            // Only a single-file run can carry preview overrides:
                            // a batch would apply one file's edits to another's strings.
                            if (queue.size == 1) overrides else null,
                        )
                    } catch (e: Exception) {
                        lastSuccess = false
                        lastMessage = "Failed: ${e.message}"
                    }
                    val status = if (lastSuccess) "done" else "error"
                    runOnUiThread {
                        item.status = status
                        queueAdapter.notifyItemChanged(index)
                        appendLog(if (lastSuccess) "[✓] ${item.file.name}: $lastMessage" else "[!] ${item.file.name}: $lastMessage")
                    }
                    if (lastSuccess) {
                        okCount++
                        if (!dryRun && lastOutput.isNotBlank()) {
                            HistoryStore.save(
                                this@MainActivity,
                                HistoryStore.Entry(
                                    inputName = input.name,
                                    inputPath = input.absolutePath,
                                    source = source,
                                    target = target,
                                    provider = provider,
                                    apiKey = apiKey,
                                    translateAll = translateAll,
                                    timestamp = System.currentTimeMillis(),
                                    outputPath = lastOutput,
                                ),
                            )
                            // Copy into the user-chosen folder (if any) on the
                            // worker thread; UI updates stay on the main thread.
                            val localFile = File(lastOutput).takeIf { it.isFile }
                            val copiedLabel = localFile?.let { copyToCustomFolder(it) }
                            val customSet = outputTreeUri() != null
                            runOnUiThread {
                                outputFile = localFile
                                if (copiedLabel != null) {
                                    tvOutput.text =
                                        getString(R.string.saved_to_folder, copiedLabel)
                                    appendLog("[✓] ${getString(R.string.saved_to_folder, copiedLabel)}")
                                } else {
                                    tvOutput.text = lastOutput
                                    if (customSet) {
                                        appendLog("[!] ${getString(R.string.copy_failed_kept)}")
                                    }
                                }
                                btnShare.isEnabled = outputFile != null
                            }
                        }
                    }
                }
                runOnUiThread {
                    onBatchDone(okCount, queue.size)
                }
            } catch (e: Exception) {
                runOnUiThread {
                    appendLog("[!] Failed: ${e.message}")
                    onBatchDone(0, queue.size)
                }
            }
        }
    }

    private fun onBatchDone(ok: Int, total: Int) {
        running = false
        cancelled = false
        overridesJson = null
        progressBar.progress = if (ok == total && total > 0) 100 else progressBar.progress
        tvStatus.text = getString(R.string.batch_done, ok, total)
        // Reset non-terminal states so a re-run starts clean; keep done/error.
        for ((i, item) in queue.withIndex()) {
            if (item.status == "running" || item.status == "cancelled" || item.status == "queued") {
                item.status = "queued"
                queueAdapter.notifyItemChanged(i)
            }
        }
        refreshButtons()
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
        }

        fun on_done(success: Boolean, outputPath: String, message: String) {
            lastSuccess = success
            lastOutput = outputPath
            lastMessage = message
        }
    }

    // -- history ----------------------------------------------------------------------
    private fun showHistory() {
        val entries = HistoryStore.load(this)
        if (entries.isEmpty()) {
            Toast.makeText(this, getString(R.string.history_empty), Toast.LENGTH_SHORT).show()
            return
        }
        val df = DateFormat.getDateTimeInstance(DateFormat.SHORT, DateFormat.SHORT)
        val labels = entries.map { "${it.label()}\n${df.format(Date(it.timestamp))}" }.toTypedArray()
        AlertDialog.Builder(this)
            .setTitle(R.string.history_title)
            .setItems(labels) { _, which -> repatch(entries[which]) }
            .setNeutralButton(R.string.history_clear) { _, _ ->
                HistoryStore.clear(this)
                Toast.makeText(this, getString(R.string.history_cleared), Toast.LENGTH_SHORT).show()
            }
            .setNegativeButton(R.string.dialog_cancel, null)
            .show()
    }

    /** One-tap re-patch: restore the entry's settings; run now if the file is still staged. */
    private fun repatch(entry: HistoryStore.Entry) {
        spinnerSource.setSelection(languages.indexOfFirst { it.second == entry.source }.coerceAtLeast(0))
        spinnerTarget.setSelection(languages.indexOfFirst { it.second == entry.target }.coerceAtLeast(0))
        spinnerProvider.setSelection(providers.indexOfFirst { it.second == entry.provider }.coerceAtLeast(0))
        etApiKey.setText(entry.apiKey.orEmpty())
        cbTranslateAll.isChecked = entry.translateAll
        val staged = File(entry.inputPath)
        if (staged.isFile) {
            queue.clear()
            queue.add(QueueItem(staged))
            queueAdapter.notifyDataSetChanged()
            appendLog("[*] Re-patch: ${entry.inputName} (${entry.source} → ${entry.target})")
            refreshButtons()
            startPatching()
        } else {
            Toast.makeText(this, getString(R.string.history_pick_new, entry.inputName), Toast.LENGTH_LONG).show()
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

    // -- queue adapter ------------------------------------------------------------------
    private class QueueAdapter(
        private val items: List<QueueItem>,
        private val onRemove: (Int) -> Unit,
    ) : RecyclerView.Adapter<QueueAdapter.Holder>() {

        class Holder(view: View) : RecyclerView.ViewHolder(view) {
            val tvName: TextView = view.findViewById(R.id.tvQueueName)
            val tvStatus: TextView = view.findViewById(R.id.tvQueueStatus)
            val btnRemove: Button = view.findViewById(R.id.btnQueueRemove)
        }

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): Holder {
            val view = LayoutInflater.from(parent.context)
                .inflate(R.layout.item_queue, parent, false)
            return Holder(view)
        }

        override fun getItemCount(): Int = items.size

        override fun onBindViewHolder(holder: Holder, position: Int) {
            val item = items[position]
            holder.tvName.text = item.file.name
            holder.tvStatus.text = item.status
            holder.btnRemove.setOnClickListener { onRemove(holder.bindingAdapterPosition) }
        }
    }

    private abstract class SimpleItemSelectedListener :
        android.widget.AdapterView.OnItemSelectedListener {
        override fun onNothingSelected(parent: android.widget.AdapterView<*>?) {}
        override fun onItemSelected(
            parent: android.widget.AdapterView<*>?, view: View?, position: Int, id: Long
        ) = onItemSelected(position)

        abstract fun onItemSelected(position: Int)
    }
}
