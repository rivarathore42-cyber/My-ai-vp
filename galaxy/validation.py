"""Input checks for every request that reaches the engine (Phase 12). Pure functions: no I/O, no framework.

Every check returns (value, error). An error is a plain sentence the page can show to the person; a value is always a
cleaned copy, never the raw input.
"""
from __future__ import annotations

import base64
import binascii
import math
import re
import unicodedata
from typing import Any, Optional, Tuple

MAX_MESSAGE_CHARS = 4000
MAX_CONVERSATION_CHARS = 64
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
UPLOAD_SIGNATURES = {
    "image/png": b"\x89PNG\r\n\x1a\n",
    "image/jpeg": b"\xff\xd8\xff",
    "image/gif": b"GIF8",
    "image/webp": b"RIFF",
    "application/pdf": b"%PDF",
}
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FILENAME_BAD = re.compile(r"[^A-Za-z0-9._-]")
_CONVERSATION_BAD = re.compile(r"[^A-Za-z0-9_-]")


def clean_text(value: Any, limit: int) -> str:
    text = unicodedata.normalize("NFC", str(value))
    return _CONTROL.sub("", text).strip()[:limit]


def check_message(value: Any) -> Tuple[str, Optional[str]]:
    if not isinstance(value, str):
        return "", "message must be a text string"
    text = clean_text(value, MAX_MESSAGE_CHARS + 1)
    if len(text) > MAX_MESSAGE_CHARS:
        return "", f"message is longer than {MAX_MESSAGE_CHARS} characters"
    return text, None


def check_conversation(value: Any) -> Tuple[str, Optional[str]]:
    if value is None:
        return "default", None
    if not isinstance(value, str):
        return "", "conversation_id must be text"
    text = _CONVERSATION_BAD.sub("", value)[:MAX_CONVERSATION_CHARS]
    return text or "default", None


def _real(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def check_location(value: Any) -> Tuple[Optional[dict], Optional[str]]:
    if value is None:
        return None, None
    if not isinstance(value, dict):
        return None, "location must be an object"
    lat, lon = _real(value.get("lat")), _real(value.get("lon"))
    if lat is None or lon is None:
        return None, "location needs numeric lat and lon"
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, "location is out of range"
    accuracy = value.get("accuracy")
    cleaned_accuracy = None
    if accuracy is not None:
        cleaned_accuracy = _real(accuracy)
        if cleaned_accuracy is None or not 0 <= cleaned_accuracy <= 100000:
            return None, "accuracy must be a number from 0 to 100000"
    return {"lat": round(lat, 6), "lon": round(lon, 6), "accuracy": cleaned_accuracy}, None


def check_chat_body(body: Any) -> Tuple[Optional[dict], Optional[str]]:
    if not isinstance(body, dict):
        return None, "request body must be a JSON object"
    message, error = check_message(body.get("message", ""))
    if error:
        return None, error
    conversation, error = check_conversation(body.get("conversation_id"))
    if error:
        return None, error
    location, error = check_location(body.get("location"))
    if error:
        return None, error
    source = body.get("source", "text")
    cleaned = {"message": message, "conversation_id": conversation,
               "source": source if source in ("text", "voice") else "text"}
    if location is not None:
        cleaned["location"] = location
    if "user_id" in body:
        cleaned["user_id"] = body["user_id"]   # identity binding refuses a named user; it is not silently dropped
    return cleaned, None


def safe_filename(value: Any) -> str:
    base = str(value or "file").replace("\\", "/").split("/")[-1]
    return _FILENAME_BAD.sub("_", base).strip("._")[:120] or "file"


def check_upload(filename: Any, mime: Any, data: Any) -> Tuple[Optional[dict], Optional[str]]:
    if not isinstance(mime, str) or mime.strip().lower() not in UPLOAD_SIGNATURES:
        return None, "this file type is not supported: use an image (PNG, JPEG, GIF, WEBP) or a PDF"
    mime = mime.strip().lower()
    if not isinstance(data, str) or not data.strip():
        return None, "the file is empty"
    payload = data.split(",", 1)[1] if data.startswith("data:") and "," in data else data
    compact = "".join(payload.split())
    if len(compact) > MAX_UPLOAD_BYTES * 4 // 3 + 8:
        return None, "the file is larger than 5 MB"
    try:
        raw = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        return None, "the file data is not valid base64"
    if not raw:
        return None, "the file is empty"
    if len(raw) > MAX_UPLOAD_BYTES:
        return None, "the file is larger than 5 MB"
    signature = UPLOAD_SIGNATURES[mime]
    if mime == "image/webp":
        matches = raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
    else:
        matches = raw.startswith(signature)
    if not matches:
        return None, "the file content does not match its type"
    return {"filename": safe_filename(filename), "mime": mime, "bytes": raw}, None
