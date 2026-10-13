"""Agent 12 (Phase 3): zero-dependency tabular analytics in pure Python with an in-memory SQLite table.

Input: CSV (with a header row), a JSON list of records or an object of equal-length lists, or a markdown pipe table.
Loading: an in-memory SQLite table with GENERATED identifiers (c0, c1, ...). Header text is never placed in SQL, so a
header such as 'x; DROP TABLE' is only ever displayed, never executed.
Statistics: SQL aggregates (COUNT, AVG, MIN, MAX, SUM, COUNT DISTINCT) and the standard library's statistics module
(median, multimode, sample variance and standard deviation, Pearson correlation). No pandas, no code execution.
Limits: 2 MB of input, 50,000 rows, 50 columns, 500 characters per cell.
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
import sqlite3
import statistics
from collections import Counter
from typing import Any

from .numparse import parse_number

MAX_BYTES = 2_000_000
MAX_ROWS = 50_000
MAX_COLS = 50
MAX_CELL = 500
PREVIEW_ROWS = 25
ANALYTICS_VERSION = "1"
MISSING = {"", "na", "n/a", "nan", "null", "none", "-"}
_SEPARATOR = re.compile(r"\|?\s*:?-{3,}:?(\s*\|\s*:?-{3,}:?)*\s*\|?")


class TableError(ValueError):
    """The input is not a table this module can read, or it is over a limit. The message is user-facing."""


def _missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in MISSING)


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()[:MAX_CELL]


def _to_number(text: str) -> float | None:
    try:
        return parse_number(text)
    except ValueError:
        return None


def _unique_headers(raw: list) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for index, header in enumerate(raw):
        base = _clean(header) or f"column {index + 1}"
        name, k = base, 2
        while name in seen:
            name = f"{base} ({k})"
            k += 1
        seen[name] = 1
        out.append(name)
    return out


def parse_table(text: str) -> dict:
    if not isinstance(text, str) or not text.strip():
        raise TableError("empty table")
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise TableError(f"table is larger than {MAX_BYTES:,} bytes")
    stripped = text.lstrip("\ufeff").strip()
    notes: list[str] = []
    if stripped[0] in "[{":
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise TableError(f"invalid JSON: {str(exc)[:100]}") from None
        if isinstance(obj, dict) and obj and all(isinstance(v, list) for v in obj.values()):
            headers = list(obj.keys())
            length = max(len(v) for v in obj.values())
            raw_rows = [[obj[h][i] if i < len(obj[h]) else None for h in headers] for i in range(length)]
            kind = "json columns"
        elif isinstance(obj, list) and obj and all(isinstance(r, dict) for r in obj):
            headers = list(dict.fromkeys(k for r in obj[:500] for k in r))
            raw_rows = [[r.get(h) for h in headers] for r in obj]
            kind = "json records"
        else:
            raise TableError("JSON must be a list of records or an object of equal-length lists")
    elif stripped.splitlines()[0].lstrip().startswith("|"):
        lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
        cells = lambda ln: [c.strip() for c in ln.strip().strip("|").split("|")]  # noqa: E731
        headers = cells(lines[0])
        raw_rows = [cells(ln) for ln in lines[1:] if not _SEPARATOR.fullmatch(ln)]
        kind = "markdown table"
    elif "," in stripped.splitlines()[0]:
        records = [r for r in csv.reader(io.StringIO(stripped)) if any(c.strip() for c in r)]
        if len(records) < 2:
            raise TableError("CSV needs a header row and at least one data row")
        headers, raw_rows = records[0], records[1:]
        kind = "CSV"
    else:
        raise TableError("no table found: use CSV with a header row, JSON records, or a markdown table")
    if len(raw_rows) > MAX_ROWS:
        raise TableError(f"more than {MAX_ROWS:,} rows")
    headers = _unique_headers(headers)
    if len(headers) > MAX_COLS:
        raise TableError(f"more than {MAX_COLS} columns")
    rows, ragged = [], 0
    for raw in raw_rows:
        cells = [_clean(c) for c in raw]
        if len(cells) != len(headers):
            ragged += 1
            cells = (cells + [""] * len(headers))[:len(headers)]
        rows.append(cells)
    if ragged:
        notes.append(f"{ragged} row(s) had a different number of cells and were padded or cut")
    return {"kind": kind, "headers": headers, "rows": rows, "notes": notes}


def column_types(headers: list[str], rows: list[list[str]]) -> list[str]:
    types = []
    for j in range(len(headers)):
        values = [r[j] for r in rows if not _missing(r[j])]
        types.append("numeric" if values and all(_to_number(v) is not None for v in values) else "text")
    return types


def load(headers: list[str], rows: list[list[str]], types: list[str]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    names = [f"c{j}" for j in range(len(headers))]
    declaration = ", ".join(f"{name} {'REAL' if kind == 'numeric' else 'TEXT'}" for name, kind in zip(names, types))
    conn.execute(f"CREATE TABLE data ({declaration})")
    prepared = []
    for row in rows:
        prepared.append([None if _missing(v) else (_to_number(v) if kind == "numeric" else v)
                         for v, kind in zip(row, types)])
    placeholders = ", ".join("?" * len(names))
    conn.executemany(f"INSERT INTO data VALUES ({placeholders})", prepared)
    return conn


def summarise(headers: list[str], rows: list[list[str]]) -> dict:
    types = column_types(headers, rows)
    conn = load(headers, rows, types)
    n = len(rows)
    columns, correlations = [], []
    try:
        for j, (name, kind) in enumerate(zip(headers, types)):
            col = f"c{j}"
            present = conn.execute(f"SELECT COUNT({col}) FROM data").fetchone()[0]
            info: dict[str, Any] = {"name": name, "type": kind, "count": present, "missing": n - present}
            values = [r[0] for r in conn.execute(f"SELECT {col} FROM data WHERE {col} IS NOT NULL")]
            if kind == "numeric":
                avg, low, high, total = conn.execute(f"SELECT AVG({col}), MIN({col}), MAX({col}), SUM({col}) FROM data").fetchone()
                info.update(mean=avg, min=low, max=high, sum=total)
                if values:
                    info["median"] = statistics.median(values)
                    info["mode"] = statistics.multimode(values)[0]
                if len(values) >= 2:
                    info["variance"] = statistics.variance(values)
                    info["std_dev"] = statistics.stdev(values)
            else:
                info["distinct"] = conn.execute(f"SELECT COUNT(DISTINCT {col}) FROM data").fetchone()[0]
                if values:
                    top, top_count = Counter(values).most_common(1)[0]
                    info.update(top=top, top_count=top_count)
            columns.append(info)
        numeric = [j for j, kind in enumerate(types) if kind == "numeric"]
        for x in range(len(numeric)):
            for y in range(x + 1, len(numeric)):
                a, b = numeric[x], numeric[y]
                pairs = conn.execute(f"SELECT c{a}, c{b} FROM data WHERE c{a} IS NOT NULL AND c{b} IS NOT NULL").fetchall()
                r, reason = None, "too few pairs" if len(pairs) < 3 else ""
                if len(pairs) >= 3:
                    try:
                        r = statistics.correlation([p[0] for p in pairs], [p[1] for p in pairs])
                    except statistics.StatisticsError:
                        reason = "zero variance"
                correlations.append({"a": headers[a], "b": headers[b], "r": r, "pairs": len(pairs), "reason": reason})
    finally:
        conn.close()
    return {"rows": n, "columns": columns, "correlations": correlations}


def fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _md(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")[:120]


def preview_table(headers: list[str], rows: list[list[str]]) -> str:
    head = "| " + " | ".join(_md(h) for h in headers) + " |"
    rule = "|" + "---|" * len(headers)
    body = ["| " + " | ".join(_md(c) for c in row) + " |" for row in rows[:PREVIEW_ROWS]]
    return "\n".join([head, rule] + body)


def render_report(parsed: dict, summary: dict) -> str:
    headers, rows = parsed["headers"], parsed["rows"]
    parts = [f"Parsed {len(rows)} row(s) and {len(headers)} column(s) from {parsed['kind']} input."]
    if parsed["notes"]:
        parts.append("Notes: " + "; ".join(parsed["notes"]))
    parts.append("**Preview**\n\n" + preview_table(headers, rows))
    numeric = [c for c in summary["columns"] if c["type"] == "numeric"]
    text_cols = [c for c in summary["columns"] if c["type"] == "text"]
    if numeric:
        lines = ["| column | count | missing | mean | median | mode | min | max | sum | std dev |", "|---|---|---|---|---|---|---|---|---|---|"]
        for c in numeric:
            lines.append(f"| {_md(c['name'])} | {c['count']} | {c['missing']} | {fmt(c.get('mean'))} | {fmt(c.get('median'))} | "
                         f"{fmt(c.get('mode'))} | {fmt(c.get('min'))} | {fmt(c.get('max'))} | {fmt(c.get('sum'))} | {fmt(c.get('std_dev'))} |")
        parts.append("**Numeric columns**\n\n" + "\n".join(lines))
    if text_cols:
        lines = ["| column | count | missing | distinct | top value (count) |", "|---|---|---|---|---|"]
        for c in text_cols:
            top = f"{_md(c.get('top', '-'))} ({c.get('top_count', 0)})" if "top" in c else "-"
            lines.append(f"| {_md(c['name'])} | {c['count']} | {c['missing']} | {c['distinct']} | {top} |")
        parts.append("**Text columns**\n\n" + "\n".join(lines))
    if summary["correlations"]:
        lines = ["| column A | column B | Pearson r | pairs |", "|---|---|---|---|"]
        for c in summary["correlations"]:
            value = f"{c['r']:.4f}" if c["r"] is not None else f"n/a ({c['reason']})"
            lines.append(f"| {_md(c['a'])} | {_md(c['b'])} | {value} | {c['pairs']} |")
        parts.append("**Correlations**\n\n" + "\n".join(lines))
    return "\n\n".join(parts)
