#!/usr/bin/env python3
"""Optional build step: fetches the offline speech library and the two Vosk models for Voice Wake-Up.

It runs only when FETCH_VOICE=1 (the Dockerfile build argument). Nothing here runs in the browser or at chat time.
Check the sources before you enable it (see README): the vosk-browser bundle comes from the npm package via jsDelivr,
and the models come from alphacephei.com as zip files, which are repacked here as the .tar.gz the loader expects.
A failed download is reported and skipped; the page then shows Voice Wake-Up as unavailable.
"""
import io
import os
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "galaxy" / "web"
VOSK_JS_URL = "https://cdn.jsdelivr.net/npm/vosk-browser@0.0.8/dist/vosk.js"
MODELS = [
    ("vosk-model-small-en-in-0.4", "https://alphacephei.com/vosk/models/vosk-model-small-en-in-0.4.zip"),
    ("vosk-model-small-hi-0.22", "https://alphacephei.com/vosk/models/vosk-model-small-hi-0.22.zip"),
]


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "galaxy-build/7.0"})
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.read()


def main() -> int:
    if os.environ.get("FETCH_VOICE") != "1":
        print("fetch_voice_assets: skipped (build with --build-arg FETCH_VOICE=1 to include voice)", flush=True)
        return 0
    vendor = WEB / "vendor" / "vosk.js"
    try:
        vendor.parent.mkdir(parents=True, exist_ok=True)
        vendor.write_bytes(fetch(VOSK_JS_URL))
        print("fetch_voice_assets: wrote galaxy/web/vendor/vosk.js", flush=True)
    except Exception as exc:
        print(f"fetch_voice_assets: vosk.js not fetched ({type(exc).__name__}); voice stays unavailable", flush=True)
        return 0
    models_dir = WEB / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    for name, url in MODELS:
        try:
            archive = zipfile.ZipFile(io.BytesIO(fetch(url)))
            with tempfile.TemporaryDirectory() as tmp:
                archive.extractall(tmp)
                inner = Path(tmp) / name
                if not inner.is_dir():
                    inner = next(p for p in Path(tmp).iterdir() if p.is_dir())
                with tarfile.open(models_dir / f"{name}.tar.gz", "w:gz") as tar:
                    tar.add(inner, arcname=name)
            print(f"fetch_voice_assets: wrote models/{name}.tar.gz", flush=True)
        except Exception as exc:
            print(f"fetch_voice_assets: {name} not fetched ({type(exc).__name__})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
