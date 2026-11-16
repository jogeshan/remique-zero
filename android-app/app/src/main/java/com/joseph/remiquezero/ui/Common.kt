package com.joseph.remiquezero.ui

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import java.text.NumberFormat
import java.time.OffsetDateTime
import java.time.format.DateTimeFormatter

@Composable
fun LoadingBox() {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        CircularProgressIndicator()
    }
}

@Composable
fun ErrorCard(message: String) {
    Card(
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.errorContainer,
        ),
        modifier = Modifier
            .fillMaxWidth()
            .padding(12.dp),
    ) {
        Text(
            message,
            color = MaterialTheme.colorScheme.onErrorContainer,
            style = MaterialTheme.typography.bodySmall,
            modifier = Modifier.padding(12.dp),
        )
    }
}

@Composable
fun NotConfigured() {
    Box(Modifier.fillMaxSize().padding(24.dp), contentAlignment = Alignment.Center) {
        Text(
            "🔑 Token needed\n\nOpen More → Settings and paste the token from Telegram's /apitoken command.",
            style = MaterialTheme.typography.bodyLarge,
        )
    }
}

fun formatMoney(value: Double, currency: String = "BDT"): String {
    val formatter = NumberFormat.getNumberInstance().apply {
        maximumFractionDigits = 2
    }
    return "${formatter.format(value)} $currency"
}

private val outputFormat = DateTimeFormatter.ofPattern("d MMM yyyy, h:mm a")

fun formatDate(iso: String?): String {
    if (iso.isNullOrBlank()) return ""
    return try {
        OffsetDateTime.parse(iso).format(outputFormat)
    } catch (e: Exception) {
        iso
    }
}
