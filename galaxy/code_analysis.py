"""Agent 7 (Phase 2): static analysis and lint for Python, JavaScript, HTML, CSS, C++ and Rust.

Nothing here executes user code. Python is parsed with ast. JavaScript is checked with `node --check`, C++ with
`g++ -fsyntax-only`, Rust with `rustc --emit=metadata`, each only when that tool is installed. Without the tool, or for
HTML and CSS, the analysis is lexical (brackets, strings, comments, and pattern rules), and the report says so.
"""
from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser
from typing import Any

MAX_CODE_CHARS = 20_000
ALIASES = {"python": "python", "python3": "python", "py3": "python", "py": "python", "javascript": "javascript", "js": "javascript", "html": "html",
           "css": "css", "cpp": "cpp", "c++": "cpp", "cxx": "cpp", "rust": "rust", "rs": "rust"}
_SECURITY = {
    "javascript": [(r"\beval\s*\(", "high", "eval() runs arbitrary code"),
                   (r"\bnew\s+Function\s*\(", "high", "new Function() is eval in disguise"),
                   (r"\.innerHTML\s*=", "medium", "innerHTML with unsanitised input is an XSS vector"),
                   (r"document\.write\s*\(", "medium", "document.write can rewrite the whole page"),
                   (r"child_process", "medium", "child_process gives shell access")],
    "cpp": [(r"\bgets\s*\(", "high", "gets() has no bounds check"),
            (r"\bstrcpy\s*\(", "medium", "strcpy() has no bounds check"),
            (r"\bsprintf\s*\(", "medium", "sprintf() has no bounds check"),
            (r"\bsystem\s*\(", "high", "system() runs a shell command")],
    "rust": [(r"\btransmute\b", "high", "transmute can cause undefined behaviour"),
             (r"\bunsafe\b", "medium", "unsafe block: review manually"),
             (r"\.unwrap\(\)", "low", "unwrap() panics on None or Err"),
             (r"std::process::Command", "medium", "spawns an external process")],
    "css": [(r"expression\s*\(", "high", "CSS expression() runs script in old browsers"),
            (r"url\(\s*['\"]?javascript:", "high", "javascript: URL in CSS")],
}


def _finding(severity: str, rule: str, message: str, line: int = 0) -> dict:
    return {"severity": severity, "rule": rule, "message": message, "line": line}


def _line_of(code: str, pos: int) -> int:
    return code.count("\n", 0, pos) + 1


def _pattern_findings(lang: str, code: str) -> list[dict]:
    found = []
    for pattern, severity, message in _SECURITY.get(lang, []):
        for match in re.finditer(pattern, code):
            found.append(_finding(severity, f"{lang.upper()}_PATTERN", message, _line_of(code, match.start())))
    return found


def lexical_balance(code: str, lang: str) -> list[dict]:
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[tuple[str, int]] = []
    findings: list[dict] = []
    line, i, n = 1, 0, len(code)
    in_str = None
    while i < n:
        ch = code[i]
        nxt = code[i + 1] if i + 1 < n else ""
        if ch == "\n":
            line += 1
            i += 1
            continue
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if lang in ("javascript", "cpp", "rust") and ch == "/" and nxt == "/":
            end = code.find("\n", i)
            i = n if end < 0 else end
            continue
        if lang in ("javascript", "cpp", "rust", "css") and ch == "/" and nxt == "*":
            end = code.find("*/", i + 2)
            line += code.count("\n", i, n if end < 0 else end)
            i = n if end < 0 else end + 2
            continue
        if ch == '"' or (ch == "'" and lang != "rust") or (ch == "`" and lang == "javascript"):
            in_str = ch
        elif ch in "([{":
            stack.append((ch, line))
        elif ch in ")]}":
            if not stack or stack[-1][0] != pairs[ch]:
                findings.append(_finding("high", "UNMATCHED_BRACKET", f"unmatched '{ch}'", line))
            else:
                stack.pop()
        i += 1
    if in_str:
        findings.append(_finding("high", "UNTERMINATED_STRING", f"unterminated {in_str} string", line))
    for ch, at in stack:
        findings.append(_finding("high", "UNCLOSED_BRACKET", f"unclosed '{ch}'", at))
    return findings


