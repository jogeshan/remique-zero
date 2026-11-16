package com.joseph.remiquezero.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import com.joseph.remiquezero.data.ChatBubble
import com.joseph.remiquezero.data.SettingsStore
import kotlinx.coroutines.launch

@Composable
fun ChatScreen(settings: SettingsStore) {
    var bubbles by remember { mutableStateOf<List<ChatBubble>>(emptyList()) }
    var input by remember { mutableStateOf("") }
    var sending by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var loaded by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    val listState = rememberLazyListState()

    if (!settings.isConfigured) {
        NotConfigured()
        return
    }

    LaunchedEffect(Unit) {
        if (!loaded) {
            loaded = true
            try {
                bubbles = settings.client().fetchChatHistory()
            } catch (e: Exception) {
                error = e.message
            }
        }
    }

    LaunchedEffect(bubbles.size) {
        if (bubbles.isNotEmpty()) {
            listState.animateScrollToItem(bubbles.size - 1)
        }
    }

    Column(Modifier.fillMaxSize()) {
        error?.let { ErrorCard(it) }

        LazyColumn(
            state = listState,
            modifier = Modifier
                .weight(1f)
                .fillMaxWidth()
                .padding(horizontal = 12.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            items(bubbles) { bubble ->
                BubbleRow(bubble)
            }
            if (sending) {
                item {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        CircularProgressIndicator(Modifier.padding(4.dp))
                        Spacer(Modifier.width(8.dp))
                        Text("Remique is thinking…", style = MaterialTheme.typography.bodySmall)
                    }
                }
            }
        }

        Row(
            Modifier
                .fillMaxWidth()
                .padding(10.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            OutlinedTextField(
                value = input,
                onValueChange = { input = it },
                placeholder = { Text("Message Remique…") },
                modifier = Modifier.weight(1f),
                maxLines = 4,
            )
            IconButton(
                enabled = input.isNotBlank() && !sending,
                onClick = {
                    val text = input.trim()
                    input = ""
                    bubbles = bubbles + ChatBubble("user", text)
                    sending = true
                    error = null
                    scope.launch {
                        try {
                            val reply = settings.client().sendChat(text)
                            bubbles = bubbles + ChatBubble("assistant", reply)
                        } catch (e: Exception) {
                            error = e.message
                        }
                        sending = false
                    }
                },
            ) {
                Icon(Icons.AutoMirrored.Filled.Send, contentDescription = "Send")
            }
        }
    }
}

@Composable
private fun BubbleRow(bubble: ChatBubble) {
    val isUser = bubble.role == "user"
    Row(
        Modifier.fillMaxWidth(),
        horizontalArrangement = if (isUser) Arrangement.End else Arrangement.Start,
    ) {
        Box(
            Modifier
                .widthIn(max = 300.dp)
                .clip(RoundedCornerShape(14.dp))
                .background(
                    if (isUser) MaterialTheme.colorScheme.primary
                    else MaterialTheme.colorScheme.surfaceVariant
                )
                .padding(10.dp),
        ) {
            Text(
                bubble.content,
                color = if (isUser) Color.White else MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}
