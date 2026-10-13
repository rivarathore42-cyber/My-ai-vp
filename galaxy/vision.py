"""Agent 20 (Phase 4): zero-dependency image and PDF inspector with format guardrails.

Reads structure only (headers, chunks, segments, PDF object markers). It never decodes pixels or runs embedded code.
Supported: PNG, JPEG (with EXIF), GIF, WEBP, PDF.
Guardrails:
  - magic bytes must match the format; anything else is refused;
  - a size limit before any parsing (images 5 MB, PDF 20 MB);
  - dimensions above 100,000 px are refused; zero dimensions are refused;
  - every loop has a hard cap (segments, IFD entries, frames);
  - header damage (the fields that define the format) is refused;
  - damage after a valid header (a truncated later chunk or frame) is reported as truncated, not fatal;
  - EXIF text is reduced to printable ASCII and 64 characters;
  - the only exception that leaves this module is ValueError, whatever the input.
"""
from __future__ import annotations

import re
import struct
from typing import Any

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_DIMENSION = 100_000
MAX_SEGMENTS = 2000
MAX_IFD_ENTRIES = 200
MAX_FRAMES = 2000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
_TIFF_TAGS = {0x010F: "make", 0x0110: "model", 0x0112: "orientation", 0x0131: "software", 0x0132: "datetime"}


def _text(raw: bytes) -> str:
    return re.sub(rb"[^\x20-\x7e]", b"", raw).decode("ascii", "ignore").strip()[:64]


