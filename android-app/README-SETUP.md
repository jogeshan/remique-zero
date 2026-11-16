# Remique Zero — personal Android app (setup)

A native Android app (Kotlin + Jetpack Compose) that talks to your
existing Remique Zero backend. Chat uses the **same AI, tools, memory
and conversation history as Telegram** — set reminders, log expenses,
manage tasks by chatting. Reminders still *arrive* on Telegram (no push
in this personal build).

Android is the easy platform for personal apps: **no fees, no expiry,
no account needed.** Once installed, it works forever.

## What you need

- **Android Studio** (free) on your Mac or PC — download from
  developer.android.com/studio
- An Android phone with **USB debugging** on (or wireless debugging),
  or just build an APK file and install it directly

## Steps

1. Unzip this folder anywhere, then in Android Studio choose
   **File → Open** and select the `remique-zero-android` folder.
   Let the first Gradle sync finish (it downloads Gradle + libraries
   once — needs internet).
2. On your phone: **Settings → About phone** → tap **Build number**
   7 times to unlock Developer options → **Developer options** →
   turn on **USB debugging**. Connect the phone by cable and allow
   the debugging prompt. (Android 11+: *Wireless debugging* also works.)
3. In Android Studio pick your phone in the device dropdown and press
   **▶ Run**. The app installs and opens.
4. In the app: **More → Settings** → server URL is already
   `https://remique-zero.onrender.com` → in Telegram send **/apitoken**
   to @RemiqueZeroBot, paste the token → **Save** → **Test connection**.

### Alternative: build an APK to install by hand

Android Studio → **Build → Build APK(s)** → the file lands in
`app/build/outputs/apk/debug/app-debug.apk`. Copy it to the phone and
tap it to install (allow "install from unknown sources" for your file
manager once).

## Notes

- Package: `com.joseph.remiquezero` · min Android 8.0 (API 26)
- The token is stored in the app's private storage on your phone and
  can be revoked any time by issuing a new one with /apitoken.
