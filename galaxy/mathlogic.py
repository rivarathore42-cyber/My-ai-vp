"""Agent 4 - Math Logic (Phase 1): safe AST evaluator with an extended grammar and an exact step trace.

Guarantees, enforced by the tests:
  - no eval(), no exec(), no compile(): the expression is parsed with ast and walked node by node;
  - only numbers, the whitelisted operators, the whitelisted functions and the constants pi, e, tau are accepted;
    attribute access, subscripts, lambdas, comprehensions, strings, keyword arguments and unknown names are refused;
  - guards on exponent size, factorial and permutation size, and expression length and nesting depth.
Steps: every operation and function call is recorded as 'operation = value', in evaluation order, and the last step
equals the final value.
"""
from __future__ import annotations

import ast
import math
import operator
import re
from typing import Optional

from .core import Agent, AgentResult, fail, ok
from .numparse import thousands_to_plain

MAX_EXPR_CHARS = 300
MAX_AST_DEPTH = 60
MAX_EXPONENT = 1000
MAX_FACTORIAL = 300
MAX_PERM_N = 1000
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_UN = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_SYM = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.FloorDiv: "//", ast.Mod: "%", ast.Pow: "**"}
_CONST = {"pi": math.pi, "e": math.e, "tau": math.tau}


def fmt_num(value) -> str:
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    if isinstance(value, float):
        return f"{value:.10g}"
    return str(value)


def _whole(name: str, value) -> int:
    if isinstance(value, bool) or not float(value).is_integer():
        raise ValueError(f"{name} needs whole numbers")
    return int(value)


def _factorial(n):
    k = _whole("factorial", n)
    if k < 0 or k > MAX_FACTORIAL:
        raise ValueError(f"factorial argument must be 0..{MAX_FACTORIAL}")
    return math.factorial(k)


def _perm(n, k):
    a, b = _whole("perm", n), _whole("perm", k)
    if not (0 <= b <= a <= MAX_PERM_N):
        raise ValueError(f"perm needs 0 <= k <= n <= {MAX_PERM_N}")
    return math.perm(a, b)


def _comb(n, k):
    a, b = _whole("comb", n), _whole("comb", k)
    if not (0 <= b <= a <= MAX_PERM_N):
        raise ValueError(f"comb needs 0 <= k <= n <= {MAX_PERM_N}")
    return math.comb(a, b)


def _gcd(a, b):
    return math.gcd(_whole("gcd", a), _whole("gcd", b))


def _lcm(a, b):
    x, y = _whole("lcm", a), _whole("lcm", b)
    return 0 if x == 0 or y == 0 else abs(x * y) // math.gcd(x, y)


def _log(x, base=None):
    if x <= 0:
        raise ValueError("log needs a positive number")
    return math.log(x) if base is None else math.log(x, base)


def _positive(name, fn):
    def inner(x):
        if x <= 0:
            raise ValueError(f"{name} needs a positive number")
        return fn(x)
    return inner


def _cbrt(x):
    return math.copysign(abs(x) ** (1.0 / 3.0), x)


def _sqrt(x):
    if x < 0:
        raise ValueError("sqrt of a negative number")
    return math.sqrt(x)


_FUNCS = {
    "sqrt": _sqrt, "cbrt": _cbrt, "log": _log, "ln": _positive("ln", math.log), "log10": _positive("log10", math.log10),
    "log2": _positive("log2", math.log2), "exp": math.exp, "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan, "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
    "deg": math.degrees, "rad": math.radians, "hypot": math.hypot, "factorial": _factorial, "perm": _perm,
    "comb": _comb, "gcd": _gcd, "lcm": _lcm, "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil,
    "min": min, "max": max,
}
FUNCTION_NAMES = tuple(sorted(_FUNCS, key=len, reverse=True))


def _pow_safe(a, b) -> bool:
    if abs(b) > MAX_EXPONENT:
        return False
    if abs(a) > 1 and abs(b) * math.log10(abs(a)) > 308:
        return False
    return True


