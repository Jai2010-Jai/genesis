package com.genesis.studio.data

import android.net.Uri
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

class ApiException(message: String, val status: Int = 0) : Exception(message)

/** Talks to a Genesis server. Every request carries the access code as a Bearer token. */
class GenesisApi(val baseUrl: String, private val accessCode: String) {

    private fun open(path: String, method: String): HttpURLConnection {
        val conn = URL(baseUrl.trimEnd('/') + path).openConnection() as HttpURLConnection
        conn.requestMethod = method
        conn.connectTimeout = 8_000
        conn.readTimeout = 30_000
        conn.setRequestProperty("Authorization", "Bearer $accessCode")
        conn.setRequestProperty("Accept", "application/json")
        return conn
    }

    private suspend fun request(path: String, method: String = "GET", body: String? = null): String =
        withContext(Dispatchers.IO) {
            val conn = try {
                open(path, method)
            } catch (e: IOException) {
                throw ApiException("That address doesn't look right")
            }
            try {
                if (body != null) {
                    conn.doOutput = true
                    conn.setRequestProperty("Content-Type", "application/json")
                    conn.outputStream.use { it.write(body.toByteArray()) }
                }
                val code = conn.responseCode
                val stream = if (code in 200..299) conn.inputStream else conn.errorStream
                val text = stream?.bufferedReader()?.use { it.readText() } ?: ""
                if (code !in 200..299) throw ApiException(errorMessage(code, text), code)
                text
            } catch (e: ApiException) {
                throw e
            } catch (e: IOException) {
                throw ApiException("Can't reach your computer. Make sure Genesis is open on it and your phone is on the same Wi-Fi.")
            } finally {
                conn.disconnect()
            }
        }

    private fun errorMessage(code: Int, text: String): String {
        val detail = runCatching { JSONObject(text).optString("detail") }.getOrNull().orEmpty()
        return when {
            code == 401 -> "That code doesn't match. Check the code shown on your computer."
            code == 429 -> "Too many tries right now. Please wait a few minutes."
            detail.isNotBlank() -> detail
            else -> "Something went wrong on your computer ($code)"
        }
    }

    suspend fun health(): Health = parseHealth(JSONObject(request("/api/health")))

    suspend fun builds(): List<Build> {
        val arr = JSONArray(request("/api/builds"))
        return (0 until arr.length()).map { parseBuild(arr.getJSONObject(it)) }
    }

    suspend fun build(id: String): Build = parseBuild(JSONObject(request("/api/builds/$id")))

    suspend fun log(id: String): String = request("/api/builds/$id/log")

    suspend fun create(prompt: String, appName: String?): Build {
        val body = JSONObject().put("prompt", prompt).put("app_name", appName ?: JSONObject.NULL)
        return parseBuild(JSONObject(request("/api/builds", "POST", body.toString())))
    }

    /** Streams the verified APK to [dest], reporting progress 0..1. */
    suspend fun downloadApk(id: String, dest: File, onProgress: (Float) -> Unit) = withContext(Dispatchers.IO) {
        val conn = open("/api/builds/$id/apk", "GET")
        conn.readTimeout = 120_000
        try {
            val code = conn.responseCode
            if (code !in 200..299) {
                val text = conn.errorStream?.bufferedReader()?.use { it.readText() } ?: ""
                throw ApiException(errorMessage(code, text), code)
            }
            val total = conn.contentLengthLong.takeIf { it > 0 } ?: -1L
            dest.parentFile?.mkdirs()
            val tmp = File(dest.parentFile, dest.name + ".part")
            conn.inputStream.use { input ->
                tmp.outputStream().use { out ->
                    val buf = ByteArray(64 * 1024)
                    var read = 0L
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        out.write(buf, 0, n)
                        read += n
                        if (total > 0) onProgress(read.toFloat() / total)
                    }
                }
            }
            if (!tmp.renameTo(dest)) throw ApiException("Couldn't save the app on your phone")
        } catch (e: IOException) {
            throw ApiException("The download stopped. Check your Wi-Fi and try again.")
        } finally {
            conn.disconnect()
        }
    }

    companion object {
        const val DEFAULT_PORT = 8765

        /** Accepts "192.168.1.5", "192.168.1.5:8765" or a full URL. */
        fun normalizeServer(input: String): String? {
            var s = input.trim().trimEnd('/')
            if (s.isEmpty()) return null
            if (!s.startsWith("http://") && !s.startsWith("https://")) s = "http://$s"
            val uri = Uri.parse(s)
            val host = uri.host ?: return null
            if (host.isBlank() || host.contains(' ')) return null
            val port = if (uri.port > 0) uri.port else if (uri.scheme == "https") -1 else DEFAULT_PORT
            return "${uri.scheme}://$host" + (if (port > 0) ":$port" else "")
        }

        /** Parses the dashboard QR link: genesis://pair?u=<server>&c=<code>. */
        fun parsePairingLink(link: String): Pair<String, String>? {
            val uri = runCatching { Uri.parse(link.trim()) }.getOrNull() ?: return null
            if (uri.scheme != "genesis" || uri.host != "pair") return null
            val server = uri.getQueryParameter("u")?.let(::normalizeServer) ?: return null
            val code = uri.getQueryParameter("c")?.takeIf { it.length >= 12 } ?: return null
            return server to formatCode(code)
        }

        fun formatCode(raw: String): String =
            raw.uppercase().filter { it.isLetterOrDigit() }.take(16).chunked(4).joinToString("-")
    }
}