def _dims(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise ValueError("image has zero width or height")
    if width > MAX_DIMENSION or height > MAX_DIMENSION:
        raise ValueError(f"image dimensions above {MAX_DIMENSION:,} px are refused")


def _parse_tiff(buf: bytes) -> dict[str, Any]:
    if len(buf) < 8:
        return {}
    order = "<" if buf[:2] == b"II" else (">" if buf[:2] == b"MM" else "")
    if not order or struct.unpack(order + "H", buf[2:4])[0] != 42:
        return {}
    ifd = struct.unpack(order + "I", buf[4:8])[0]
    if ifd + 2 > len(buf):
        return {}
    out: dict[str, Any] = {}
    count = min(struct.unpack(order + "H", buf[ifd:ifd + 2])[0], MAX_IFD_ENTRIES)
    for index in range(count):
        at = ifd + 2 + 12 * index
        if at + 12 > len(buf):
            break
        tag, kind, n = struct.unpack(order + "HHI", buf[at:at + 8])
        value = buf[at + 8:at + 12]
        name = _TIFF_TAGS.get(tag)
        if name is None:
            continue
        if kind == 2:
            if n <= 4:
                raw = value[:n]
            else:
                start = struct.unpack(order + "I", value)[0]
                if start + n > len(buf):
                    continue
                raw = buf[start:start + n]
            out[name] = _text(raw.split(b"\x00", 1)[0])
        elif kind == 3 and n == 1:
            out[name] = struct.unpack(order + "H", value[:2])[0]
    return out


def _png(d: bytes) -> dict[str, Any]:
    if len(d) < 33 or d[12:16] != b"IHDR":
        raise ValueError("PNG without a valid IHDR header")
    width, height, depth, ctype = struct.unpack(">IIBB", d[16:26])
    _dims(width, height)
    if ctype not in (0, 2, 3, 4, 6):
        raise ValueError("PNG colour type is not valid")
    info: dict[str, Any] = {"format": "PNG", "mime": "image/png", "width": width, "height": height,
                            "bit_depth": depth, "color_space": {0: "grayscale", 2: "RGB", 3: "indexed",
                                                                4: "grayscale+alpha", 6: "RGBA"}[ctype],
                            "has_alpha": ctype in (4, 6), "interlaced": bool(d[28])}
    pos, seen = 8, 0
    while pos + 12 <= len(d) and seen < MAX_SEGMENTS:
        length = struct.unpack(">I", d[pos:pos + 4])[0]
        name = d[pos + 4:pos + 8]
        if pos + 12 + length > len(d):
            info["truncated"] = True
            break
        body = d[pos + 8:pos + 8 + length]
        if name == b"gAMA" and length == 4:
            info["gamma"] = round(struct.unpack(">I", body)[0] / 100000, 5)
        elif name == b"sRGB":
            info["srgb"] = True
        elif name == b"iCCP":
            info["icc_profile"] = _text(body.split(b"\x00", 1)[0])
        elif name == b"eXIf":
            info["exif"] = _parse_tiff(body)
        elif name == b"IEND":
            break
        pos += 12 + length
        seen += 1
    return info


def _jpeg(d: bytes) -> dict[str, Any]:
    info: dict[str, Any] = {"format": "JPEG", "mime": "image/jpeg"}
    frame = None
    i, seen = 2, 0
    while i + 4 <= len(d) and seen < MAX_SEGMENTS:
        if d[i] != 0xFF:
            i += 1
            continue
        marker = d[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        seen += 1
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            i += 2
            continue
        if marker == 0xD9:
            break
        length = struct.unpack(">H", d[i + 2:i + 4])[0]
        if length < 2 or i + 2 + length > len(d):
            if frame is not None:
                info["truncated"] = True
                break
            raise ValueError("JPEG segment runs past the end of the file")
        segment = d[i + 4:i + 2 + length]
        if marker in SOF_MARKERS and frame is None:
            if len(segment) < 6:
                raise ValueError("JPEG frame header is truncated")
            precision, height, width, comps = struct.unpack(">BHHB", segment[:6])
            _dims(width, height)
            frame = {"width": width, "height": height, "bit_depth": precision, "components": comps,
                     "color_space": {1: "grayscale", 3: "YCbCr", 4: "CMYK"}.get(comps, "unknown"),
                     "progressive": marker in (0xC2, 0xC6, 0xCA, 0xCE)}
        elif marker == 0xE1 and segment[:6] == b"Exif\x00\x00":
            info["exif"] = _parse_tiff(segment[6:])
        elif marker == 0xE0 and segment[:4] == b"JFIF":
            info["jfif"] = True
        i += 2 + length
    if frame is None:
        raise ValueError("JPEG has no frame header")
    info.update(frame)
    return info


def _gif(d: bytes) -> dict[str, Any]:
    if len(d) < 13:
        raise ValueError("GIF header is truncated")
    width, height, flags = struct.unpack("<HH", d[6:10])[0], struct.unpack("<HH", d[6:10])[1], d[10]
    _dims(width, height)
    info: dict[str, Any] = {"format": "GIF", "mime": "image/gif", "width": width, "height": height,
                            "global_color_table": bool(flags & 0x80), "color_depth": ((flags >> 4) & 7) + 1}
    pos = 13 + (3 * (2 ** ((flags & 7) + 1)) if flags & 0x80 else 0)
    frames, loops = 0, None
    try:
        while pos < len(d) and frames <= MAX_FRAMES:
            block = d[pos]
            if block == 0x3B:
                break
            if block == 0x21:
                label = d[pos + 1]
                pos += 2
                while True:
                    size = d[pos]
                    pos += 1
                    if size == 0:
                        break
                    if label == 0xFF and size == 11 and d[pos:pos + 11] == b"NETSCAPE2.0":
                        if d[pos + 11] == 3 and d[pos + 12] == 1:
                            loops = struct.unpack("<H", d[pos + 13:pos + 15])[0]
                    pos += size
            elif block == 0x2C:
                flags2 = d[pos + 9]
                pos += 10 + (3 * (2 ** ((flags2 & 7) + 1)) if flags2 & 0x80 else 0) + 1
                while True:
                    size = d[pos]
                    pos += 1
                    if size == 0:
                        break
                    pos += size
                frames += 1
            else:
                info["truncated"] = True
                break
    except IndexError:
        info["truncated"] = True
    info.update(frames=frames, animated=frames > 1, loop_count=loops)
    if frames == 0:
        info["warning"] = "no image frames found"
    return info


def _webp(d: bytes) -> dict[str, Any]:
    if len(d) < 30:
        raise ValueError("WEBP header is truncated")
    chunk = d[12:16]
    if chunk == b"VP8 ":
        if d[23:26] != b"\x9d\x01\x2a":
            raise ValueError("bad VP8 signature")
        width, height = struct.unpack("<HH", d[26:30])[0] & 0x3FFF, struct.unpack("<HH", d[26:30])[1] & 0x3FFF
        extra = {"compression": "lossy", "color_space": "YUV 4:2:0", "has_alpha": False, "animated": False}
    elif chunk == b"VP8L":
        if d[20] != 0x2F:
            raise ValueError("bad VP8L signature")
        b0, b1, b2, b3 = d[21], d[22], d[23], d[24]
        width, height = 1 + (((b1 & 0x3F) << 8) | b0), 1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
        extra = {"compression": "lossless", "color_space": "RGBA" if b3 & 0x10 else "RGB",
                 "has_alpha": bool(b3 & 0x10), "animated": False}
    elif chunk == b"VP8X":
        flags = d[20]
        width, height = 1 + int.from_bytes(d[24:27], "little"), 1 + int.from_bytes(d[27:30], "little")
        extra = {"compression": "extended", "color_space": "RGBA" if flags & 0x10 else "RGB",
                 "has_alpha": bool(flags & 0x10), "animated": bool(flags & 0x02)}
    else:
        raise ValueError("unknown WEBP chunk type")
    _dims(width, height)
    return {"format": "WEBP", "mime": "image/webp", "width": width, "height": height, **extra}


def _pdf(d: bytes) -> dict[str, Any]:
    head = re.search(rb"%PDF-(\d\.\d)", d[:1024])
    if head is None:
        raise ValueError("PDF header is missing")
    scan = d[:MAX_PDF_BYTES]
    return {"format": "PDF", "mime": "application/pdf", "pdf_version": head.group(1).decode("ascii"),
            "pages_estimate": len(re.findall(rb"/Type\s*/Page(?![s\w])", scan)),
            "encrypted": b"/Encrypt" in scan,
            "javascript": bool(re.search(rb"/(?:JavaScript|JS)\b", scan)),
            "launch_action": b"/Launch" in scan,
            "embedded_files": b"/EmbeddedFile" in scan,
            "has_eof_marker": b"%%EOF" in d[-1024:]}


def inspect(data: bytes) -> dict[str, Any]:
    try:
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise ValueError("file is empty")
        data = bytes(data)
        if data[:8] == PNG_SIGNATURE:
            info = _png(_limit(data, MAX_IMAGE_BYTES))
        elif data[:6] in (b"GIF87a", b"GIF89a"):
            info = _gif(_limit(data, MAX_IMAGE_BYTES))
        elif data[:3] == b"\xff\xd8\xff":
            info = _jpeg(_limit(data, MAX_IMAGE_BYTES))
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            info = _webp(_limit(data, MAX_IMAGE_BYTES))
        elif data.find(b"%PDF-", 0, 1024) >= 0:
            info = _pdf(_limit(data, MAX_PDF_BYTES))
        else:
            raise ValueError("unsupported format (PNG, JPEG, GIF, WEBP or PDF only)")
        info["payload_bytes"] = len(data)
        return info
    except ValueError:
        raise
    except (struct.error, IndexError, TypeError, OverflowError) as exc:
        raise ValueError(f"malformed file ({type(exc).__name__})") from None


def _limit(data: bytes, cap: int) -> bytes:
    if len(data) > cap:
        raise ValueError(f"file larger than {cap // (1024 * 1024)} MB")
    return data
