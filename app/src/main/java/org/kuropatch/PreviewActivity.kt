package org.kuropatch

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.text.Editable
import android.text.TextWatcher
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File
import kotlin.concurrent.thread
import org.json.JSONObject

/**
 * Review every translatable string before a patch run.
 *
 * Each row shows the original string and where it was found; the user may
 * type a manual translation (used verbatim, no provider call) or tick
 * "skip" to leave that string untouched. The result goes back to
 * MainActivity as a JSON object: {original: translation | null}.
 */
class PreviewActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_INPUT_PATH = "input_path"
        const val EXTRA_TRANSLATE_ALL = "translate_all"
        const val EXTRA_OVERRIDES_JSON = "overrides_json"
    }

    data class Row(
        val value: String,
        val origin: String,
        var override: String = "",
        var skip: Boolean = false,
    )

    private lateinit var recycler: RecyclerView
    private lateinit var progress: ProgressBar
    private lateinit var tvCount: TextView
    private lateinit var btnApply: Button
    private lateinit var btnCancel: Button
    private val rows = mutableListOf<Row>()
    private lateinit var adapter: PreviewAdapter

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_preview)

        recycler = findViewById(R.id.recyclerPreview)
        progress = findViewById(R.id.progressPreview)
        tvCount = findViewById(R.id.tvPreviewCount)
        btnApply = findViewById(R.id.btnPreviewApply)
        btnCancel = findViewById(R.id.btnPreviewCancel)

        adapter = PreviewAdapter(rows)
        recycler.layoutManager = LinearLayoutManager(this)
        recycler.adapter = adapter

        btnCancel.setOnClickListener { finish() }
        btnApply.setOnClickListener { applyAndFinish() }

        val inputPath = intent.getStringExtra(EXTRA_INPUT_PATH).orEmpty()
        val translateAll = intent.getBooleanExtra(EXTRA_TRANSLATE_ALL, false)
        if (inputPath.isBlank() || !File(inputPath).isFile) {
            Toast.makeText(this, getString(R.string.err_no_input), Toast.LENGTH_SHORT).show()
            finish()
            return
        }
        loadPreview(inputPath, translateAll)
    }

    private fun loadPreview(inputPath: String, translateAll: Boolean) {
        progress.visibility = View.VISIBLE
        thread(isDaemon = true, name = "kuropatch-preview") {
            try {
                if (!Python.isStarted()) {
                    Python.start(AndroidPlatform(this@PreviewActivity))
                }
                val bridge = Python.getInstance().getModule("android_bridge")
                val json = bridge.callAttr(
                    "preview_strings",
                    inputPath,
                    translateAll,
                    File(cacheDir, "kuropatch-work").absolutePath,
                    null,
                ).toString()
                val parsed = JSONObject(json)
                val error = parsed.optString("error")
                val items = parsed.optJSONArray("strings")
                runOnUiThread {
                    progress.visibility = View.GONE
                    if (error.isNotBlank()) {
                        Toast.makeText(this, error, Toast.LENGTH_LONG).show()
                        finish()
                        return@runOnUiThread
                    }
                    rows.clear()
                    if (items != null) {
                        for (i in 0 until items.length()) {
                            val o = items.getJSONObject(i)
                            rows.add(Row(o.optString("value"), o.optString("origin")))
                        }
                    }
                    adapter.notifyDataSetChanged()
                    tvCount.text = getString(R.string.preview_count, rows.size)
                    btnApply.isEnabled = rows.isNotEmpty()
                }
            } catch (e: Exception) {
                runOnUiThread {
                    progress.visibility = View.GONE
                    Toast.makeText(this, "Preview failed: ${e.message}", Toast.LENGTH_LONG).show()
                    finish()
                }
            }
        }
    }

    private fun applyAndFinish() {
        val overrides = JSONObject()
        var manual = 0
        var skipped = 0
        for (row in rows) {
            when {
                row.skip -> {
                    overrides.put(row.value, JSONObject.NULL)
                    skipped++
                }
                row.override.isNotBlank() -> {
                    overrides.put(row.value, row.override)
                    manual++
                }
            }
        }
        val result = Intent().putExtra(EXTRA_OVERRIDES_JSON, overrides.toString())
        setResult(Activity.RESULT_OK, result)
        Toast.makeText(
            this,
            getString(R.string.preview_applied, manual, skipped),
            Toast.LENGTH_SHORT,
        ).show()
        finish()
    }

    // -- adapter -------------------------------------------------------------------
    private class PreviewAdapter(private val rows: List<Row>) :
        RecyclerView.Adapter<PreviewAdapter.Holder>() {

        class Holder(view: View) : RecyclerView.ViewHolder(view) {
            val tvOriginal: TextView = view.findViewById(R.id.tvOriginal)
            val tvOrigin: TextView = view.findViewById(R.id.tvOrigin)
            val etOverride: EditText = view.findViewById(R.id.etOverride)
            val cbSkip: CheckBox = view.findViewById(R.id.cbSkip)
            var watcher: TextWatcher? = null
        }

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): Holder {
            val view = LayoutInflater.from(parent.context)
                .inflate(R.layout.item_preview, parent, false)
            return Holder(view)
        }

        override fun getItemCount(): Int = rows.size

        override fun onBindViewHolder(holder: Holder, position: Int) {
            val row = rows[position]
            holder.tvOriginal.text = row.value
            holder.tvOrigin.text = row.origin
            holder.watcher?.let { holder.etOverride.removeTextChangedListener(it) }
            holder.etOverride.setText(row.override)
            holder.cbSkip.setOnCheckedChangeListener(null)
            holder.cbSkip.isChecked = row.skip
            holder.etOverride.isEnabled = !row.skip
            val watcher = object : TextWatcher {
                override fun beforeTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
                override fun onTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
                override fun afterTextChanged(s: Editable?) {
                    row.override = s?.toString().orEmpty()
                }
            }
            holder.watcher = watcher
            holder.etOverride.addTextChangedListener(watcher)
            holder.cbSkip.setOnCheckedChangeListener { _, checked ->
                row.skip = checked
                holder.etOverride.isEnabled = !checked
            }
        }
    }
}