class _Evaluator:
    def __init__(self) -> None:
        self.steps: list[str] = []

    def _step(self, text: str) -> None:
        self.steps.append(text)

    def eval_node(self, node: ast.AST, depth: int = 0):
        if depth > MAX_AST_DEPTH:
            raise ValueError("expression is nested too deeply")
        if isinstance(node, ast.Expression):
            return self.eval_node(node.body, depth + 1)
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("only numbers are allowed")
            return value
        if isinstance(node, ast.Name):
            if node.id in _CONST:
                return _CONST[node.id]
            raise ValueError(f"unknown name '{node.id}'")
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            a = self.eval_node(node.left, depth + 1)
            b = self.eval_node(node.right, depth + 1)
            if isinstance(node.op, ast.Pow) and not _pow_safe(a, b):
                raise ValueError("exponent too large")
            value = _BIN[type(node.op)](a, b)
            self._step(f"{fmt_num(a)} {_SYM[type(node.op)]} {fmt_num(b)} = {fmt_num(value)}")
            return value
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UN:
            return _UN[type(node.op)](self.eval_node(node.operand, depth + 1))
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
                raise ValueError("only whitelisted function names may be called")
            if node.keywords:
                raise ValueError("keyword arguments are not allowed")
            args = [self.eval_node(a, depth + 1) for a in node.args]
            value = _FUNCS[node.func.id](*args)
            self._step(f"{node.func.id}({', '.join(fmt_num(a) for a in args)}) = {fmt_num(value)}")
            return value
        raise ValueError(f"unsupported element: {type(node).__name__}")


def preprocess(expr: str) -> str:
    text = thousands_to_plain(expr.strip()).replace("^", "**").replace("×", "*").replace("÷", "/")
    text = re.sub(r"\b(\d+)\s*C\s*(\d+)\b", r"comb(\1,\2)", text)
    text = re.sub(r"\b(\d+)\s*P\s*(\d+)\b", r"perm(\1,\2)", text)
    text = re.sub(r"\b(\d+)\s+choose\s+(\d+)\b", r"comb(\1,\2)", text, flags=re.I)
    text = re.sub(r"\bC\s*\(", "comb(", text)
    text = re.sub(r"\bP\s*\(", "perm(", text)
    text = re.sub(r"(\d+(?:\.\d+)?)\s*!", r"factorial(\1)", text)
    return text


def evaluate(expr: str) -> dict:
    if not isinstance(expr, str) or not expr.strip() or len(expr) > MAX_EXPR_CHARS:
        return {"ok": False, "error": "expression is empty or longer than 300 characters"}
    try:
        tree = ast.parse(preprocess(expr), mode="eval")
    except SyntaxError:
        return {"ok": False, "error": "cannot evaluate: not a valid arithmetic expression"}
    evaluator = _Evaluator()
    try:
        value = evaluator.eval_node(tree)
        if isinstance(value, complex) or (isinstance(value, float) and not math.isfinite(value)):
            raise ValueError("result is not a finite real number")
        return {"ok": True, "value": fmt_num(value), "steps": evaluator.steps}
    except ZeroDivisionError:
        return {"ok": False, "error": "division by zero"}
    except (ValueError, OverflowError, TypeError, RecursionError) as exc:
        return {"ok": False, "error": f"cannot evaluate: {str(exc)[:120]}"}


_NUM = r"(-?\d+(?:\.\d+)?)"
_PERCENT_PATTERNS = [
    ("percent_of", re.compile(_NUM + r"\s*%\s*of\s*" + _NUM, re.I)),
    ("increase", re.compile(r"(?:increase|raise|grow)\s+" + _NUM + r"\s+by\s+" + _NUM + r"\s*%", re.I)),
    ("decrease", re.compile(r"(?:decrease|reduce|drop|cut)\s+" + _NUM + r"\s+by\s+" + _NUM + r"\s*%", re.I)),
    ("change", re.compile(r"percent(?:age)?\s+(?:change|increase|decrease)\s+from\s+" + _NUM + r"\s+to\s+" + _NUM, re.I)),
    ("what_percent", re.compile(r"what\s+(?:percent|percentage)\s+(?:is|of)\s+" + _NUM + r"\s+(?:of\s+)?" + _NUM, re.I)),
]


