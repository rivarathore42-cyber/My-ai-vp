"""Section AB: input checks on the server and on the page (Phase 12).

Every chat body, location and file upload is checked before it reaches the engine. The page's own logic (cleaning,
payloads, error wording) is tested in Node by tests/js/app_logic.test.js.
"""
import base64
import shutil
import subprocess
from pathlib import Path

from galaxy import validation as V

ROOT = Path(__file__).resolve().parents[1]
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20
PDF = b"%PDF-1.4\n%test"
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 8


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


async def run(check) -> None:
    check(V.check_message("  hello\x00\x07 world\n  ")[0] == "hello world",
          "AB-text: control characters are removed and the ends are trimmed")
    check(V.check_message("नमस्ते गैलेक्सी")[0] == "नमस्ते गैलेक्सी", "AB-text: Devanagari passes through unchanged")
    check(V.check_message("a\tb\nc")[0] == "a\tb\nc", "AB-text: tabs and newlines are kept")
    check(V.check_message("x" * 4001)[1] is not None, "AB-text: more than 4000 characters is refused")
    check(V.check_message("x" * 4000)[1] is None, "AB-text: exactly 4000 characters is accepted")
    check(V.check_message(123)[1] is not None and V.check_message(None)[1] is not None,
          "AB-text: a non-text message is refused")

    body, error = V.check_chat_body({"message": "hi", "conversation_id": "a b/c?<script>", "source": "shout"})
    check(error is None and body["conversation_id"] == "abcscript" and body["source"] == "text",
          "AB-chat: the conversation id keeps only safe characters; an unknown source becomes text")
    check(V.check_chat_body("not an object")[1] is not None, "AB-chat: a body that is not a JSON object is refused")
    body, error = V.check_chat_body({"message": "hi", "user_id": "someone-else"})
    check(error is None and body.get("user_id") == "someone-else",
          "AB-chat: a named user is passed on, so identity binding can refuse it visibly")

    body, error = V.check_chat_body({"message": "hi", "location": {"lat": 28.6, "lon": 77.2, "accuracy": 12}})
    check(error is None and body["location"] == {"lat": 28.6, "lon": 77.2, "accuracy": 12.0},
          "AB-location: a valid location is kept")
    check(V.check_chat_body({"message": "hi", "location": {"lat": 91, "lon": 0}})[1] is not None,
          "AB-location: latitude above 90 is refused")
    check(V.check_chat_body({"message": "hi", "location": {"lat": "28.6", "lon": 77}})[1] is not None,
          "AB-location: text coordinates are refused, not converted")
    check(V.check_chat_body({"message": "hi", "location": {"lat": True, "lon": 77}})[1] is not None,
          "AB-location: a boolean is not a coordinate")
    check(V.check_chat_body({"message": "hi", "location": {"lat": 28.6, "lon": 77, "accuracy": -4}})[1] is not None,
          "AB-location: a negative accuracy is refused")

    upload, error = V.check_upload("../../etc/passwd.png", "image/png", "data:image/png;base64," + _b64(PNG))
    check(error is None and upload["filename"] == "passwd.png" and upload["mime"] == "image/png",
          "AB-upload: the name keeps only its last part, and a data URL is accepted")
    check(V.check_upload("x.exe", "application/x-msdownload", _b64(PNG))[1] is not None,
          "AB-upload: an executable is refused by type")
    check(V.check_upload("a.png", "image/png", _b64(JPEG))[1] is not None,
          "AB-upload: a file whose content does not match its type is refused")
    check(V.check_upload("a.webp", "image/webp", _b64(WEBP))[1] is None, "AB-upload: a real WEBP is accepted")
    check(V.check_upload("a.pdf", "application/pdf", _b64(PDF))[1] is None, "AB-upload: a PDF is accepted")
    check(V.check_upload("a.png", "image/png", "!!!not base64!!!")[1] is not None,
          "AB-upload: text that is not base64 is refused")
    check(V.check_upload("a.png", "image/png", "")[1] is not None, "AB-upload: an empty file is refused")
    check(V.check_upload("a.png", "image/png", _b64(b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024)))[1] is not None,
          "AB-upload: a file over 5 MB is refused")

    api_source = (ROOT / "galaxy" / "api.py").read_text(encoding="utf-8")
    check("validation.check_chat_body(" in api_source and "validation.check_upload(" in api_source,
          "AB-api: the chat and upload routes validate before the engine sees anything")

    node = shutil.which("node")
    if node is None:
        check(False, "AB-page: node is needed to run the page logic tests")
    else:
        result = subprocess.run([node, str(ROOT / "tests" / "js" / "app_logic.test.js")],
                                capture_output=True, text=True, timeout=120)
        check(result.returncode == 0, "AB-page: the page's input checks, payloads and error wording pass in Node "
                                      + (result.stderr.strip()[:200] if result.returncode else ""))