def _python(code: str) -> dict:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return {"syntax_ok": False, "tool": "python ast", "lexical_only": False,
                "findings": [_finding("high", "SYNTAX", f"syntax error: {exc.msg}", exc.lineno or 0)], "metrics": {}}
    findings: list[dict] = []
    loop_depth = {"max": 0}

    def walk(node: ast.AST, depth: int) -> None:
        if isinstance(node, (ast.For, ast.While, ast.AsyncFor)):
            depth += 1
            loop_depth["max"] = max(loop_depth["max"], depth)
        for child in ast.iter_child_nodes(node):
            walk(child, depth)

    walk(tree, 0)
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            findings.append(_finding("low", "BARE_EXCEPT", "bare except also catches SystemExit and KeyboardInterrupt", line))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for default in node.args.defaults + node.args.kw_defaults:
                if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                    findings.append(_finding("medium", "MUTABLE_DEFAULT", "mutable default argument is shared between calls", line))
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            findings.append(_finding("low", "WILDCARD_IMPORT", "wildcard import hides where names come from", line))
        if isinstance(node, ast.Compare) and any(isinstance(c, ast.Constant) and c.value is None for c in node.comparators) \
                and any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
            findings.append(_finding("low", "EQ_NONE", "compare to None with 'is' or 'is not'", line))
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else None
            attr = node.func.attr if isinstance(node.func, ast.Attribute) else None
            if name in ("eval", "exec", "compile", "__import__"):
                findings.append(_finding("high", "DANGEROUS_CALL", f"{name}() runs or compiles arbitrary code", line))
            if attr == "system" or (attr == "loads" and isinstance(node.func.value, ast.Name) and node.func.value.id == "pickle"):
                findings.append(_finding("high", "UNSAFE_CALL", "os.system or pickle.loads on untrusted input is unsafe", line))
            for keyword in node.keywords:
                if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                    findings.append(_finding("high", "SHELL_TRUE", "subprocess with shell=True", line))
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and n.id in ("eval", "exec", "compile", "__import__") and id(n) not in called:
            findings.append(_finding("high", "DANGEROUS_NAME", f"{n.id} used as a value (aliasing hides the call)",
                                     getattr(n, "lineno", 0)))
    metrics = {"functions": sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in ast.walk(tree)),
               "classes": sum(isinstance(n, ast.ClassDef) for n in ast.walk(tree)),
               "max_loop_depth": loop_depth["max"], "lines": len(code.splitlines())}
    return {"syntax_ok": True, "tool": "python ast", "lexical_only": False, "findings": findings, "metrics": metrics}


def _run_tool(cmd: list[str], cwd: str, timeout: int = 10) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, (proc.stdout + proc.stderr)[-4000:]


def _javascript(code: str) -> dict:
    node = shutil.which("node")
    findings = _pattern_findings("javascript", code)
    if node is None:
        findings += lexical_balance(code, "javascript")
        return {"syntax_ok": None if not any(f["severity"] == "high" for f in findings) else False,
                "tool": "lexical (node not installed)", "lexical_only": True, "findings": findings, "metrics": {}}
    with tempfile.TemporaryDirectory(prefix="galaxy_js_") as tmp:
        path = os.path.join(tmp, "snippet.js")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(code)
        rc, out = _run_tool([node, "--check", path], tmp)
    if rc != 0:
        match = re.search(r"snippet\.js:(\d+)", out)
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        error_line = next((ln for ln in lines if "Error" in ln), next((ln for ln in lines if not ln.startswith("Node.js")), "syntax error"))
        findings.append(_finding("high", "SYNTAX", "syntax error: " + error_line[:200],
                                 int(match.group(1)) if match else 0))
    return {"syntax_ok": rc == 0, "tool": "node --check (parse only, not executed)", "lexical_only": False,
            "findings": findings, "metrics": {}}