def solve_percent(text: str) -> Optional[dict]:
    for kind, pattern in _PERCENT_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        a, b = float(match.group(1)), float(match.group(2))
        if kind == "percent_of":
            part = a / 100
            return {"ok": True, "value": fmt_num(part * b), "steps": [
                f"{fmt_num(a)} / 100 = {fmt_num(part)}", f"{fmt_num(part)} * {fmt_num(b)} = {fmt_num(part * b)}"],
                "label": f"{fmt_num(a)}% of {fmt_num(b)}"}
        if kind in ("increase", "decrease"):
            base, pct = a, b
            delta = base * pct / 100
            total = base + delta if kind == "increase" else base - delta
            op = "+" if kind == "increase" else "-"
            return {"ok": True, "value": fmt_num(total), "steps": [
                f"{fmt_num(base)} * {fmt_num(pct)} / 100 = {fmt_num(delta)}", f"{fmt_num(base)} {op} {fmt_num(delta)} = {fmt_num(total)}"],
                "label": f"{kind} {fmt_num(base)} by {fmt_num(pct)}%"}
        if kind == "change":
            if a == 0:
                return {"ok": False, "error": "percent change from zero is undefined"}
            diff = b - a
            pct = diff / a * 100
            return {"ok": True, "value": fmt_num(pct), "steps": [
                f"{fmt_num(b)} - {fmt_num(a)} = {fmt_num(diff)}", f"{fmt_num(diff)} / {fmt_num(a)} = {fmt_num(diff / a)}",
                f"{fmt_num(diff / a)} * 100 = {fmt_num(pct)}"], "label": f"percent change from {fmt_num(a)} to {fmt_num(b)}"}
        if b == 0:
            return {"ok": False, "error": "division by zero"}
        ratio = a / b
        return {"ok": True, "value": fmt_num(ratio * 100), "steps": [
            f"{fmt_num(a)} / {fmt_num(b)} = {fmt_num(ratio)}", f"{fmt_num(ratio)} * 100 = {fmt_num(ratio * 100)}"],
            "label": f"what percent is {fmt_num(a)} of {fmt_num(b)}"}
    return None


def extract(text: str) -> Optional[str]:
    best = None
    fn_pattern = "|".join(re.escape(n) for n in FUNCTION_NAMES)
    for match in re.finditer(r"[\w.()\s+\-*/^×÷%!,]+", text):
        chunk = match.group(0).strip()
        if not re.search(r"\d", chunk):
            continue
        core = re.sub(r"^(?:[a-z]+\s+)*?(?=[\d(\-])", "", chunk, flags=re.I)
        core = re.sub(r"(?<=[\d)!])\s+[a-z][a-z\s]*$", "", core, flags=re.I).strip(" ?.")
        if re.search(rf"[-+*/^×÷%()!]|\b(?:{fn_pattern})\b|\b\d+\s*[CP]\s*\d+\b|\bchoose\b", core, re.I):
            if best is None or len(core) > len(best):
                best = core
    return best


_WORD_OPS = [(r"\bmultiplied\s+by\b|\btimes\b", "*"), (r"\bdivided\s+by\b", "/"), (r"\bto\s+the\s+power\s+of\b|\braised\s+to\b", "^"),
              (r"\bplus\b", "+"), (r"\bminus\b", "-"), (r"\bmodulo\b|\bmod\b", "%")]


def normalize_natural(text: str) -> str:
    """Turns spoken operators into symbols (A1, A4): '5 times 4', '15 percent of 200', 'raised to'."""
    text = re.sub(r"(\d+(?:\.\d+)?)\s*percent\b", r"\1%", text, flags=re.I)
    for pattern, symbol in _WORD_OPS:
        text = re.sub(pattern, symbol, text, flags=re.I)
    return text


class MathLogicAgent(Agent):
    name = "Math Logic"

    def _eval(self, node, steps):  # kept for callers that used the v6 signature
        raise NotImplementedError("use evaluate(); the evaluator is internal to mathlogic")

    def evaluate(self, expr: str) -> dict:
        return evaluate(expr)

    def extract(self, text: str) -> Optional[str]:
        return extract(text)

    def looks_like_math(self, text: str) -> bool:
        return extract(text) is not None or solve_percent(text) is not None

    async def process(self, text: str, ctx: dict) -> AgentResult:
        text = normalize_natural(text)
        pct = solve_percent(text)
        if pct is not None:
            if not pct["ok"]:
                return fail(self.name, pct["error"])
            body = f"**Answer:** `{pct['label']} = {pct['value']}`\n\nSteps:\n" + \
                   "\n".join(f"{i}. {s}" for i, s in enumerate(pct["steps"], 1))
            return ok(self.name, body, value=pct["value"], cacheable=True)
        claim = re.search(r"(\d[\d\s.+\-*/^()%]*\d)\s*=\s*(-?\d+(?:\.\d+)?)", text)
        if claim:
            actual = evaluate(claim.group(1))
            if actual["ok"] and abs(float(actual["value"]) - float(claim.group(2))) > 1e-9:
                return ok(self.name, f"**Mismatch:** `{claim.group(1).strip()} = {actual['value']}`, not {claim.group(2)}. "
                                     "The claimed result is wrong.", value=actual["value"], cacheable=False)
        expr = extract(text) or text
        result = evaluate(expr)
        if not result["ok"]:
            return fail(self.name, result["error"])
        body = f"**Answer:** `{expr} = {result['value']}`"
        if result["steps"]:
            body += "\n\nSteps:\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(result["steps"], 1))
        return ok(self.name, body, value=result["value"], cacheable=True)
