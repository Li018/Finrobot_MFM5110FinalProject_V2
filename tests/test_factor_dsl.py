"""Offline checks for the safe factor DSL and the ideation validator."""
import importlib.util
import os
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
os.environ["DEBUG"] = "false"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


factor_dsl = load_module("factor_dsl_under_test",
                         ROOT / "finrobot_autogen/finrobot/functional/factor_dsl.py")
FactorDslError = factor_dsl.FactorDslError
parse_expression = factor_dsl.parse_expression
signal_from_expression = factor_dsl.signal_from_expression
validate_causality = factor_dsl.validate_causality


def fixture(rows=120):
    dates = pd.bdate_range("2024-01-01", periods=rows)
    close = 10 + np.sin(np.arange(rows) / 5) + np.arange(rows) / 500
    return pd.DataFrame({
        "Open": close,
        "High": close + 0.2,
        "Low": close - 0.2,
        "Close": close,
        "Volume": 100000 + np.arange(rows) * 100,
    }, index=dates)


class FactorDslTests(unittest.TestCase):
    def setUp(self):
        self.data = fixture()

    def test_momentum_signal_is_binary_and_causal(self):
        expression = "sma(close, 3) > sma(close, 7)"
        signal = signal_from_expression(expression, self.data)
        self.assertTrue(set(signal.unique()).issubset({0, 1}))
        self.assertTrue((signal.iloc[:6] == 0).all())
        validate_causality(expression, self.data, (40, 80))

    def test_boolean_and_composition(self):
        expression = "ret(close, 5) > 0 and volume > sma(volume, 5)"
        signal = signal_from_expression(expression, self.data)
        self.assertEqual(len(signal), len(self.data))
        self.assertIn(1, signal.values)

    def test_zscore_and_rank_are_trailing(self):
        expression = "zscore(close, 10) < 1 and rank(close, 10) > 0.3"
        signal = signal_from_expression(expression, self.data)
        self.assertTrue(set(signal.unique()).issubset({0, 1}))

    def test_rejects_plain_series(self):
        with self.assertRaises(FactorDslError):
            parse_expression("close")

    def test_rejects_attributes_and_imports(self):
        for expression in ("close.shift(-1) > 0", "__import__('os') is not None"):
            with self.assertRaises(FactorDslError):
                parse_expression(expression)

    def test_rejects_unknown_name_and_window(self):
        for expression in ("sma(close, 300) > 0", "future_return > 0", "sma(close, 2.5) > 0"):
            with self.assertRaises(FactorDslError):
                parse_expression(expression)

    def test_rejects_non_comparison_arithmetic(self):
        with self.assertRaises(FactorDslError):
            parse_expression("sma(close, 5) - sma(close, 20)")

    def test_rejects_multiply_by_zero(self):
        with self.assertRaises(FactorDslError):
            parse_expression("rank(close, 60) * 0 + close > 0")

    def test_division_by_zero_is_not_inf(self):
        data = self.data.copy()
        data["Volume"] = 0.0
        expression = "close / volume > 1"
        signal = signal_from_expression(expression, data)
        self.assertTrue(set(signal.unique()).issubset({0, 1}))

    def test_where_and_scalar_functions(self):
        expression = "where(close > sma(close, 5), abs(ret(close, 3)), 0) > 0.01"
        signal = signal_from_expression(expression, self.data)
        self.assertEqual(len(signal), len(self.data))

    def test_scalar_not_keeps_boolean_meaning(self):
        for value, expected in (("True", 0), ("False", 1)):
            with self.subTest(value=value):
                signal = signal_from_expression(f"where(not {value}, close, -close) > 0", self.data)
                self.assertTrue((signal == expected).all())

    def test_series_not_inverts_each_comparison(self):
        expression = "not (close > 10) and close > 0"
        expected = ((self.data.Close <= 10) & (self.data.Close > 0)).astype(int)
        pd.testing.assert_series_equal(signal_from_expression(expression, self.data), expected)

    def test_rejects_power_before_evaluation(self):
        with self.assertRaises(FactorDslError):
            parse_expression("close > (2 ** (2 ** 40))")

    def test_rejects_unbounded_numeric_literals(self):
        for expression in ("close > 1e309", "close > 1000000000000000000000000000000"):
            with self.subTest(expression=expression), self.assertRaises(FactorDslError):
                parse_expression(expression)

    def test_rejects_excessive_expression_complexity(self):
        deep = "close"
        for _ in range(40):
            deep = f"abs({deep})"
        for expression in (deep + " > 0", " or ".join(["close > 0"] * 100)):
            with self.subTest(expression=expression), self.assertRaises(FactorDslError):
                parse_expression(expression)


class IdeationValidationTests(unittest.TestCase):
    def test_validate_ideas_keeps_valid_and_reports_invalid(self):
        validate_ideas = factor_dsl.validate_ideas
        frame = fixture()
        ideas = [
            {"name": "trend", "hypothesis": "trend persists", "signal": "sma(close, 3) > sma(close, 7)",
             "parameters": {"fast": 3, "slow": 7}, "risk_notes": ["single stock"]},
            {"name": "bad", "hypothesis": "broken", "signal": "close.shift(-1) > 0",
             "parameters": {}, "risk_notes": []},
        ]
        valid, errors = validate_ideas(ideas, frame, 20)
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0]["name"], "trend")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["name"], "bad")

    def test_constant_signal_is_rejected(self):
        validate = factor_dsl.validate_ideas
        frame = fixture()
        ideas = [{"name": "always", "hypothesis": "trivial", "signal": "close > 0",
                  "parameters": {}, "risk_notes": []}]
        valid, errors = validate(ideas, frame, 20)
        self.assertEqual(valid, [])
        self.assertIn("constant", errors[0]["error"])

    def test_constant_after_warmup_is_rejected(self):
        idea = {"name": "always", "hypothesis": "trivial", "signal": "sma(close, 20) > 0",
                "parameters": {}, "risk_notes": []}
        valid, errors = factor_dsl.validate_ideas([idea], fixture(), 30)
        self.assertEqual(valid, [])
        self.assertIn("constant", errors[0]["error"])


if __name__ == "__main__":
    unittest.main()
