package org.kuropatch.cloud

import kotlinx.serialization.json.Json
import okhttp3.Call
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import java.io.File
import java.io.IOException
import java.security.MessageDigest
import java.util.concurrent.TimeUnit

/**
 * Thin OkHttp client for kuro.eksi.biz.id (/api/v1).
 *
 * All methods are BLOCKING — call from a background thread (the app's
 * `thread {}` pattern), never the UI thread. Pure networking + JSON:
 * no Android framework calls, so it is unit-testable on the JVM
 * (see CloudRepositoryTest with MockWebServer).
 *
 * Contract (backend server/main.py, BLUEPRINT.md v2):
 * - Auth: `Authorization: Bearer <owner token>` on EVERY request
 *   (fail-closed server-side).
 * - Cloud v1 is EN->ID only: src_lang=en, target_lang=id.
 * - by-hash dedup key: sha256 + src_lang + target_lang + minimum +
 *   translate_all + cache_version.
 * - Error envelope: {error:{code, message, retryable}}. by-hash 404 is
 *   the exception: body is {"found": false}, NOT the envelope.
 * - Download integrity: X-Result-SHA256 must match the bytes received.
 *
 * The transport is an OkHttp [Call.Factory] (default: a real
 * OkHttpClient). Tests inject a fake — no sockets needed.
 */
