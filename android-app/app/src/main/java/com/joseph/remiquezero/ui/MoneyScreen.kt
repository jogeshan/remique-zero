package com.joseph.remiquezero.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.joseph.remiquezero.data.PeriodSummary
import com.joseph.remiquezero.data.SettingsStore
import com.joseph.remiquezero.data.SummaryResponse

@Composable
fun MoneyScreen(settings: SettingsStore) {
    var summary by remember { mutableStateOf<SummaryResponse?>(null) }
    var error by remember { mutableStateOf<String?>(null) }

    if (!settings.isConfigured) {
        NotConfigured()
        return
    }

    LaunchedEffect(Unit) {
        try {
            summary = settings.client().fetchSummary()
        } catch (e: Exception) {
            error = e.message
        }
    }

    Column(Modifier.fillMaxSize()) {
        error?.let { ErrorCard(it) }
        val data = summary
        if (data == null && error == null) {
            LoadingBox()
        } else if (data != null) {
            LazyColumn(
                Modifier.padding(12.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                item { PeriodCard("This month", data.month, data.currency) }
                item { PeriodCard("Yesterday", data.yesterday, data.currency) }
                if (data.debts.openCount > 0) {
                    item {
                        Card(Modifier.fillMaxWidth()) {
                            Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                                Text("Debts", style = MaterialTheme.typography.titleMedium)
                                MoneyRow("You owe", formatMoney(data.debts.owedByUser, data.currency))
                                MoneyRow("Owed to you", formatMoney(data.debts.owedToUser, data.currency))
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun PeriodCard(title: String, period: PeriodSummary, currency: String) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(title, style = MaterialTheme.typography.titleMedium)
            MoneyRow("Income", formatMoney(period.totalIncome, currency))
            MoneyRow("Expenses", formatMoney(period.totalExpense, currency))
            MoneyRow("Net", formatMoney(period.balance, currency))
            if (period.transactionCount == 0) {
                Text(
                    "No transactions in this period.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            period.expenseByCategory.forEach { item ->
                MoneyRow(item.category, formatMoney(item.total, currency), small = true)
            }
        }
    }
}

@Composable
private fun MoneyRow(label: String, value: String, small: Boolean = false) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
        Text(
            label,
            style = if (small) MaterialTheme.typography.bodySmall else MaterialTheme.typography.bodyMedium,
        )
        Text(
            value,
            style = if (small) MaterialTheme.typography.bodySmall else MaterialTheme.typography.bodyMedium,
        )
    }
}
