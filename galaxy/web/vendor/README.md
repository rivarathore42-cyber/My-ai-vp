# Vendored offline speech library

Place the vosk-browser browser bundle here as `vosk.js` (it defines the global `Vosk`).
Nothing in this directory is fetched at runtime. Copy the file in during the build, from a checkout you have verified.
If the file is absent, the Voice Wake-Up switch is disabled and the page says why.
