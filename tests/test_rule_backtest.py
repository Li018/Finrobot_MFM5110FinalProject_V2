"""Offline rule validation and real-engine backtests on synthetic bars."""
import json
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from finrobot.functional import momentum_backtest as tools


def condition(window=20):
    return {"indicator": "momentum", "window": window, "operator": ">", "value": 0}


class RuleBacktestTests(unittest.TestCase):
    def setUp(self):
        dates = pd.bdate_range("2024-01-01", periods=100)
        close = 10 + np.arange(100) * .05 + np.sin(np.arange(100) / 5)
        self.bars = pd.DataFrame({"Open": close, "High": close + .1, "Low": close - .1,
                                 "Close": close, "Volume": 100000 + np.arange(100)}, index=dates)
        self.rule = {"signal": {"all": [condition()]}}

    def test_nested_condition_limit_is_global(self):
        rule = {"signal": {"all": [{"all": [condition()] * 12}] * 3}}
        with self.assertRaisesRegex(ValueError, "12"):
            tools.validate_strategy_rule(rule)

    def test_twelve_nested_conditions_are_allowed(self):
        rule = {"signal": {"any": [{"all": [condition()] * 6}] * 2}}
        self.assertEqual(tools.validate_strategy_rule(rule)["execution"], "next_open")

    def test_excessive_nesting_is_rejected(self):
        signal = condition()
        for _ in range(20):
            signal = {"all": [signal]}
        with self.assertRaises(ValueError):
            tools.validate_strategy_rule({"signal": signal})

    def test_invalid_windows_fields_and_execution_are_rejected(self):
        for value in (-1, True, 253):
            with self.subTest(window=value), self.assertRaises(ValueError):
                tools.validate_strategy_rule({"signal": {"all": [condition(value)]}})
        with self.assertRaises(ValueError):
            tools.validate_strategy_rule({**self.rule, "execution": "same_close"})
        with self.assertRaises(ValueError):
            tools.validate_strategy_rule({"signal": {"all": [{**condition(), "import": "os"}]}})

    def test_signals_are_causal_and_preserve_warmup(self):
        signals = tools.build_strategy_signals(self.bars, self.rule)
        self.assertTrue(signals.iloc[:20].isna().all())
        pd.testing.assert_series_equal(signals.iloc[:70], tools.build_strategy_signals(self.bars.iloc[:70], self.rule))

    def test_rule_backtest_uses_costs_and_passes_ledger_audit(self):
        start, end = str(self.bars.index[30].date()), str(self.bars.index[80].date())
        with patch.object(tools.CNStockUtils, "get_stock_data", return_value=self.bars.loc[:end]), \
                patch.object(tools, "_csi300_open", side_effect=OSError("offline fixture")):
            result = json.loads(tools.backtest_a_share_strategy("000001", start, end, json.dumps(self.rule)))
        self.assertTrue(result["audit_ok"])
        self.assertEqual(result["cost_assumptions"]["max_position"], .8)
        self.assertGreaterEqual(result["strategy"]["min_cash"], 0)
        self.assertEqual(result["evaluation_last_close"], end)
        self.assertFalse(result["csi300"]["available"])

    def test_provider_bars_after_end_are_excluded(self):
        start, end = str(self.bars.index[30].date()), str(self.bars.index[80].date())
        with patch.object(tools.CNStockUtils, "get_stock_data", return_value=self.bars), \
                patch.object(tools, "_csi300_open", side_effect=OSError("offline fixture")):
            result = json.loads(tools.backtest_a_share_strategy("000001", start, end, json.dumps(self.rule)))
        self.assertEqual(result["evaluation_last_close"], end)

    def test_invalid_json_never_fetches_data(self):
        with patch.object(tools.CNStockUtils, "get_stock_data") as fetch:
            with self.assertRaises(ValueError):
                tools.backtest_a_share_strategy("000001", "2024-02-01", "2024-03-01", "not json")
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
