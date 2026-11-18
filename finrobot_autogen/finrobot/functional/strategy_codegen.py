"""Constrained Python strategy modules for the fixed V2 task."""
import ast

import pandas as pd

from .quantitative import reference_signals, run_signal_backtest


def validate_source(source):
    if len(source) > 20000:
        raise ValueError("Generated module exceeds size limit")
    tree = ast.parse(source)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    if [node.name for node in functions] != ["generate_signals", "run_strategy"]:
        raise ValueError("Expected generate_signals and run_strategy in that order")
    if len(tree.body) != 2:
        raise ValueError("Only two function definitions are allowed; no top-level execution or imports")
    if [arg.arg for arg in functions[0].args.args] != ["data", "fast", "slow"] or [arg.arg for arg in functions[1].args.args] != ["data", "config"]:
        raise ValueError("Function interfaces must match the fixed template")
    allowed = (ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign,
        ast.Expr, ast.Constant, ast.Name, ast.Load, ast.Store, ast.Subscript, ast.Attribute,
        ast.Call, ast.keyword, ast.Compare, ast.Gt, ast.GtE, ast.Lt, ast.LtE, ast.Eq,
        ast.NotEq, ast.BinOp, ast.BitAnd, ast.BitOr, ast.Add, ast.Sub, ast.Mult, ast.Div,
        ast.UnaryOp, ast.USub, ast.Invert, ast.BoolOp, ast.And, ast.Or)
    methods = {"rolling", "mean", "astype", "fillna", "copy", "where", "clip", "reindex"}
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            raise ValueError("Disallowed syntax: " + type(node).__name__)
        if isinstance(node, ast.FunctionDef) and (node.decorator_list or node.args.defaults or node.args.kw_defaults or node.args.vararg or node.args.kwarg or node.args.kwonlyargs or node.args.posonlyargs):
            raise ValueError("No decorators, defaults or variable argument lists")
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise ValueError("Private names are not allowed")
        if isinstance(node, ast.Attribute) and node.attr not in methods | {"index"}:
            raise ValueError("Disallowed attribute: " + node.attr)
        if isinstance(node, ast.Call):
            ok = isinstance(node.func, ast.Name) and node.func.id in {"generate_signals", "run_signal_backtest"}
            ok = ok or isinstance(node.func, ast.Attribute) and node.func.attr in methods
            if not ok:
                raise ValueError("Disallowed function call")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) > 2000:
            raise ValueError("String literal exceeds limit")
    return tree


def execute_strategy(source, data, config):
    tree = validate_source(source)
    namespace = {"__builtins__": {}, "int": int, "float": float, "bool": bool,
                 "run_signal_backtest": run_signal_backtest}
    exec(compile(tree, "<generated_strategy>", "exec"), namespace)
    signals = namespace["generate_signals"](data.copy(), config["fast"], config["slow"])
    expected = reference_signals(data, config["fast"], config["slow"])
    if not isinstance(signals, pd.Series) or not signals.index.equals(data.index):
        raise ValueError("Generated signals are not aligned with market data")
    if not signals.equals(expected):
        # Numeric dtype differences are harmless; the signal values must be identical.
        if signals.isna().any() or not (signals == expected).all():
            raise ValueError("Generated signals differ from the specified fixed SMA rule")
    for stop in (config["slow"] + 5, len(data) // 2, len(data) - 1):
        if stop < 1 or stop >= len(data):
            continue
        prefix = namespace["generate_signals"](data.iloc[:stop].copy(), config["fast"], config["slow"])
        if not prefix.equals(signals.iloc[:stop]):
            raise ValueError("Prefix causality check failed")
    result = namespace["run_strategy"](data.copy(), dict(config))
    expected_result = run_signal_backtest(data, expected, config)
    if result != expected_result:
        raise ValueError("Generated backtest differs from the trusted execution engine")
    return result, {"ast_passed": True, "fixed_rule_equal": True, "prefix_causality_passed": True,
                    "trusted_engine_equal": True}
