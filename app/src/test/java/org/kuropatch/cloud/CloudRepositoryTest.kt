package org.kuropatch.cloud

import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import okio.Timeout
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import java.io.File
import java.security.MessageDigest
import java.util.ArrayDeque

/**
 * CloudRepository unit tests.
 *
 * The transport is a fake OkHttp [Call.Factory]: canned responses are
 * enqueued per test and requests are captured for assertions. No
 * sockets are used (the sandbox blocks JVM TCP, which also rules out
 * MockWebServer here) — every bit of repository logic (URL building,
 * headers, JSON parsing, error envelope, SHA-256 verification,
 * fail-closed auth) is still exercised.
 */
class CloudRepositoryTest {

    /** Scriptable fake transport: enqueue responses, capture requests. */
    private class FakeTransport : Call.Factory {
        data class Scripted(val code: Int, val body: String, val headers: Map<String, String>)

        val requests = mutableListOf<Request>()
        private val queue: ArrayDeque<Scripted> = ArrayDeque()

        fun enqueue(code: Int, body: String, headers: Map<String, String> = emptyMap()) {
            queue.add(Scripted(code, body, headers))
        }

        fun takeRequest(): Request = requests.removeAt(0)

        override fun newCall(request: Request): Call {
            requests.add(request)
            val s = queue.removeFirst()
            return object : Call {
                override fun request(): Request = request
                override fun execute(): Response {
                    val b = Response.Builder()
                        .request(request)
                        .protocol(Protocol.HTTP_1_1)
                        .code(s.code)
                        .message("")
                    s.headers.forEach { (k, v) -> b.header(k, v) }
                    b.body(s.body.toResponseBody("application/json".toMediaType()))
                    return b.build()
                }

                override fun enqueue(responseCallback: Callback) =
                    throw UnsupportedOperationException("sync only")

                override fun cancel() {}
                override fun isExecuted(): Boolean = false
                override fun isCanceled(): Boolean = false
                override fun clone(): Call = this
                override fun timeout(): Timeout = Timeout.NONE
            }
        }
    }

    private lateinit var transport: FakeTransport
    private lateinit var repo: CloudRepository
    private lateinit var tmp: File

    @Before
    fun setUp() {
        transport = FakeTransport()
        repo = CloudRepository(
            callFactory = transport,
            baseUrl = "https://kuro.test/api/v1",
            ownerToken = { "test-owner-token" },
            llmApiKey = { "test-llm-key" },
            llmModel = { "test-model" },
            llmBaseUrl = { "https://ai.eksi.biz.id/v1" },
        )
        tmp = kotlin.io.path.createTempDirectory("cloud-test").toFile()
    }

    @After
    fun tearDown() {
        tmp.deleteRecursively()
    }

