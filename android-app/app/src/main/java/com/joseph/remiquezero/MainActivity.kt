package com.joseph.remiquezero

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Alarm
import androidx.compose.material.icons.filled.Checklist
import androidx.compose.material.icons.filled.Chat
import androidx.compose.material.icons.filled.MoreHoriz
import androidx.compose.material.icons.filled.PieChart
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.navigation.NavHostController
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.joseph.remiquezero.data.SettingsStore
import com.joseph.remiquezero.ui.ChatScreen
import com.joseph.remiquezero.ui.DebtsScreen
import com.joseph.remiquezero.ui.MemoriesScreen
import com.joseph.remiquezero.ui.MoneyScreen
import com.joseph.remiquezero.ui.MoreScreen
import com.joseph.remiquezero.ui.RefreshButton
import com.joseph.remiquezero.ui.RemindersScreen
import com.joseph.remiquezero.ui.SettingsScreen
import com.joseph.remiquezero.ui.TasksScreen

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val settings = SettingsStore(applicationContext)
        setContent {
            MaterialTheme {
                AppRoot(settings)
            }
        }
    }
}

private data class Tab(val route: String, val label: String, val icon: ImageVector)

private val tabs = listOf(
    Tab("chat", "Chat", Icons.Filled.Chat),
    Tab("money", "Money", Icons.Filled.PieChart),
    Tab("reminders", "Reminders", Icons.Filled.Alarm),
    Tab("tasks", "Tasks", Icons.Filled.Checklist),
    Tab("more", "More", Icons.Filled.MoreHoriz),
)

private val titles = mapOf(
    "chat" to "Remique Zero",
    "money" to "Money",
    "reminders" to "Reminders",
    "tasks" to "Tasks",
    "more" to "More",
    "debts" to "Debts & Loans",
    "memories" to "Memories",
    "settings" to "Settings",
)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun AppRoot(settings: SettingsStore) {
    val navController: NavHostController = rememberNavController()
    val backStack by navController.currentBackStackEntryAsState()
    val currentRoute = backStack?.destination?.route ?: "chat"
    var refreshKey by remember { mutableIntStateOf(0) }
    var settingsVersion by remember { mutableIntStateOf(0) }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(titles[currentRoute] ?: "Remique Zero") },
                actions = {
                    if (currentRoute == "reminders" || currentRoute == "tasks") {
                        RefreshButton { refreshKey++ }
                    }
                },
            )
        },
        bottomBar = {
            NavigationBar {
                tabs.forEach { tab ->
                    NavigationBarItem(
                        selected = currentRoute == tab.route,
                        onClick = {
                            navController.navigate(tab.route) {
                                popUpTo("chat") { saveState = true }
                                launchSingleTop = true
                                restoreState = true
                            }
                        },
                        icon = { Icon(tab.icon, contentDescription = tab.label) },
                        label = { Text(tab.label) },
                    )
                }
            }
        },
    ) { padding ->
        NavHost(
            navController = navController,
            startDestination = "chat",
            modifier = Modifier.padding(padding),
        ) {
            composable("chat") {
                // settingsVersion in the key reloads chat after a token change.
                androidx.compose.runtime.key(settingsVersion) {
                    ChatScreen(settings)
                }
            }
            composable("money") {
                androidx.compose.runtime.key(settingsVersion) {
                    MoneyScreen(settings)
                }
            }
            composable("reminders") { RemindersScreen(settings, refreshKey) }
            composable("tasks") { TasksScreen(settings, refreshKey) }
            composable("more") { MoreScreen(onOpen = { navController.navigate(it) }) }
            composable("debts") { DebtsScreen(settings) }
            composable("memories") { MemoriesScreen(settings) }
            composable("settings") {
                SettingsScreen(settings, onSaved = { settingsVersion++ })
            }
        }
    }
}
