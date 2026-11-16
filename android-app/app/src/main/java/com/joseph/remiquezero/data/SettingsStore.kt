package com.joseph.remiquezero.data

import android.content.Context
import android.content.SharedPreferences

/**
 * Server URL + API token, kept in the app's private SharedPreferences
 * (sandboxed to this app). Personal-use build: good enough on your own
 * phone; the token can be revoked/rotated any time with /apitoken.
 */
class SettingsStore(context: Context) {
    private val prefs: SharedPreferences =
        context.getSharedPreferences("remique_settings", Context.MODE_PRIVATE)

    var serverUrl: String
        get() = prefs.getString(KEY_URL, DEFAULT_URL) ?: DEFAULT_URL
        set(value) = prefs.edit().putString(KEY_URL, value).apply()

    var token: String
        get() = prefs.getString(KEY_TOKEN, "") ?: ""
        set(value) = prefs.edit().putString(KEY_TOKEN, value).apply()

    val isConfigured: Boolean get() = token.isNotBlank()

    fun client(): ApiClient = ApiClient(serverUrl.trim(), token.trim())

    companion object {
        private const val KEY_URL = "server_url"
        private const val KEY_TOKEN = "api_token"
        const val DEFAULT_URL = "https://remique-zero.onrender.com"
    }
}