class CloudRepository(
    private val callFactory: Call.Factory = defaultHttpClient(),
    private val baseUrl: String = DEFAULT_BASE_URL,
    private val ownerToken: () -> String = { "" },
    private val llmApiKey: () -> String = { "" },
    private val llmModel: () -> String = { DEFAULT_CLOUD_MODEL },
    private val llmBaseUrl: () -> String = { DEFAULT_LLM_BASE_URL },
) {
    companion object {
        const val DEFAULT_BASE_URL = "https://kuro.eksi.biz.id/api/v1"
        const val DEFAULT_LLM_BASE_URL = "https://ai.eksi.biz.id/v1"

        // Cloud v1 translation parameters (backend-enforced).
        const val SRC_LANG = "en"
        const val TARGET_LANG = "id"
        const val MINIMUM = 3
        const val TRANSLATE_ALL = false

        /**
         * Default model for the ai.eksi.biz.id gateway. The backend has
         * no server-side default (KURO_LLM_MODEL unset), so the client
         * must always send X-LLM-Model. User-editable in the cloud key
         * dialog; last verified live 2026-10-13.
         */
        const val DEFAULT_CLOUD_MODEL = "ddgw/gpt-5.6-luna"

        fun defaultHttpClient(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
            .writeTimeout(180, TimeUnit.SECONDS)
            .build()

        val json = Json { ignoreUnknownKeys = true; isLenient = true }

        /** Lowercase hex SHA-256 of a file, streamed (never fully in RAM). */
        fun sha256Hex(file: File): String {
            val md = MessageDigest.getInstance("SHA-256")
            file.inputStream().use { ins ->
                val buf = ByteArray(65536)
                var n: Int
                while (ins.read(buf).also { n = it } > 0) md.update(buf, 0, n)
            }
            return md.digest().joinToString("") { "%02x".format(it) }
        }
    }

    private fun authed(url: String): Request.Builder =
        Request.Builder()
            .url(url)
            .header("Authorization", "Bearer ${ownerToken()}")

    /** Fail-closed: never send an unauthenticated request. */
    private fun requireToken(): CloudResult.ApiError? =
        if (ownerToken().isBlank()) {
            CloudResult.ApiError(
                CloudApiError(
                    code = "NO_OWNER_TOKEN",
                    message = "Cloud mode is not configured on this build (owner token missing).",
                    retryable = false,
                    httpCode = 0,
                )
            )
        } else null

    private fun parseError(code: Int, body: String, message: String): CloudApiError {
        val parsed = runCatching { json.decodeFromString<ErrorBody>(body).error }
            .getOrNull()
        if (parsed != null && parsed.code.isNotEmpty()) {
            return CloudApiError(parsed.code, parsed.message, parsed.retryable, code)
        }
        // Not the envelope (proxy HTML, empty body, ...): synthesize.
        // 408/429/5xx are worth a retry; anything else is not.
        return CloudApiError(
            code = "HTTP_$code",
            message = message.ifEmpty { "Request failed (HTTP $code)" },
            retryable = code == 408 || code == 429 || code >= 500,
            httpCode = code,
        )
    }

    private fun errorResult(code: Int, resp: okhttp3.Response): CloudResult.ApiError {
        val body = try { resp.body?.string().orEmpty() } catch (e: Exception) { "" }
        return CloudResult.ApiError(parseError(code, body, resp.message))
    }

    // ------------------------------------------------------------ by-hash

    /**
     * Dedup check BEFORE uploading. 404 {"found": false} is a normal
     * "not found", not an error.
     */
    fun byHash(sha256: String): CloudResult<ByHashResponse> {
        requireToken()?.let { return it }
        val sha = sha256.lowercase()
        val url = "$baseUrl/jobs/by-hash/$sha" +
            "?src_lang=$SRC_LANG&target_lang=$TARGET_LANG" +
            "&minimum=$MINIMUM&translate_all=$TRANSLATE_ALL"
        val req = authed(url).get().build()
        return try {
            callFactory.newCall(req).execute().use { resp ->
                if (resp.code == 404) {
                    val body = try { resp.body?.string().orEmpty() } catch (e: Exception) { "" }
                    val parsed = runCatching {
                        json.decodeFromString<ByHashResponse>(body)
                    }.getOrNull()
                    return CloudResult.Ok(parsed ?: ByHashResponse(found = false))
                }
                if (!resp.isSuccessful) return errorResult(resp.code, resp)
                val parsed = json.decodeFromString<ByHashResponse>(
                    resp.body?.string().orEmpty()
                )
                CloudResult.Ok(parsed)
            }
        } catch (e: IOException) {
            CloudResult.NetworkError(e.message ?: "network error")
        }
    }

    // -------------------------------------------------------------- submit

    /** Multipart upload. 202 {job_id, status, jar_sha256, poll_url}. */
    fun submit(jarFile: File, deviceId: String?): CloudResult<JobSubmitResponse> {
        requireToken()?.let { return it }
        val key = llmApiKey()
        if (key.isBlank()) {
            return CloudResult.ApiError(
                CloudApiError(
                    code = "NO_LLM_KEY",
                    message = "Cloud LLM key is not set. Set it before translating via cloud.",
                    retryable = false,
                    httpCode = 0,
                )
            )
        }
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart(
                "file", jarFile.name,
                jarFile.asRequestBody("application/java-archive".toMediaType())
            )
            .addFormDataPart("src_lang", SRC_LANG)
            .addFormDataPart("target_lang", TARGET_LANG)
            .addFormDataPart("minimum", MINIMUM.toString())
            .addFormDataPart("translate_all", TRANSLATE_ALL.toString())
            .apply { if (!deviceId.isNullOrBlank()) addFormDataPart("device_id", deviceId) }
            .build()
        val req = authed("$baseUrl/jobs/patch")
            // BYOK: server holds no LLM key; creds are RAM-only server-side.
            .header("X-LLM-API-Key", key)
            .header("X-LLM-Model", llmModel())
            .header("X-LLM-Base-URL", llmBaseUrl())
            .post(body)
            .build()
        return try {
            callFactory.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) return errorResult(resp.code, resp)
                CloudResult.Ok(
                    json.decodeFromString<JobSubmitResponse>(resp.body?.string().orEmpty())
                )
            }
        } catch (e: IOException) {
            CloudResult.NetworkError(e.message ?: "network error")
        }
    }

    // ---------------------------------------------------------------- poll

    fun poll(jobId: String): CloudResult<JobPollResponse> {
        requireToken()?.let { return it }
        val req = authed("$baseUrl/jobs/$jobId").get().build()
        return try {
            callFactory.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) return errorResult(resp.code, resp)
                CloudResult.Ok(
                    json.decodeFromString<JobPollResponse>(resp.body?.string().orEmpty())
                )
            }
        } catch (e: IOException) {
            CloudResult.NetworkError(e.message ?: "network error")
        }
    }

    // ------------------------------------------------------------ download

    /**
     * Stream a download to [dest] and verify X-Result-SHA256 against the
     * received bytes. Fail-closed: missing header or mismatch deletes
     * the file and returns ApiError (never a half/trusted artifact).
     */
    private fun streamAndVerify(
        url: String,
        dest: File,
    ): CloudResult<DownloadResult> {
        requireToken()?.let { return it }
        val req = authed(url).get().build()
        return try {
            callFactory.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) return errorResult(resp.code, resp)
                val expected = resp.header("X-Result-SHA256").orEmpty()
                if (expected.isBlank()) {
                    return CloudResult.ApiError(
                        CloudApiError(
                            code = "NO_SHA_HEADER",
                            message = "Server did not send X-Result-SHA256; refusing unverified download.",
                            retryable = true,
                            httpCode = resp.code,
                        )
                    )
                }
                dest.parentFile?.mkdirs()
                resp.body?.byteStream()?.use { ins ->
                    dest.outputStream().use { out -> ins.copyTo(out) }
                } ?: return CloudResult.ApiError(
                    CloudApiError("EMPTY_BODY", "Empty download body.", true, resp.code)
                )
                val actual = sha256Hex(dest)
                if (!actual.equals(expected, ignoreCase = true)) {
                    dest.delete()
                    return CloudResult.ApiError(
                        CloudApiError(
                            code = "SHA_MISMATCH",
                            message = "Download integrity check failed (SHA-256 mismatch).",
                            retryable = true,
                            httpCode = resp.code,
                        )
                    )
                }
                CloudResult.Ok(DownloadResult(dest, actual))
            }
        } catch (e: IOException) {
            CloudResult.NetworkError(e.message ?: "network error")
        }
    }

    /** Download a COMPLETED job's result. */
    fun downloadJob(jobId: String, dest: File): CloudResult<DownloadResult> =
        streamAndVerify("$baseUrl/jobs/$jobId/download", dest)

    /** Download from a by-hash hit's download_url (relative or absolute). */
    fun downloadFromPath(url: String, dest: File): CloudResult<DownloadResult> {
        val full = when {
            url.startsWith("http://") || url.startsWith("https://") -> url
            else -> {
                // by-hash returns "/api/v1/library/<sha>/download?...":
                // the repository base already ends with /api/v1.
                val path = if (url.startsWith("/")) url else "/$url"
                "$baseUrl${path.removePrefix("/api/v1")}"
            }
        }
        return streamAndVerify(full, dest)
    }

    /** Download a library entry by its own parameters. */
    fun downloadLibraryEntry(
        entry: LibraryEntryDto,
        dest: File,
    ): CloudResult<DownloadResult> {
        val url = "$baseUrl/library/${entry.jarSha256.lowercase()}/download" +
            "?src_lang=${entry.srcLang}&target_lang=${entry.targetLang}" +
            "&minimum=${entry.minimum}&translate_all=${entry.translateAll}" +
            "&cache_version=${entry.cacheVersion}"
        return streamAndVerify(url, dest)
    }

    // --------------------------------------------------------------- misc

    /** Cancel/delete a job (also cancels the ACTIVE server task). */
    fun deleteJob(jobId: String): CloudResult<Boolean> {
        requireToken()?.let { return it }
        val req = authed("$baseUrl/jobs/$jobId").delete().build()
        return try {
            callFactory.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) return errorResult(resp.code, resp)
                CloudResult.Ok(true)
            }
        } catch (e: IOException) {
            CloudResult.NetworkError(e.message ?: "network error")
        }
    }

    /** List the server-side private library (newest last). */
    fun listLibrary(): CloudResult<List<LibraryEntryDto>> {
        requireToken()?.let { return it }
        val req = authed("$baseUrl/library").get().build()
        return try {
            callFactory.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) return errorResult(resp.code, resp)
                val parsed = json.decodeFromString<LibraryListResponse>(
                    resp.body?.string().orEmpty()
                )
                CloudResult.Ok(parsed.entries)
            }
        } catch (e: IOException) {
            CloudResult.NetworkError(e.message ?: "network error")
        }
    }
}
