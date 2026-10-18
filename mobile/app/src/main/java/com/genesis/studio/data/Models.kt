package com.genesis.studio.data

import org.json.JSONArray
import org.json.JSONObject

data class Build(
    val id: String,
    val createdAt: String,
    val prompt: String,
    val status: String,
    val stage: String,
    val progress: Int,
    val message: String,
    val appName: String?,
    val packageId: String?,
    val category: String?,
    val primaryColor: String?,
    val description: String?,
    val features: List<String>,
    val validationRules: List<String>,
    val attempts: List<Attempt>,
    val apk: Apk?,
    val error: String?,
) {
    val isDone: Boolean get() = status == "succeeded" || status == "failed"
    val succeeded: Boolean get() = status == "succeeded" && apk != null
    val displayName: String get() = appName ?: "Untitled app"
}

data class Attempt(
    val n: Int,
    val kind: String,
    val exitCode: Int?,
    val seconds: Double,
    val timedOut: Boolean,
    val errors: List<String>,
    val patchExplanation: String?,
) {
    val passed: Boolean get() = exitCode == 0 && !timedOut
}

data class Apk(val filename: String, val size: Long, val pkg: String?, val checks: List<String>)

data class Check(val name: String, val ok: Boolean, val detail: String, val fix: String)

data class Health(val ready: Boolean, val model: String?, val geminiError: String?, val checks: List<Check>)

private fun JSONObject.str(key: String): String? = if (!has(key) || isNull(key)) null else getString(key)

private fun JSONArray?.strings(): List<String> =
    if (this == null) emptyList() else (0 until length()).map { getString(it) }

private fun JSONArray?.objects(): List<JSONObject> =
    if (this == null) emptyList() else (0 until length()).map { getJSONObject(it) }

fun parseBuild(o: JSONObject): Build = Build(
    id = o.getString("id"),
    createdAt = o.str("created_at") ?: "",
    prompt = o.str("prompt") ?: "",
    status = o.str("status") ?: "queued",
    stage = o.str("stage") ?: "queued",
    progress = o.optInt("progress", 0),
    message = o.str("message") ?: "",
    appName = o.str("app_name"),
    packageId = o.str("package_id"),
    category = o.str("category"),
    primaryColor = o.str("primary_color"),
    description = o.str("description"),
    features = o.optJSONArray("features").strings(),
    validationRules = o.optJSONArray("validation_rules").strings(),
    attempts = o.optJSONArray("attempts").objects().map { a ->
        Attempt(
            n = a.optInt("n"),
            kind = a.str("kind") ?: "initial",
            exitCode = if (a.isNull("exit_code")) null else a.optInt("exit_code"),
            seconds = a.optDouble("seconds", 0.0),
            timedOut = a.optBoolean("timed_out"),
            errors = a.optJSONArray("errors").strings(),
            patchExplanation = a.optJSONObject("patch")?.str("explanation"),
        )
    },
    apk = o.optJSONObject("apk")?.let { a ->
        Apk(a.getString("filename"), a.optLong("size"), a.str("package"), a.optJSONArray("checks").strings())
    },
    error = o.str("error"),
)

fun parseHealth(o: JSONObject): Health = Health(
    ready = o.optBoolean("ready"),
    model = o.str("gemini_model"),
    geminiError = o.str("gemini_error"),
    checks = o.optJSONArray("checks").objects().map {
        Check(it.optString("name"), it.optBoolean("ok"), it.optString("detail"), it.optString("fix"))
    },
)

fun categoryLabel(category: String?): String = when (category) {
    "calculator" -> "Calculator"
    "habit_tracker" -> "Habit tracker"
    "flashcards" -> "Flashcards"
    "workout_tracker" -> "Workout"
    "todo_list" -> "To-do"
    else -> "App"
}
