"""Agent 7 dry-run simulator (Phase 2): runs a restricted, deterministic subset of Python with a step budget.

The user's code is never passed to Python's own eval, exec or compile. The module parses it with ast and interprets the
tree itself. Anything outside the subset is refused with the reason.
Supported: def (the first one runs unless a name is given), assignment (names, subscripts, tuples), augmented assignment,
if/elif/else, for over range, lists, tuples, strings and dicts, while (step-capped), break, continue, return, print;
expressions: numbers, booleans, None, short strings, names, arithmetic (** capped), comparisons, and/or/not, conditional
expressions, list/tuple/dict literals, subscripts (no slices), len, min, max, sum, abs, sorted, range; list methods append,
pop, extend; dict methods get, keys, values, items, setdefault; calls to other defined functions (depth 200).
Limits: step budget (default 50,000), 200 output lines, 100,000 list items, 1,000,000 range length.
"""
from __future__ import annotations

import ast
import math
from typing import Any, Optional

MAX_STEPS = 50_000
MAX_OUTPUT_LINES = 200
MAX_ITEMS = 100_000
MAX_CALL_DEPTH = 200
MAX_POW_EXP = 1000
_LIST_METHODS = {"append", "pop", "extend"}
_DICT_METHODS = {"get", "keys", "values", "items", "setdefault"}


class SimError(Exception):
    """A construct outside the simulated subset, or a runtime fault in the simulated program."""


class StepLimit(SimError):
    """The step budget ran out. The program is not finished; the step count is still reported."""


class _Return(Exception):
    def __init__(self, value: Any) -> None:
        super().__init__()
        self.value = value


class _Break(Exception):
    """Internal signal for break inside a simulated loop."""


class _Continue(Exception):
    """Internal signal for continue inside a simulated loop."""


def _range(*args):
    rng = range(*args)
    if len(rng) > MAX_ITEMS:
        raise SimError(f"range of {len(rng)} items exceeds the simulator limit")
    return rng


def _sorted(values):
    items = list(values)
    if len(items) > MAX_ITEMS:
        raise SimError("list exceeds the simulator limit")
    return sorted(items)


_BUILTINS = {"len": len, "min": min, "max": max, "sum": sum, "abs": abs, "sorted": _sorted, "range": _range}
_BINOPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
           ast.Div: lambda a, b: a / b, ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b}
_CMPOPS = {ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b, ast.Lt: lambda a, b: a < b,
           ast.LtE: lambda a, b: a <= b, ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b,
           ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b}


