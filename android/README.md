# Galaxy AI Android app (WebView wrapper)

The app opens the Galaxy AI page in a WebView. The page is the same one the browser uses; this wrapper only answers its
permission requests: the microphone, the camera and the location. Each one is first asked of Android, then granted to the
page only if the phone allows it.

## Build

1. Open the `android/` folder in Android Studio (it creates the Gradle wrapper on first sync).
2. Put your service address in `android/gradle.properties`, for example `galaxyUrl=https://galaxy-ai.onrender.com/`.
   The app shows a message instead of a blank page if this is empty.
3. Build a debug APK (Build > Build APK) and install it.

The Gradle versions in `build.gradle.kts` are a starting point; Android Studio may offer newer ones.

## What is not verified here

This wrapper was written without an Android SDK in the build environment, so it has not been compiled or run on a device.
Check these on a real phone before you rely on it: the microphone prompt for voice, the location prompt, the file picker,
and that the page loads over https.

## If you use a WebView wrapper service instead

A "web to app" service has to grant the page's microphone, camera and location requests inside its WebView, and request
the matching Android permissions. If it does not, the page's permission cards will show "denied" on the phone even when
the browser would allow it.
