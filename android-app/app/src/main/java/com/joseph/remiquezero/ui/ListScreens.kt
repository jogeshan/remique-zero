package com.joseph.remiquezero.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowForward
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.ListItem
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
import com.joseph.remiquezero.data.DebtItem
import com.joseph.remiquezero.data.MemoryItem
import com.joseph.remiquezero.data.ReminderItem
import com.joseph.remiquezero.data.SettingsStore
import com.joseph.remiquezero.data.TaskItem

@Composable
fun RefreshButton(onClick: () -> Unit) {
    IconButton(onClick = onClick) {
        Icon(Icons.Filled.Refresh, contentDescription = "Refresh")
    }
}

@Composable
fun RemindersScreen(settings: SettingsStore, refreshKey: Int) {
    var reminders by remember { mutableStateOf<List<ReminderItem>?>(null) }
    var error by remember { mutableStateOf<String?>(null) }

    if (!settings.isConfigured) {
        NotConfigured()
        return
    }

    LaunchedEffect(refreshKey) {
        try {
            reminders = settings.client().fetchReminders()
            error = null
        } catch (e: Exception) {
            error = e.message
        }
    }

    Column(Modifier.fillMaxSize()) {
        error?.let { ErrorCard(it) }
        val data = reminders
        when {
            data == null && error == null -> LoadingBox()
            data != null && data.isEmpty() -> EmptyState("No reminders.\nAsk Remique in chat to set one.")
            data != null -> LazyColumn {
                items(data) { reminder ->
                    ListItem(
                        headlineContent = { Text(reminder.title) },
                        supportingContent = {
                            val recurrence = reminder.recurrence?.takeIf { it.isNotBlank() }
                            Text(
                                listOfNotNull(formatDate(reminder.nextAt), recurrence)
                                    .joinToString(" · ")
                            )
                        },
                    )
                    HorizontalDivider()
                }
            }
        }
    }
}

@Composable
fun TasksScreen(settings: SettingsStore, refreshKey: Int) {
    var open by remember { mutableStateOf<List<TaskItem>?>(null) }
    var done by remember { mutableStateOf<List<TaskItem>>(emptyList()) }
    var error by remember { mutableStateOf<String?>(null) }

    if (!settings.isConfigured) {
        NotConfigured()
        return
    }

    LaunchedEffect(refreshKey) {
        try {
            val response = settings.client().fetchTasks()
            open = response.open
            done = response.recentlyCompleted
            error = null
        } catch (e: Exception) {
            error = e.message
        }
    }

    Column(Modifier.fillMaxSize()) {
        error?.let { ErrorCard(it) }
        val data = open
        when {
            data == null && error == null -> LoadingBox()
            data != null && data.isEmpty() && done.isEmpty() ->
                EmptyState("No tasks.\nAsk Remique in chat to add one.")
            data != null -> LazyColumn {
                items(data) { task ->
                    ListItem(
                        headlineContent = { Text(task.title) },
                        supportingContent = {
                            val parts = listOfNotNull(
                                task.dueAt?.let { "Due ${formatDate(it)}" },
                                task.notes?.takeIf { it.isNotBlank() },
                            )
                            if (parts.isNotEmpty()) Text(parts.joinToString("\n"))
                        },
                    )
                    HorizontalDivider()
                }
                if (done.isNotEmpty()) {
                    item {
                        Text(
                            "Recently completed",
                            style = MaterialTheme.typography.titleSmall,
                            modifier = Modifier.padding(16.dp),
                        )
                    }
                    items(done) { task ->
                        ListItem(
                            headlineContent = {
                                Text(task.title, color = MaterialTheme.colorScheme.onSurfaceVariant)
                            },
                        )
                        HorizontalDivider()
                    }
                }
            }
        }
    }
}

@Composable
fun DebtsScreen(settings: SettingsStore) {
    var open by remember { mutableStateOf<List<DebtItem>?>(null) }
    var settled by remember { mutableStateOf<List<DebtItem>>(emptyList()) }
    var error by remember { mutableStateOf<String?>(null) }

    if (!settings.isConfigured) {
        NotConfigured()
        return
    }

    LaunchedEffect(Unit) {
        try {
            val response = settings.client().fetchDebts()
            open = response.open
            settled = response.settled
        } catch (e: Exception) {
            error = e.message
        }
    }

    Column(Modifier.fillMaxSize()) {
        error?.let { ErrorCard(it) }
        val data = open
        when {
            data == null && error == null -> LoadingBox()
            data != null && data.isEmpty() -> EmptyState("No open debts or loans. 🎉")
            data != null -> LazyColumn {
                items(data) { debt ->
                    ListItem(
                        headlineContent = {
                            Text(
                                if (debt.direction == "borrowed") {
                                    "You owe · ${debt.counterparty}"
                                } else {
                                    "Owes you · ${debt.counterparty}"
                                }
                            )
                        },
                        supportingContent = {
                            Text("${formatMoney(debt.remaining)} left of ${formatMoney(debt.principal)}")
                        },
                    )
                    HorizontalDivider()
                }
                if (settled.isNotEmpty()) {
                    item {
                        Text(
                            "Settled",
                            style = MaterialTheme.typography.titleSmall,
                            modifier = Modifier.padding(16.dp),
                        )
                    }
                    items(settled) { debt ->
                        ListItem(
                            headlineContent = {
                                Text("${debt.counterparty} — ${formatMoney(debt.principal)}")
                            },
                        )
                        HorizontalDivider()
                    }
                }
            }
        }
    }
}

@Composable
fun MemoriesScreen(settings: SettingsStore) {
    var memories by remember { mutableStateOf<List<MemoryItem>?>(null) }
    var error by remember { mutableStateOf<String?>(null) }

    if (!settings.isConfigured) {
        NotConfigured()
        return
    }

    LaunchedEffect(Unit) {
        try {
            memories = settings.client().fetchMemories()
        } catch (e: Exception) {
            error = e.message
        }
    }

    Column(Modifier.fillMaxSize()) {
        error?.let { ErrorCard(it) }
        val data = memories
        when {
            data == null && error == null -> LoadingBox()
            data != null && data.isEmpty() -> EmptyState("No memories saved yet.")
            data != null -> LazyColumn {
                items(data) { memory ->
                    ListItem(
                        headlineContent = { Text(memory.content) },
                        supportingContent = { Text(memory.category) },
                    )
                    HorizontalDivider()
                }
            }
        }
    }
}

@Composable
fun MoreScreen(onOpen: (String) -> Unit) {
    LazyColumn(Modifier.fillMaxSize()) {
        listOf(
            Triple("debts", "Debts & Loans", "Who owes whom"),
            Triple("memories", "Memories", "What Remique remembers about you"),
            Triple("settings", "Settings", "Server URL and API token"),
        ).forEach { (route, title, subtitle) ->
            item {
                ListItem(
                    headlineContent = { Text(title) },
                    supportingContent = { Text(subtitle) },
                    trailingContent = {
                        Icon(Icons.AutoMirrored.Filled.ArrowForward, contentDescription = null)
                    },
                    modifier = Modifier.clickable { onOpen(route) },
                )
                HorizontalDivider()
            }
        }
    }
}

@Composable
fun EmptyState(message: String) {
    Column(
        Modifier
            .fillMaxSize()
            .padding(24.dp),
        verticalArrangement = Arrangement.Center,
    ) {
        Text(message, style = MaterialTheme.typography.bodyLarge)
    }
}
