package com.joseph.remiquezero.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import com.joseph.remiquezero.data.SettingsStore
import kotlinx.coroutines.launch

@Composable
fun SettingsScreen(settings: SettingsStore, onSaved: () -> Unit) {
    var serverUrl by remember { mutableStateOf(settings.serverUrl) }
    var token by remember { mutableStateOf(settings.token) }
    var result by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    Column(
        Modifier
            .fillMaxSize()
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("Server", style = MaterialTheme.typography.titleMedium)
        OutlinedTextField(
            value = serverUrl,
            onValueChange = { serverUrl = it },
            label = { Text("Server URL") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )

        Text("API token", style = MaterialTheme.typography.titleMedium)
        OutlinedTextField(
            value = token,
            onValueChange = { token = it },
            label = { Text("Token from /apitoken") },
            singleLine = true,
            visualTransformation = PasswordVisualTransformation(),
            modifier = Modifier.fillMaxWidth(),
        )
        Text(
            "In Telegram, send /apitoken to @RemiqueZeroBot and paste the token here.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        Button(
            onClick = {
                settings.serverUrl = serverUrl.trim()
                settings.token = token.trim()
                result = "Saved."
                onSaved()
            },
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text("Save")
        }
        OutlinedButton(
            onClick = {
                settings.serverUrl = serverUrl.trim()
                settings.token = token.trim()
                scope.launch {
                    result = try {
                        val me = settings.client().fetchMe()
                        "Connected as ${me.displayName ?: me.username ?: "you"} (${me.role})."
                    } catch (e: Exception) {
                        e.message ?: "Connection failed."
                    }
                }
            },
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text("Test connection")
        }
        result?.let { Text(it, style = MaterialTheme.typography.bodyMedium) }
    }
}