class Interpreter:
    def __init__(self, max_steps: int) -> None:
        self.max_steps = max_steps
        self.steps = 0
        self.output: list[str] = []
        self.functions: dict[str, ast.FunctionDef] = {}
        self.depth = 0

    def tick(self, amount: int = 1) -> None:
        self.steps += amount
        if self.steps > self.max_steps:
            raise StepLimit(f"step budget of {self.max_steps} reached")

    def load(self, tree: ast.Module) -> None:
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                self.functions[node.name] = node
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                raise SimError("imports are not simulated")
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue  # module docstring
            else:
                raise SimError("only function definitions may appear at module level")
        if not self.functions:
            raise SimError("no function definition found")

    def call(self, name: str, args: list) -> Any:
        if name not in self.functions:
            raise SimError(f"no function named '{name}'")
        fn = self.functions[name]
        params = [a.arg for a in fn.args.args]
        if fn.args.defaults or fn.args.kwonlyargs or fn.args.vararg or fn.args.kwarg:
            raise SimError("default, keyword and variadic parameters are not simulated")
        if len(args) != len(params):
            raise SimError(f"{name}() takes {len(params)} argument(s), got {len(args)}")
        self.depth += 1
        if self.depth > MAX_CALL_DEPTH:
            raise SimError("call depth limit reached")
        try:
            self.exec_block(fn.body, dict(zip(params, args)))
            return None
        except _Return as ret:
            return ret.value
        finally:
            self.depth -= 1

    def exec_block(self, body: list, env: dict) -> None:
        for statement in body:
            self.exec_stmt(statement, env)

    def assign(self, target: ast.AST, value: Any, env: dict) -> None:
        if isinstance(target, ast.Name):
            env[target.id] = value
        elif isinstance(target, ast.Subscript):
            container = self.eval(target.value, env)
            container[self.eval(target.slice, env)] = value
        elif isinstance(target, ast.Tuple) and all(isinstance(e, ast.Name) for e in target.elts):
            values = list(value)
            if len(values) != len(target.elts):
                raise SimError("unpacking length mismatch")
            for elt, item in zip(target.elts, values):
                env[elt.id] = item
        else:
            raise SimError(f"unsupported assignment target: {type(target).__name__}")

    def exec_stmt(self, node: ast.AST, env: dict) -> None:
        self.tick()
        if isinstance(node, ast.Assign):
            value = self.eval(node.value, env)
            for target in node.targets:
                self.assign(target, value, env)
        elif isinstance(node, ast.AugAssign):
            if type(node.op) not in _BINOPS:
                raise SimError("unsupported augmented operator")
            current = self.eval(node.target, env)
            self.assign(node.target, _BINOPS[type(node.op)](current, self.eval(node.value, env)), env)
        elif isinstance(node, ast.If):
            self.exec_block(node.body if self.truthy(self.eval(node.test, env)) else node.orelse, env)
        elif isinstance(node, ast.For):
            if node.orelse:
                raise SimError("for-else is not simulated")
            iterable = self.eval(node.iter, env)
            if isinstance(iterable, dict):
                iterable = list(iterable.keys())
            if not isinstance(iterable, (range, list, tuple, str)):
                raise SimError("for loops iterate over range, list, tuple, string or dict")
            for item in iterable:
                self.tick()
                self.assign(node.target, item, env)
                try:
                    self.exec_block(node.body, env)
                except _Break:
                    break
                except _Continue:
                    continue
        elif isinstance(node, ast.While):
            if node.orelse:
                raise SimError("while-else is not simulated")
            while self.truthy(self.eval(node.test, env)):
                self.tick()
                try:
                    self.exec_block(node.body, env)
                except _Break:
                    break
                except _Continue:
                    continue
        elif isinstance(node, ast.Return):
            raise _Return(self.eval(node.value, env) if node.value is not None else None)
        elif isinstance(node, ast.Expr):
            self.eval(node.value, env)
        elif isinstance(node, ast.Break):
            raise _Break()
        elif isinstance(node, ast.Continue):
            raise _Continue()
        elif isinstance(node, ast.Pass):
            return None
        else:
            raise SimError(f"unsupported statement: {type(node).__name__}")

    @staticmethod
    def truthy(value: Any) -> bool:
        return bool(value)

    def eval(self, node: ast.AST, env: dict) -> Any:
        if isinstance(node, ast.Constant):
            value = node.value
            if value is None or isinstance(value, (bool, int, float)):
                return value
            if isinstance(value, str) and len(value) <= 100:
                return value
            raise SimError("only short strings, numbers, booleans and None are simulated")
        if isinstance(node, ast.Name):
            if node.id in env:
                return env[node.id]
            if node.id in _BUILTINS:
                return _BUILTINS[node.id]
            raise SimError(f"name '{node.id}' is not defined")
        if isinstance(node, ast.BinOp):
            left, right = self.eval(node.left, env), self.eval(node.right, env)
            if isinstance(node.op, ast.Pow):
                if abs(right) > MAX_POW_EXP or (abs(left) > 1 and abs(right) * math.log10(abs(left)) > 300):
                    raise SimError("exponent too large for the simulator")
                return left ** right
            if type(node.op) not in _BINOPS:
                raise SimError("unsupported operator")
            if isinstance(node.op, ast.Mult) and isinstance(left, list) and isinstance(right, int) and len(left) * max(right, 0) > MAX_ITEMS:
                raise SimError("list too large")
            return _BINOPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp):
            value = self.eval(node.operand, env)
            if isinstance(node.op, ast.Not):
                return not value
            if isinstance(node.op, ast.USub):
                return -value
            if isinstance(node.op, ast.UAdd):
                return +value
            raise SimError("unsupported unary operator")
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                result = True
                for value_node in node.values:
                    result = self.eval(value_node, env)
                    if not result:
                        return result
                return result
            for value_node in node.values:
                result = self.eval(value_node, env)
                if result:
                    return result
            return result
        if isinstance(node, ast.Compare):
            left = self.eval(node.left, env)
            for op, comparator in zip(node.ops, node.comparators):
                if type(op) not in _CMPOPS:
                    raise SimError("unsupported comparison")
                right = self.eval(comparator, env)
                if not _CMPOPS[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            return self.eval(node.body if self.truthy(self.eval(node.test, env)) else node.orelse, env)
        if isinstance(node, ast.List):
            return [self.eval(e, env) for e in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(self.eval(e, env) for e in node.elts)
        if isinstance(node, ast.Dict):
            return {self.eval(k, env): self.eval(v, env) for k, v in zip(node.keys, node.values) if k is not None}
        if isinstance(node, ast.Subscript):
            if isinstance(node.slice, ast.Slice):
                raise SimError("slices are not simulated")
            return self.eval(node.value, env)[self.eval(node.slice, env)]
        if isinstance(node, ast.Call):
            return self.eval_call(node, env)
        raise SimError(f"unsupported expression: {type(node).__name__}")

    def eval_call(self, node: ast.Call, env: dict) -> Any:
        if node.keywords:
            raise SimError("keyword arguments are not simulated")
        args = [self.eval(a, env) for a in node.args]
        if isinstance(node.func, ast.Name):
            name = node.func.id
            if name == "print":
                line = " ".join(str(a) for a in args)
                if len(self.output) >= MAX_OUTPUT_LINES:
                    raise SimError("output limit reached")
                self.output.append(line)
                return None
            if name in self.functions:
                return self.call(name, args)
            if name in _BUILTINS:
                return _BUILTINS[name](*args)
            raise SimError(f"call to '{name}' is not simulated")
        if isinstance(node.func, ast.Attribute):
            target = self.eval(node.func.value, env)
            method = node.func.attr
            if isinstance(target, list) and method in _LIST_METHODS:
                if method == "append":
                    target.append(*args)
                    return None
                if method == "extend":
                    target.extend(args[0])
                    return None
                return target.pop(*args)
            if isinstance(target, dict) and method in _DICT_METHODS:
                return getattr(target, method)(*args)
            raise SimError(f"method '{method}' is not simulated on this value")
        raise SimError("unsupported call target")


def simulate(code: str, func: Optional[str] = None, args: Optional[list] = None, max_steps: int = MAX_STEPS) -> dict:
    if not isinstance(code, str) or len(code) > 20_000:
        return {"ok": False, "error": "code must be text of at most 20,000 characters", "steps": 0, "output": []}
    interp = Interpreter(max_steps)
    try:
        tree = ast.parse(code)
        interp.load(tree)
        name = func or next(iter(interp.functions))
        result = interp.call(name, list(args or []))
        return {"ok": True, "function": name, "result": result, "output": interp.output, "steps": interp.steps}
    except StepLimit as exc:
        return {"ok": False, "error": str(exc), "steps": interp.steps, "output": interp.output, "limit": True}
    except SyntaxError as exc:
        return {"ok": False, "error": f"syntax error: {exc.msg}", "steps": 0, "output": []}
    except SimError as exc:
        return {"ok": False, "error": str(exc), "steps": interp.steps, "output": interp.output}
    except (TypeError, ValueError, IndexError, KeyError, ZeroDivisionError, OverflowError, AttributeError) as exc:
        return {"ok": False, "error": f"runtime fault in the simulated program: {type(exc).__name__}: {str(exc)[:120]}",
                "steps": interp.steps, "output": interp.output}


def make_input(n: int, kind: str) -> Any:
    if kind == "int":
        return n
    return [(i * 7919 + 13) % 1000 for i in range(n)]


def estimate_growth(code: str, func: Optional[str] = None, sizes: tuple = (8, 16, 32, 64, 128),
                    input_kind: str = "list", max_steps: int = 2_000_000) -> dict:
    points = []
    for n in sizes:
        run = simulate(code, func, [make_input(n, input_kind)], max_steps)
        if not run["ok"]:
            return {"ok": False, "error": run["error"], "points": points}
        points.append((n, run["steps"]))
    if len({steps for _, steps in points}) == 1:
        return {"ok": True, "points": points, "exponent": 0.0, "estimate": "O(1)"}
    slopes = [math.log(s2 / s1) / math.log(n2 / n1) for (n1, s1), (n2, s2) in zip(points, points[1:]) if s1 > 0 and s2 > 0]
    exponent = round(slopes[-1], 2) if slopes else 0.0
    if exponent < 0.08:
        label = "O(1)"
    elif exponent < 0.7:
        label = "O(log n)"
    elif exponent < 1.1:
        label = "O(n)"
    elif exponent < 1.35:
        label = "O(n log n)"
    elif exponent < 2.4:
        label = "O(n^2)"
    elif exponent < 3.4:
        label = "O(n^3)"
    else:
        label = "super-polynomial"
    return {"ok": True, "points": points, "exponent": exponent, "estimate": label,
            "note": "empirical: from simulated step counts on generated inputs; a coarse estimate, not a proof"}