def _html(code: str) -> dict:
    void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    class Checker(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.stack: list[str] = []
            self.problems: list[dict] = []

        def handle_starttag(self, tag, attrs):
            for name, value in attrs:
                value = (value or "").strip().lower()
                if name.startswith("on"):
                    self.problems.append(_finding("low", "INLINE_HANDLER", f"inline {name} handler on <{tag}>", self.getpos()[0]))
                if name in ("href", "src", "action") and value.startswith("javascript:"):
                    self.problems.append(_finding("high", "JAVASCRIPT_URL", f"javascript: URL in <{tag} {name}>", self.getpos()[0]))
            if tag not in void:
                self.stack.append(tag)

        def handle_endtag(self, tag):
            if tag in void:
                return
            if self.stack and self.stack[-1] == tag:
                self.stack.pop()
            elif tag in self.stack:
                while self.stack and self.stack[-1] != tag:
                    self.problems.append(_finding("high", "UNCLOSED_TAG", f"<{self.stack.pop()}> not closed before </{tag}>", self.getpos()[0]))
                self.stack.pop()
            else:
                self.problems.append(_finding("high", "STRAY_CLOSE", f"</{tag}> has no opening tag", self.getpos()[0]))

    checker = Checker()
    checker.feed(code)
    checker.close()
    findings = checker.problems + [_finding("high", "UNCLOSED_TAG", f"<{t}> is never closed", 0) for t in checker.stack[:5]]
    return {"syntax_ok": not any(f["severity"] == "high" for f in findings), "tool": "html.parser (structure)",
            "lexical_only": True, "findings": findings, "metrics": {}}


def _css(code: str) -> dict:
    findings = _pattern_findings("css", code) + lexical_balance(code, "css")
    for block in re.finditer(r"\{([^{}]*)\}", code):
        body = block.group(1)
        if not body.strip():
            findings.append(_finding("low", "EMPTY_RULE", "empty rule block", _line_of(code, block.start())))
        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        for index, ln in enumerate(lines):
            if ":" in ln and not ln.endswith((";", "{")) and index < len(lines) - 1:
                findings.append(_finding("low", "MISSING_SEMICOLON", f"declaration without ';': {ln[:60]}", _line_of(code, block.start())))
    return {"syntax_ok": not any(f["severity"] == "high" for f in findings), "tool": "css lint (lexical)",
            "lexical_only": True, "findings": findings, "metrics": {}}


def _cpp(code: str) -> dict:
    gpp = shutil.which("g++")
    findings = _pattern_findings("cpp", code)
    if gpp is None:
        findings += lexical_balance(code, "cpp")
        return {"syntax_ok": not any(f["severity"] == "high" for f in findings), "tool": "lexical (g++ not installed)",
                "lexical_only": True, "findings": findings, "metrics": {}}
    with tempfile.TemporaryDirectory(prefix="galaxy_cpp_") as tmp:
        path = os.path.join(tmp, "snippet.cpp")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(code)
        rc, out = _run_tool([gpp, "-std=c++17", "-fsyntax-only", "-Wall", "-Wextra", path], tmp)
    for match in re.finditer(r"snippet\.cpp:(\d+):\d+: (error|warning): (.*)", out):
        findings.append(_finding("high" if match.group(2) == "error" else "low", "COMPILER",
                                 match.group(3)[:200], int(match.group(1))))
    return {"syntax_ok": rc == 0, "tool": "g++ -fsyntax-only (not executed)", "lexical_only": False,
            "findings": findings, "metrics": {}}


def _rust(code: str) -> dict:
    rustc = shutil.which("rustc")
    findings = _pattern_findings("rust", code)
    if rustc is None:
        findings += lexical_balance(code, "rust")
        return {"syntax_ok": not any(f["severity"] == "high" for f in findings), "tool": "lexical (rustc not installed)",
                "lexical_only": True, "findings": findings, "metrics": {}}
    with tempfile.TemporaryDirectory(prefix="galaxy_rs_") as tmp:
        path = os.path.join(tmp, "snippet.rs")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(code)
        rc, out = _run_tool([rustc, "--edition", "2021", "--emit=metadata", "-o", os.path.join(tmp, "out.rmeta"), path], tmp)
    for match in re.finditer(r"^error(?:\[\w+\])?: (.*)\n\s*-->\s*\S+:(\d+):\d+", out, re.M):
        findings.append(_finding("high", "COMPILER", match.group(1)[:200], int(match.group(2))))
    return {"syntax_ok": rc == 0, "tool": "rustc --emit=metadata (not executed)", "lexical_only": False,
            "findings": findings, "metrics": {}}


def analyze(language: str, code: Any) -> dict:
    lang = ALIASES.get(str(language or "").strip().lower())
    if lang is None:
        return {"ok": False, "error": f"unsupported language '{language}'; supported: python, javascript, html, css, cpp, rust"}
    if not isinstance(code, str) or len(code) > MAX_CODE_CHARS:
        return {"ok": False, "error": f"code must be text of at most {MAX_CODE_CHARS:,} characters"}
    handler = {"python": _python, "javascript": _javascript, "html": _html, "css": _css, "cpp": _cpp, "rust": _rust}[lang]
    report = handler(code)
    return {"ok": True, "language": lang, **report}
