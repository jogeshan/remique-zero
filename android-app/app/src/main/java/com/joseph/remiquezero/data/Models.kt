package com.joseph.remiquezero.data

// API response models (match app/api/dashboard.py + app/api/chat.py)

data class MeResponse(
    val displayName: String?,
    val username: String?,
    val role: String,
    val timezone: String,
)

data class CategoryTotal(val category: String, val total: Double)

data class PeriodSummary(
    val totalIncome: Double,
    val totalExpense: Double,
    val balance: Double,
    val expenseByCategory: List<CategoryTotal>,
    val transactionCount: Int,
)

data class DebtTotals(
    val owedByUser: Double,
    val owedToUser: Double,
    val openCount: Int,
)

data class SummaryResponse(
    val currency: String,
    val month: PeriodSummary,
    val yesterday: PeriodSummary,
    val debts: DebtTotals,
)

data class DebtItem(
    val id: String,
    val direction: String,
    val counterparty: String,
    val principal: Double,
    val remaining: Double,
    val status: String,
    val note: String?,
)

data class DebtsResponse(
    val open: List<DebtItem>,
    val settled: List<DebtItem>,
    val totals: DebtTotals,
)

data class ReminderItem(
    val id: String,
    val title: String,
    val description: String?,
    val recurrence: String?,
    val nextAt: String?,
)

data class TaskItem(
    val id: String,
    val title: String,
    val notes: String?,
    val status: String,
    val dueAt: String?,
)

data class TasksResponse(
    val open: List<TaskItem>,
    val recentlyCompleted: List<TaskItem>,
)

data class MemoryItem(
    val id: String,
    val content: String,
    val category: String,
)

data class ChatBubble(
    val role: String, // "user" or "assistant"
    val content: String,
)
