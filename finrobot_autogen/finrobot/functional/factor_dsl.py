"""Safe trailing-only expression DSL for LLM-proposed timing factors.

The DSL is interpreted from a validated AST. It never calls ``eval``/``exec``
and never allows attribute access, subscripting, imports, lambdas or string
literals. Every primitive looks backwards only, so a signal at date ``t``
cannot use data after ``t``.
"""
from __future__ import annotations

import ast
import math
from typing import Any

import numpy as np
import pandas as pd

SERIES_NAMES = {"open", "high", "low", "close", "volume"}
MIN_WINDOW = 1
MAX_WINDOW = 252
MAX_EXPRESSION_LENGTH = 4000
MAX_SHIFT = 252
MAX_AST_NODES = 256
MAX_AST_DEPTH = 32
MAX_LITERAL_ABS = 1e12

WINDOW_FUNCTIONS = {"sma", "ema", "std", "ret", "shift", "zscore", "rank", "delta"}
SCALAR_FUNCTIONS = {"abs", "sign", "log", "sqrt", "max", "min", "where", "clip"}
ALLOWED_FUNCTIONS = WINDOW_FUNCTIONS | SCALAR_FUNCTIONS
ARITY = {name: 2 for name in WINDOW_FUNCTIONS}
ARITY.update({"abs": 1, "sign": 1, "log": 1, "sqrt": 1, "max": 2, "min": 2, "where": 3, "clip": 3})

_ALLOWED_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Compare,
    ast.BoolOp,
    ast.Call,
    ast.Name,
    ast.Constant,
    ast.Load,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Mod,
    ast.USub,
    ast.UAdd,
    ast.Not,
    ast.Gt,
    ast.GtE,
    ast.Lt,
    ast.LtE,
    ast.Eq,
    ast.NotEq,
    ast.And,
    ast.Or,
)


class FactorDslError(ValueError):
    """Raised when an LLM expression violates the factor DSL contract."""


def parse_expression(expression: str) -> ast.Expression:
    """Parse and validate an expression without evaluating it."""
    if not isinstance(expression, str) or not expression.strip():
        raise FactorDslError("Expression must be a non-empty string")
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise FactorDslError("Expression exceeds the length limit")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise FactorDslError(f"Invalid expression syntax: {exc.msg}") from exc
    pending, count = [(tree, 0)], 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > MAX_AST_NODES or depth > MAX_AST_DEPTH:
            raise FactorDslError("Expression exceeds the AST node or nesting limit")
        pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise FactorDslError(f"Disallowed syntax: {type(node).__name__}")
        if isinstance(node, ast.Name):
            if node.id not in SERIES_NAMES and node.id not in ALLOWED_FUNCTIONS:
                raise FactorDslError(f"Unknown name: {node.id}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in ALLOWED_FUNCTIONS:
                raise FactorDslError("Only whitelisted functions may be called")
            if node.keywords:
                raise FactorDslError("Keyword arguments are not allowed")
            name = node.func.id
            expected = ARITY[name]
            if len(node.args) != expected:
                raise FactorDslError(f"{name} expects {expected} arguments, got {len(node.args)}")
            if name in WINDOW_FUNCTIONS:
                window = node.args[1]
                if not isinstance(window, ast.Constant) or isinstance(window.value, bool) \
                        or not isinstance(window.value, int):
                    raise FactorDslError(f"{name} window must be an integer literal")
                low = 0 if name == "shift" else MIN_WINDOW
                high = MAX_SHIFT if name == "shift" else MAX_WINDOW
                if not low <= window.value <= high:
                    raise FactorDslError(f"{name} window must be in [{low}, {high}]")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float, bool)):
            raise FactorDslError("Only numeric and boolean constants are allowed")
        if isinstance(node, ast.Constant) and not isinstance(node.value, bool):
            if abs(node.value) > MAX_LITERAL_ABS or not math.isfinite(node.value):
                raise FactorDslError("Numeric literal exceeds the finite magnitude limit")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
            for side in (node.left, node.right):
                if isinstance(side, ast.Constant) and side.value == 0:
                    raise FactorDslError("Multiplying by zero is a degenerate expression")
    if not isinstance(tree.body, (ast.Compare, ast.BoolOp)):
        raise FactorDslError(
            "Signal must be a comparison or boolean combination, "
            "for example sma(close, 5) > sma(close, 20)"
        )
    return tree


def _as_series(value: Any, index: pd.Index, label: str) -> pd.Series:
    if isinstance(value, pd.Series):
        return value
    if isinstance(value, (int, float, bool)) and not isinstance(value, complex):
        return pd.Series(float(value), index=index)
    raise FactorDslError(f"{label} must be a market series or a number")