    private fun sha256Hex(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(bytes)
            .joinToString("") { "%02x".format(it) }

    // ------------------------------------------------------------ by-hash

    @Test
    fun `byHash hit returns download url and sends bearer auth`() {
        transport.enqueue(
            200,
            """{"found":true,"job_id":null,"status":"COMPLETED",
               "download_url":"/api/v1/library/abc123/download?src_lang=en&target_lang=id&minimum=3&translate_all=false&cache_version=1"}"""
        )
        val r = repo.byHash("ABC123".repeat(16).take(64))
        assertTrue(r is CloudResult.Ok)
        val v = (r as CloudResult.Ok).value
        assertTrue(v.found)
        assertEquals("COMPLETED", v.status)
        assertNotNull(v.downloadUrl)
        val req = transport.takeRequest()
        assertEquals("Bearer test-owner-token", req.header("Authorization"))
        // sha is lowercased before sending
        assertTrue(req.url.toString().startsWith("https://kuro.test/api/v1/jobs/by-hash/abc123"))
        assertTrue(req.url.toString().contains("src_lang=en"))
        assertTrue(req.url.toString().contains("target_lang=id"))
    }

    @Test
    fun `byHash 404 found-false is Ok not error`() {
        transport.enqueue(404, """{"found":false}""")
        val r = repo.byHash("00".repeat(32))
        assertTrue(r is CloudResult.Ok)
        assertFalse((r as CloudResult.Ok).value.found)
    }

    // -------------------------------------------------------------- submit

    @Test
    fun `submit sends multipart with BYOK headers and parses 202`() {
        transport.enqueue(
            202,
            """{"job_id":"job123","status":"QUEUED",
               "jar_sha256":"${"ab".repeat(32)}","poll_url":"/api/v1/jobs/job123"}"""
        )
        val jar = File(tmp, "game.jar").apply { writeBytes(byteArrayOf(0x50, 0x4B, 0x03, 0x04)) }
        val r = repo.submit(jar, "device-1")
        assertTrue(r is CloudResult.Ok)
        assertEquals("job123", (r as CloudResult.Ok).value.jobId)
        val req = transport.takeRequest()
        assertEquals("Bearer test-owner-token", req.header("Authorization"))
        assertEquals("test-llm-key", req.header("X-LLM-API-Key"))
        assertEquals("test-model", req.header("X-LLM-Model"))
        assertEquals("https://ai.eksi.biz.id/v1", req.header("X-LLM-Base-URL"))
        // Multipart content type lives on the body, not request headers.
        assertTrue((req.body?.contentType()?.toString() ?: "").startsWith("multipart/form-data"))
        assertEquals("POST", req.method)
    }

    @Test
    fun `submit without llm key fails closed without network`() {
        val noKey = CloudRepository(
            callFactory = transport,
            baseUrl = "https://kuro.test/api/v1",
            ownerToken = { "t" },
            llmApiKey = { "" },
        )
        val r = noKey.submit(File(tmp, "x.jar").apply { writeBytes(byteArrayOf(1)) }, null)
        assertTrue(r is CloudResult.ApiError)
        assertEquals("NO_LLM_KEY", (r as CloudResult.ApiError).error.code)
        assertTrue(transport.requests.isEmpty())
    }

    // ---------------------------------------------------------------- poll

    @Test
    fun `poll parses progress including skipped strings`() {
        transport.enqueue(
            200,
            """{"job_id":"job123","status":"TRANSLATING",
               "progress":{"total_strings":100,"cached_strings":20,
               "translated_strings":30,"skipped_strings":10,"percent":60.0},
               "error_msg":null,"created_at":1,"expires_at":2,"download_ready":false}"""
        )
        val r = repo.poll("job123")
        assertTrue(r is CloudResult.Ok)
        val p = (r as CloudResult.Ok).value
        assertEquals("TRANSLATING", p.status)
        assertEquals(100, p.progress.total)
        assertEquals(10, p.progress.skipped)
        assertEquals(60, p.progress.done) // done = cached+translated+skipped
        assertEquals(60.0, p.progress.percent, 0.001)
    }

    @Test
    fun `poll failed surfaces error_msg`() {
        transport.enqueue(
            200,
            """{"job_id":"job123","status":"FAILED",
               "progress":{"total_strings":10,"cached_strings":0,
               "translated_strings":0,"skipped_strings":0,"percent":0.0},
               "error_msg":"boom","created_at":1,"expires_at":2,"download_ready":false}"""
        )
        val r = repo.poll("job123")
        assertTrue(r is CloudResult.Ok)
        val v = (r as CloudResult.Ok).value
        assertEquals("FAILED", v.status)
        assertEquals("boom", v.errorMsg)
    }

    // ------------------------------------------------------------ download

    @Test
    fun `downloadJob verifies X-Result-SHA256`() {
        val bytes = "fake-jar-bytes".toByteArray()
        val sha = sha256Hex(bytes)
        transport.enqueue(200, String(bytes), mapOf("X-Result-SHA256" to sha))
        val dest = File(tmp, "out.jar")
        val r = repo.downloadJob("job123", dest)
        assertTrue(r is CloudResult.Ok)
        val d = (r as CloudResult.Ok).value
        assertEquals(sha, d.sha256)
        assertArrayEquals(bytes, d.file.readBytes())
    }

    @Test
    fun `downloadJob sha mismatch deletes file and errors`() {
        val bytes = "fake-jar-bytes".toByteArray()
        transport.enqueue(200, String(bytes), mapOf("X-Result-SHA256" to "00".repeat(32)))
        val dest = File(tmp, "out.jar")
        val r = repo.downloadJob("job123", dest)
        assertTrue(r is CloudResult.ApiError)
        assertEquals("SHA_MISMATCH", (r as CloudResult.ApiError).error.code)
        assertFalse(dest.exists())
    }

    @Test
    fun `downloadJob missing sha header fails closed`() {
        transport.enqueue(200, "x")
        val r = repo.downloadJob("job123", File(tmp, "out.jar"))
        assertTrue(r is CloudResult.ApiError)
        assertEquals("NO_SHA_HEADER", (r as CloudResult.ApiError).error.code)
    }

    @Test
    fun `downloadFromPath joins relative by-hash url against base`() {
        val bytes = "lib-bytes".toByteArray()
        val sha = sha256Hex(bytes)
        transport.enqueue(200, String(bytes), mapOf("X-Result-SHA256" to sha))
        val r = repo.downloadFromPath(
            "/api/v1/library/abc/download?src_lang=en&target_lang=id&minimum=3&translate_all=false&cache_version=1",
            File(tmp, "lib.jar")
        )
        assertTrue(r is CloudResult.Ok)
        val req = transport.takeRequest()
        assertEquals(
            "https://kuro.test/api/v1/library/abc/download" +
                "?src_lang=en&target_lang=id&minimum=3&translate_all=false&cache_version=1",
            req.url.toString()
        )
    }

    // --------------------------------------------------------------- errors

    @Test
    fun `error envelope code message retryable preserved`() {
        transport.enqueue(
            429,
            """{"error":{"code":"QUOTA_EXCEEDED",
               "message":"Daily job cap reached","retryable":true}}"""
        )
        val r = repo.poll("job123")
        assertTrue(r is CloudResult.ApiError)
        val e = (r as CloudResult.ApiError).error
        assertEquals("QUOTA_EXCEEDED", e.code)
        assertEquals("Daily job cap reached", e.message)
        assertTrue(e.retryable)
        assertEquals(429, e.httpCode)
    }

    @Test
    fun `non-envelope error body synthesizes retryable for 5xx`() {
        transport.enqueue(502, "<html>bad gateway</html>")
        val r = repo.poll("job123")
        assertTrue(r is CloudResult.ApiError)
        val e = (r as CloudResult.ApiError).error
        assertEquals("HTTP_502", e.code)
        assertTrue(e.retryable)
    }

    @Test
    fun `missing owner token fails closed without network call`() {
        val noToken = CloudRepository(
            callFactory = transport,
            baseUrl = "https://kuro.test/api/v1",
            ownerToken = { "" },
        )
        val r = noToken.poll("job123")
        assertTrue(r is CloudResult.ApiError)
        assertEquals("NO_OWNER_TOKEN", (r as CloudResult.ApiError).error.code)
        assertTrue(transport.requests.isEmpty())
    }

    // ----------------------------------------------------------------- misc

    @Test
    fun `listLibrary parses entries`() {
        transport.enqueue(
            200,
            """{"entries":[{"jar_sha256":"${"cd".repeat(32)}",
               "src_lang":"en","target_lang":"id","minimum":3,
               "translate_all":false,"cache_version":1,"jar_name":"game.jar",
               "string_count":42,"size_bytes":12345,
               "completed_at":1700000000,"last_accessed_at":1700000001}]}"""
        )
        val r = repo.listLibrary()
        assertTrue(r is CloudResult.Ok)
        val entries = (r as CloudResult.Ok).value
        assertEquals(1, entries.size)
        assertEquals("game.jar", entries[0].jarName)
        assertEquals(42, entries[0].stringCount)
    }

    @Test
    fun `deleteJob returns true on 200`() {
        transport.enqueue(200, """{"deleted":true,"cancelled_active":true}""")
        val r = repo.deleteJob("job123")
        assertTrue(r is CloudResult.Ok)
        assertTrue((r as CloudResult.Ok).value)
        val req = transport.takeRequest()
        assertEquals("DELETE", req.method)
        assertEquals("Bearer test-owner-token", req.header("Authorization"))
    }

    @Test
    fun `downloadLibraryEntry builds url from entry params`() {
        val bytes = "e".toByteArray()
        val sha = sha256Hex(bytes)
        transport.enqueue(200, String(bytes), mapOf("X-Result-SHA256" to sha))
        val entry = LibraryEntryDto(
            jarSha256 = "AB".repeat(32), jarName = "g.jar",
            minimum = 3, translateAll = false, cacheVersion = 1,
        )
        val r = repo.downloadLibraryEntry(entry, File(tmp, "g.jar"))
        assertTrue(r is CloudResult.Ok)
        val req = transport.takeRequest()
        // sha lowercased in path
        assertTrue(req.url.toString().startsWith("https://kuro.test/api/v1/library/abab"))
        assertTrue(req.url.toString().contains("target_lang=id"))
    }

    @Test
    fun `sha256Hex streams correctly`() {
        val f = File(tmp, "a.bin").apply { writeBytes("abc".toByteArray()) }
        assertEquals(
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            CloudRepository.sha256Hex(f)
        )
    }
}
