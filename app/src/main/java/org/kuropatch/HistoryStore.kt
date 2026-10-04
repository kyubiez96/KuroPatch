package org.kuropatch

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

/**
 * One-tap re-patch history: the settings of every successful run, so a game
 * update can be re-patched with the same languages/provider without
 * re-entering everything.
 *
 * Stored as a capped JSON array in SharedPreferences. The staged input file
 * path is kept too — if the file is still there, re-patch runs immediately;
 * otherwise the user picks the (updated) archive and the saved settings are
 * applied to it.
 */
object HistoryStore {

    private const val PREFS = "kuropatch_history"
    private const val KEY_ENTRIES = "entries"
    private const val MAX_ENTRIES = 20

    data class Entry(
        val inputName: String,
        val inputPath: String,
        val source: String,
        val target: String,
        val provider: String,
        val apiKey: String?,
        val translateAll: Boolean,
        val timestamp: Long,
        val outputPath: String,
    ) {
        fun toJson(): JSONObject = JSONObject()
            .put("inputName", inputName)
            .put("inputPath", inputPath)
            .put("source", source)
            .put("target", target)
            .put("provider", provider)
            .put("apiKey", apiKey)
            .put("translateAll", translateAll)
            .put("timestamp", timestamp)
            .put("outputPath", outputPath)

        fun label(): String = "$inputName  ·  $source → $target"

        companion object {
            fun fromJson(o: JSONObject): Entry = Entry(
                inputName = o.optString("inputName"),
                inputPath = o.optString("inputPath"),
                source = o.optString("source", "en"),
                target = o.optString("target", "id"),
                provider = o.optString("provider", "google-web"),
                apiKey = o.optString("apiKey").ifBlank { null },
                translateAll = o.optBoolean("translateAll"),
                timestamp = o.optLong("timestamp"),
                outputPath = o.optString("outputPath"),
            )
        }
    }

    fun load(context: Context): List<Entry> {
        val raw = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_ENTRIES, null) ?: return emptyList()
        return try {
            val arr = JSONArray(raw)
            List(arr.length()) { i -> Entry.fromJson(arr.getJSONObject(i)) }
        } catch (_: Exception) {
            emptyList()
        }
    }

    fun save(context: Context, entry: Entry) {
        val updated = (listOf(entry) + load(context)).take(MAX_ENTRIES)
        val arr = JSONArray()
        updated.forEach { arr.put(it.toJson()) }
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_ENTRIES, arr.toString())
            .apply()
    }

    fun clear(context: Context) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .remove(KEY_ENTRIES)
            .apply()
    }
}