def _window(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FactorDslError(f"{name} window must be an integer literal")
    if not MIN_WINDOW <= value <= MAX_WINDOW:
        raise FactorDslError(f"{name} window must be in [{MIN_WINDOW}, {MAX_WINDOW}]")
    return value


def _divide(left: Any, right: Any) -> Any:
    with np.errstate(divide="ignore", invalid="ignore"):
        result = left / right
    if isinstance(result, pd.Series):
        return result.replace([np.inf, -np.inf], np.nan)
    if isinstance(result, float) and not math.isfinite(result):
        return float("nan")
    return result


def _compare(left: Any, op: ast.cmpop, right: Any) -> Any:
    if isinstance(op, ast.Gt):
        return left > right
    if isinstance(op, ast.GtE):
        return left >= right
    if isinstance(op, ast.Lt):
        return left < right
    if isinstance(op, ast.LtE):
        return left <= right
    if isinstance(op, ast.Eq):
        return left == right
    if isinstance(op, ast.NotEq):
        return left != right
    raise FactorDslError(f"Unsupported comparison: {type(op).__name__}")


class _Evaluator:
    def __init__(self, data: pd.DataFrame) -> None:
        if not isinstance(data.index, pd.DatetimeIndex):
            raise FactorDslError("Factor data must use a DatetimeIndex")
        self.data = data
        self._columns = {str(column).lower(): column for column in data.columns}

    def visit(self, node: ast.AST) -> Any:
        method = getattr(self, f"visit_{type(node).__name__}", None)
        if method is None:
            raise FactorDslError(f"Disallowed syntax: {type(node).__name__}")
        return method(node)

    def visit_Expression(self, node: ast.Expression) -> Any:
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> Any:
        return node.value

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id not in SERIES_NAMES:
            raise FactorDslError(f"Function used as a value: {node.id}")
        column = self._columns.get(node.id)
        if column is None:
            raise FactorDslError(f"Missing market column: {node.id}")
        return pd.to_numeric(self.data[column], errors="coerce").astype(float)

    def visit_BinOp(self, node: ast.BinOp) -> Any:
        left, right = self.visit(node.left), self.visit(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return _divide(left, right)
        if isinstance(node.op, ast.Mod):
            return left % right
        raise FactorDslError(f"Unsupported operator: {type(node.op).__name__}")

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        value = self.visit(node.operand)
        if isinstance(node.op, ast.USub):
            return -value
        if isinstance(node.op, ast.UAdd):
            return value
        if isinstance(node.op, ast.Not):
            return ~value if isinstance(value, pd.Series) else not value
        raise FactorDslError(f"Unsupported unary operator: {type(node.op).__name__}")

    def visit_Compare(self, node: ast.Compare) -> Any:
        left = self.visit(node.left)
        result: Any = None
        for op, comparator in zip(node.ops, node.comparators):
            right = self.visit(comparator)
            current = _compare(left, op, right)
            result = current if result is None else (result & current)
            left = right
        return result

    def visit_BoolOp(self, node: ast.BoolOp) -> Any:
        values = [self.visit(value) for value in node.values]
        result = values[0]
        for value in values[1:]:
            result = (result & value) if isinstance(node.op, ast.And) else (result | value)
        return result

    def visit_Call(self, node: ast.Call) -> Any:
        name = node.func.id
        args = [self.visit(arg) for arg in node.args]
        return self._call(name, args)

    def _call(self, name: str, args: list[Any]) -> Any:
        index = self.data.index
        if name == "sma":
            self._arity(name, args, 2)
            return _as_series(args[0], index, name).rolling(_window(args[1], name)).mean()
        if name == "ema":
            self._arity(name, args, 2)
            window = _window(args[1], name)
            return _as_series(args[0], index, name).ewm(span=window, adjust=False, min_periods=window).mean()
        if name == "std":
            self._arity(name, args, 2)
            return _as_series(args[0], index, name).rolling(_window(args[1], name)).std()
        if name == "ret":
            self._arity(name, args, 2)
            return _as_series(args[0], index, name).pct_change(_window(args[1], name))
        if name == "shift":
            self._arity(name, args, 2)
            if isinstance(args[1], bool) or not isinstance(args[1], int) or not 0 <= args[1] <= MAX_SHIFT:
                raise FactorDslError(f"shift periods must be an integer in [0, {MAX_SHIFT}]")
            return _as_series(args[0], index, name).shift(args[1])
        if name == "zscore":
            self._arity(name, args, 2)
            series = _as_series(args[0], index, name)
            window = _window(args[1], name)
            mean = series.rolling(window).mean()
            std = series.rolling(window).std()
            return _divide(series - mean, std)
        if name == "rank":
            self._arity(name, args, 2)
            return _as_series(args[0], index, name).rolling(_window(args[1], name)).rank(pct=True)
        if name == "delta":
            self._arity(name, args, 2)
            series = _as_series(args[0], index, name)
            window = _window(args[1], name)
            return series - series.shift(window)
        if name == "abs":
            self._arity(name, args, 1)
            return abs(args[0])
        if name == "sign":
            self._arity(name, args, 1)
            return np.sign(args[0])
        if name == "log":
            self._arity(name, args, 1)
            series = _as_series(args[0], index, name)
            return np.log(series.where(series > 0))
        if name == "sqrt":
            self._arity(name, args, 1)
            series = _as_series(args[0], index, name)
            return np.sqrt(series.where(series >= 0))
        if name == "max":
            self._arity(name, args, 2)
            return np.maximum(args[0], args[1])
        if name == "min":
            self._arity(name, args, 2)
            return np.minimum(args[0], args[1])
        if name == "where":
            self._arity(name, args, 3)
            condition = args[0]
            if isinstance(condition, pd.Series):
                return pd.Series(np.where(condition.fillna(False), args[1], args[2]), index=index)
            return args[1] if condition else args[2]
        if name == "clip":
            self._arity(name, args, 3)
            series = _as_series(args[0], index, name)
            return series.clip(lower=args[1], upper=args[2])
        raise FactorDslError(f"Unknown function: {name}")

    @staticmethod
    def _arity(name: str, args: list[Any], expected: int) -> None:
        if len(args) != expected:
            raise FactorDslError(f"{name} expects {expected} arguments, got {len(args)}")


def signal_from_expression(expression: str, data: pd.DataFrame) -> pd.Series:
    """Evaluate an expression into an aligned 0/1 signal.

    Warm-up values are set to 0 (flat). The result is causally safe because the
    DSL only exposes trailing primitives.
    """
    tree = parse_expression(expression)
    values = _Evaluator(data).visit(tree.body)
    if not isinstance(values, pd.Series):
        raise FactorDslError("Signal must depend on at least one market series")
    values = values.reindex(data.index)
    if values.isna().all():
        raise FactorDslError("Signal is entirely undefined")
    values = values.fillna(False)
    if values.dtype != bool:
        if not values.isin([0, 1]).all():
            raise FactorDslError("Signal must evaluate to a boolean or 0/1 values")
        values = values.astype(bool)
    return values.astype(int)


def validate_causality(expression: str, data: pd.DataFrame, stops: tuple[int, ...] = ()) -> None:
    """Check that truncating future data never changes past signals."""
    full = signal_from_expression(expression, data)
    for stop in stops:
        if not 1 <= stop < len(data):
            continue
        prefix = signal_from_expression(expression, data.iloc[:stop])
        if not prefix.equals(full.iloc[:stop]):
            raise FactorDslError(f"Prefix causality check failed at row {stop}")


def validate_idea(idea: Any, data: pd.DataFrame, warmup: int) -> dict[str, Any]:
    """Validate one LLM idea object against the DSL and causal contract."""
    if not isinstance(idea, dict):
        raise FactorDslError("Each idea must be a JSON object")
    for key in ("name", "hypothesis", "signal"):
        if not isinstance(idea.get(key), str) or not idea[key].strip():
            raise FactorDslError(f"Idea field {key!r} must be a non-empty string")
    if not isinstance(idea.get("parameters"), dict):
        raise FactorDslError("Idea field 'parameters' must be an object")
    notes = idea.get("risk_notes")
    if not isinstance(notes, list) or not all(isinstance(note, str) for note in notes):
        raise FactorDslError("Idea field 'risk_notes' must be a list of strings")
    expression = idea["signal"].strip()
    parse_expression(expression)
    signal = signal_from_expression(expression, data)
    if not 0 <= warmup < len(signal):
        raise FactorDslError("Warm-up must leave at least one ideation observation")
    evaluated = signal.iloc[warmup:]
    if evaluated.nunique() == 1:
        raise FactorDslError("Signal is constant over the ideation window")
    stops = tuple(stop for stop in (warmup + 20, len(data) // 2, len(data) - 1) if 0 < stop < len(data))
    validate_causality(expression, data, stops)
    return {
        "name": idea["name"].strip(),
        "hypothesis": idea["hypothesis"].strip(),
        "signal": expression,
        "parameters": idea["parameters"],
        "risk_notes": notes,
        "valid": True,
        "observations": int(len(signal)),
        "long_days": int(signal.sum()),
        "evaluated_observations": int(len(evaluated)),
        "evaluated_long_days": int(evaluated.sum()),
    }


def validate_ideas(ideas: Any, data: pd.DataFrame, warmup: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return validated ideas and per-item errors; one bad idea does not hide the rest."""
    if not isinstance(ideas, list):
        raise FactorDslError("'ideas' must be a list")
    validated, errors = [], []
    for position, idea in enumerate(ideas):
        try:
            validated.append(validate_idea(idea, data, warmup))
        except Exception as exc:
            label = idea.get("name") if isinstance(idea, dict) else None
            errors.append({"position": position, "name": label, "error_type": type(exc).__name__,
                           "error": str(exc), "signal": idea.get("signal") if isinstance(idea, dict) else None})
    return validated, errors
