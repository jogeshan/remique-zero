package com.joseph.remiquezero.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

class ApiException(message: String) : Exception(message)

/** Thin client for the Remique Zero /api/v1 endpoints (org.json parsing, no extra deps). */
class ApiClient(private val baseUrl: String, private val token: String) {

    private suspend fun request(path: String, method: String = "GET", body: JSONObject? = null): JSONObject =
        withContext(Dispatchers.IO) {
            if (token.isBlank()) {
                throw ApiException("Add your API token in Settings first. Get one in Telegram with /apitoken.")
            }
            val connection = URL(baseUrl + path).openConnection() as HttpURLConnection
            try {
                connection.requestMethod = method
                connection.setRequestProperty("Authorization", "Bearer $token")
                connection.connectTimeout = 15_000
                connection.readTimeout = 120_000 // AI replies can take a while
                if (body != null) {
                    connection.doOutput = true
                    connection.setRequestProperty("Content-Type", "application/json")
                    connection.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
                }
                val status = connection.responseCode
                val stream = if (status in 200..299) connection.inputStream else connection.errorStream
                val text = stream?.bufferedReader()?.use { it.readText() } ?: ""
                if (status !in 200..299) {
                    val detail = try {
                        JSONObject(text).optString("detail").ifBlank { null }
                    } catch (e: Exception) {
                        null
                    }
                    throw ApiException(detail ?: "Server error ($status).")
                }
                JSONObject(text)
            } finally {
                connection.disconnect()
            }
        }

    suspend fun fetchMe(): MeResponse {
        val json = request("/api/v1/me")
        return MeResponse(
            displayName = json.optStringOrNull("display_name"),
            username = json.optStringOrNull("username"),
            role = json.optString("role"),
            timezone = json.optString("timezone"),
        )
    }

    suspend fun fetchSummary(): SummaryResponse {
        val json = request("/api/v1/summary")
        return SummaryResponse(
            currency = json.optString("currency", "BDT"),
            month = parsePeriod(json.getJSONObject("month")),
            yesterday = parsePeriod(json.getJSONObject("yesterday")),
            debts = parseDebtTotals(json.getJSONObject("debts")),
        )
    }

    suspend fun fetchDebts(): DebtsResponse {
        val json = request("/api/v1/debts")
        return DebtsResponse(
            open = parseDebts(json.getJSONArray("open")),
            settled = parseDebts(json.getJSONArray("settled")),
            totals = parseDebtTotals(json.getJSONObject("totals")),
        )
    }

    suspend fun fetchReminders(): List<ReminderItem> {
        val json = request("/api/v1/reminders")
        val array = json.getJSONArray("reminders")
        return List(array.length()) { i ->
            val item = array.getJSONObject(i)
            ReminderItem(
                id = item.getString("id"),
                title = item.optString("title"),
                description = item.optStringOrNull("description"),
                recurrence = item.optStringOrNull("recurrence"),
                nextAt = item.optStringOrNull("next_at"),
            )
        }
    }

    suspend fun fetchTasks(): TasksResponse {
        val json = request("/api/v1/tasks")
        return TasksResponse(
            open = parseTasks(json.getJSONArray("open")),
            recentlyCompleted = parseTasks(json.getJSONArray("recently_completed")),
        )
    }

    suspend fun fetchMemories(): List<MemoryItem> {
        val json = request("/api/v1/memories")
        val array = json.getJSONArray("memories")
        return List(array.length()) { i ->
            val item = array.getJSONObject(i)
            MemoryItem(
                id = item.getString("id"),
                content = item.optString("content"),
                category = item.optString("category"),
            )
        }
    }

    suspend fun fetchChatHistory(): List<ChatBubble> {
        val json = request("/api/v1/chat/history?limit=50")
        val array = json.getJSONArray("messages")
        return List(array.length()) { i ->
            val item = array.getJSONObject(i)
            ChatBubble(
                role = item.optString("role"),
                content = item.optString("content"),
            )
        }
    }

    suspend fun sendChat(message: String): String {
        val body = JSONObject().put("message", message)
        val json = request("/api/v1/chat", method = "POST", body = body)
        return json.getString("reply")
    }

    private fun parsePeriod(json: JSONObject): PeriodSummary = PeriodSummary(
        totalIncome = json.optDouble("total_income", 0.0),
        totalExpense = json.optDouble("total_expense", 0.0),
        balance = json.optDouble("balance", 0.0),
        expenseByCategory = parseCategories(json.optJSONArray("expense_by_category")),
        transactionCount = json.optInt("transaction_count", 0),
    )

    private fun parseCategories(array: JSONArray?): List<CategoryTotal> {
        if (array == null) return emptyList()
        return List(array.length()) { i ->
            val item = array.getJSONObject(i)
            CategoryTotal(
                category = item.optString("category"),
                total = item.optDouble("total", 0.0),
            )
        }
    }

    private fun parseDebtTotals(json: JSONObject): DebtTotals = DebtTotals(
        owedByUser = json.optDouble("owed_by_user", 0.0),
        owedToUser = json.optDouble("owed_to_user", 0.0),
        openCount = json.optInt("open_count", 0),
    )

    private fun parseDebts(array: JSONArray): List<DebtItem> = List(array.length()) { i ->
        val item = array.getJSONObject(i)
        DebtItem(
            id = item.getString("id"),
            direction = item.optString("direction"),
            counterparty = item.optString("counterparty"),
            principal = item.optDouble("principal", 0.0),
            remaining = item.optDouble("remaining", 0.0),
            status = item.optString("status"),
            note = item.optStringOrNull("note"),
        )
    }

    private fun parseTasks(array: JSONArray): List<TaskItem> = List(array.length()) { i ->
        val item = array.getJSONObject(i)
        TaskItem(
            id = item.getString("id"),
            title = item.optString("title"),
            notes = item.optStringOrNull("notes"),
            status = item.optString("status"),
            dueAt = item.optStringOrNull("due_at"),
        )
    }
}

private fun JSONObject.optStringOrNull(name: String): String? {
    if (isNull(name)) return null
    return optString(name).ifBlank { null }
}
