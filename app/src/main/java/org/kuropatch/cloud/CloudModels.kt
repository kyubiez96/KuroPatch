package org.kuropatch.cloud

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import java.io.File

/**
 * Cloud mode DTOs for kuro.eksi.biz.id (/api/v1).
 *
 * Shapes mirror the backend Pydantic schemas (server/schemas.py):
 * progress {total_strings, cached_strings, translated_strings,
 * skipped_strings, percent}, error envelope
 * {error:{code, message, retryable}}.
 */

// ------------------------------------------------------------------ polls

@Serializable
data class CloudProgress(
    @SerialName("total_strings") val total: Int = 0,
    @SerialName("cached_strings") val cached: Int = 0,
    @SerialName("translated_strings") val translated: Int = 0,
    @SerialName("skipped_strings") val skipped: Int = 0,
    @SerialName("percent") val percent: Double = 0.0,
) {
    /** Server-side rule (BLUEPRINT audit H4): done = cached+translated+skipped. */
    val done: Int get() = cached + translated + skipped
}

@Serializable
data class JobPollResponse(
    @SerialName("job_id") val jobId: String,
    val status: String = "",
    val progress: CloudProgress = CloudProgress(),
    @SerialName("error_msg") val errorMsg: String? = null,
    @SerialName("download_ready") val downloadReady: Boolean = false,
)

@Serializable
data class ByHashResponse(
    val found: Boolean = false,
    @SerialName("job_id") val jobId: String? = null,
    val status: String? = null,
    @SerialName("poll_url") val pollUrl: String? = null,
    @SerialName("download_url") val downloadUrl: String? = null,
)

@Serializable
data class JobSubmitResponse(
    @SerialName("job_id") val jobId: String,
    val status: String = "",
    @SerialName("jar_sha256") val jarSha256: String = "",
    @SerialName("poll_url") val pollUrl: String = "",
)

// ---------------------------------------------------------------- library

@Serializable
data class LibraryEntryDto(
    @SerialName("jar_sha256") val jarSha256: String,
    @SerialName("src_lang") val srcLang: String = "en",
    @SerialName("target_lang") val targetLang: String = "id",
    @SerialName("minimum") val minimum: Int = 3,
    @SerialName("translate_all") val translateAll: Boolean = false,
    @SerialName("cache_version") val cacheVersion: Int = 1,
    @SerialName("jar_name") val jarName: String = "",
    @SerialName("string_count") val stringCount: Int = 0,
    @SerialName("size_bytes") val sizeBytes: Long = 0,
    @SerialName("completed_at") val completedAt: Long = 0,
)

@Serializable
data class LibraryListResponse(
    val entries: List<LibraryEntryDto> = emptyList(),
)

// ----------------------------------------------------------------- errors

@Serializable
data class ErrorDetail(
    val code: String = "",
    val message: String = "",
    val retryable: Boolean = false,
)

@Serializable
data class ErrorBody(val error: ErrorDetail? = null)

/** Normalized cloud error surfaced to the UI. */
data class CloudApiError(
    val code: String,
    val message: String,
    val retryable: Boolean,
    val httpCode: Int,
)

sealed interface CloudResult<out T> {
    data class Ok<T>(val value: T) : CloudResult<T>
    data class ApiError(val error: CloudApiError) : CloudResult<Nothing>
    data class NetworkError(val message: String) : CloudResult<Nothing>
}

/** A verified download: bytes on disk whose SHA-256 matched X-Result-SHA256. */
data class DownloadResult(val file: File, val sha256: String)
