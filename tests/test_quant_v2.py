"""Offline checks. Artificial prices here are fixtures, never experiment observations."""
import json
import os
import unittest
import runpy
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
os.environ["DEBUG"] = "false"
os.environ["NLTK_DATA"] = str(ROOT / ".cache/nltk_data")
from finrobot.functional.quantitative import calculate_metrics, reference_signals, run_signal_backtest, BackTraderUtils
from finrobot.functional.quant_tools import clean_market_data, load_config, load_data
from finrobot.functional.strategy_codegen import execute_strategy, validate_source
from finrobot.data_source.akshare_utils import _standardize_price, AkShareUtils


VALID_SOURCE = '''def generate_signals(data, fast, slow):
    fast_ma = data["Close"].rolling(fast).mean()
    slow_ma = data["Close"].rolling(slow).mean()
    return (fast_ma > slow_ma).astype(int)

def run_strategy(data, config):
    signals = generate_signals(data, config["fast"], config["slow"])
    return run_signal_backtest(data, signals, config)
'''


def fixture():
    dates = pd.bdate_range("2024-01-01", periods=100)
    close = 10 + np.sin(np.arange(100) / 5)
    return pd.DataFrame({"Open": close, "High": close + 0.2, "Low": close - 0.2,
                         "Close": close, "Volume": 100000.0}, index=dates)


class QuantTests(unittest.TestCase):
    def setUp(self):
        self.data = fixture()
        self.config = {**load_config(), "fast": 3, "slow": 7, "start_date": "2024-01-01", "end_date": "2024-12-31"}

    def test_metrics_known_equity(self):
        values = [100, 120, 90, 108]
        metrics = calculate_metrics(values, 100)
        self.assertAlmostEqual(metrics["total_return"], 0.08)
        self.assertAlmostEqual(metrics["max_drawdown"], 0.25)
        self.assertAlmostEqual(metrics["annual_return"], 1.08 ** (252 / 3) - 1)

    def test_flat_sharpe_is_undefined(self):
        self.assertIsNone(calculate_metrics([100, 100, 100], 100)["sharpe"])

    def test_signal_warmup_and_prefix(self):
        signals = reference_signals(self.data, 3, 7)
        self.assertTrue((signals.iloc[:6] == 0).all())
        changed = self.data.copy()
        changed.loc[changed.index[50]:, "Close"] *= 3
        self.assertTrue(signals.iloc[:50].equals(reference_signals(changed, 3, 7).iloc[:50]))

    def test_next_open_and_entry_budget(self):
        signals = pd.Series(0, index=self.data.index)
        signals.iloc[10:30] = 1
        result = run_signal_backtest(self.data, signals, self.config)
        buy, sell = result["trades"]
        self.assertEqual(buy["date"], str(self.data.index[11].date()))
        self.assertEqual(sell["date"], str(self.data.index[31].date()))
        self.assertAlmostEqual(buy["price"], self.data.Open.iloc[11] * 1.0005)
        self.assertAlmostEqual(sell["price"], self.data.Open.iloc[31] * 0.9995)
        self.assertLessEqual(buy["size"] * buy["price"] + buy["commission"], buy["entry_budget"])
        self.assertEqual(buy["size"] % 100, 0)
        self.assertGreaterEqual(result["metrics"]["min_cash"], 0)

    def test_independent_cash_ledger(self):
        result = run_signal_backtest(self.data, reference_signals(self.data, 3, 7), self.config)
        cash, shares = self.config["initial_cash"], 0
        for trade in result["trades"]:
            cash -= trade["size"] * trade["price"] + trade["commission"]
            shares += trade["size"]
        self.assertAlmostEqual(cash + shares * self.data.Close.iloc[-1], result["metrics"]["final_equity"])

    def test_unauthorized_signal_rejected(self):
        with self.assertRaises(ValueError):
            run_signal_backtest(self.data, pd.Series(-1, index=self.data.index), self.config)

    def test_too_little_cash_has_no_trades(self):
        result = run_signal_backtest(self.data, pd.Series(1, index=self.data.index), {**self.config, "initial_cash": 50})
        self.assertEqual(result["trades"], [])
        self.assertTrue(result["risk_events"])

    def test_cleaning_no_imputed_bars(self):
        dirty = self.data.iloc[:10].copy()
        dirty.index.name = "Date"
        dirty.iloc[1, 0] = np.nan
        dirty = pd.concat([dirty, dirty.iloc[[2]]]).iloc[::-1]
        clean, record = clean_market_data(dirty, self.config)
        self.assertEqual(len(clean), 9)
        self.assertEqual(record["duplicate_dates_removed"], 1)
        self.assertEqual(record["missing_rows_removed"], 1)
        self.assertEqual(clean.Volume.iloc[0], 10000000)

    def test_bad_ohlc_rejected(self):
        dirty = self.data.copy()
        dirty.iloc[0, dirty.columns.get_loc("High")] = 1
        with self.assertRaises(ValueError):
            clean_market_data(dirty, self.config)

    def test_tencent_columns_standardized(self):
        raw = pd.DataFrame({"date": ["2024-01-02"], "open": [1], "high": [2], "low": [0.5], "close": [1.5], "volume": [10]})
        out = _standardize_price(raw)
        self.assertTrue({"Open", "High", "Low", "Close", "Volume"}.issubset(out.columns))
        self.assertEqual(out.index.name, "Date")

    def test_tencent_000_volume_regression(self):
        class FakeAk:
            __version__ = "1.20.7"
            @staticmethod
            def stock_zh_a_hist(**kwargs):
                raise ConnectionError("test provider fallback")
            @staticmethod
            def stock_zh_a_hist_tx(**kwargs):
                return pd.DataFrame({"date": ["2024-01-02"], "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.0], "volume": [123.0], "turnover": [0.01]})
        with patch("finrobot.data_source.akshare_utils._require_ak", return_value=FakeAk()):
            out = AkShareUtils.get_stock_data("000001", "2024-01-01", "2024-01-03")
        self.assertEqual(out.Volume.iloc[0], 123.0)
        self.assertEqual(out.Turnover.iloc[0], 1.0)
        self.assertEqual(out.attrs["source"], "akshare_tencent")

    def test_generated_code_matches_rule(self):
        result, checks = execute_strategy(VALID_SOURCE, self.data, self.config)
        self.assertTrue(all(checks.values()))
        self.assertEqual(result, run_signal_backtest(self.data, reference_signals(self.data, 3, 7), self.config))

    def test_generated_index_alignment_allowed(self):
        code = VALID_SOURCE.replace('return (fast_ma > slow_ma).astype(int)', 'return (fast_ma > slow_ma).astype(int).reindex(data.index).fillna(0).astype(int)')
        result, checks = execute_strategy(code, self.data, self.config)
        self.assertTrue(checks["trusted_engine_equal"])

    def test_future_shift_rejected(self):
        bad = VALID_SOURCE.replace('.rolling(fast).mean()', '.shift(-1).rolling(fast).mean()')
        with self.assertRaises(ValueError):
            validate_source(bad)

    def test_external_actions_rejected(self):
        for source in ("import os\n" + VALID_SOURCE, VALID_SOURCE.replace('return (fast_ma > slow_ma).astype(int)', 'return open("secret.txt")'), VALID_SOURCE.replace('data["Close"]', 'data.__class__')):
            with self.assertRaises(ValueError):
                validate_source(source)

    def test_incorrect_strategy_rejected(self):
        with self.assertRaises(ValueError):
            execute_strategy(VALID_SOURCE.replace('fast_ma > slow_ma', 'fast_ma < slow_ma'), self.data, self.config)

    def test_actual_snapshot_and_legacy_entry(self):
        data = load_data()
        self.assertGreater(len(data), 100)
        text = BackTraderUtils.back_test("000001", "2021-01-01", "2025-12-31", "SMA_CrossOver", data_path=str(ROOT / "data/quant_v2/prices.csv"), config_path=str(ROOT / "finrobot_autogen/experiments/quant_v2_config.json"))
        result = json.loads(text)
        self.assertGreater(result["metrics"]["executed_orders"], 0)

    def test_warmup_excluded_from_performance_and_trades(self):
        cfg = {**self.config, "evaluation_start": self.data.index[40].date().isoformat()}
        result = run_signal_backtest(self.data, pd.Series(1, index=self.data.index), cfg)
        self.assertEqual(len(result["equity"]), 60)
        self.assertEqual(result["equity"][0]["Date"], cfg["evaluation_start"])
        self.assertEqual(result["equity"][0]["equity"], cfg["initial_cash"])
        self.assertEqual(result["trades"][0]["date"], self.data.index[41].date().isoformat())
        self.assertEqual(result["trades"][0]["signal_date"], cfg["evaluation_start"])

    def test_short_evaluation_rejected(self):
        cfg = {**self.config, "evaluation_start": self.data.index[-1].date().isoformat()}
        with self.assertRaises(ValueError):
            run_signal_backtest(self.data, pd.Series(1, index=self.data.index), cfg)

    def test_window_has_only_past_context(self):
        functions = runpy.run_path(str(ROOT / "finrobot_autogen/experiments/run_generalization.py"))
        window = {"start": str(self.data.index[40].date()), "end": str(self.data.index[60].date())}
        frame, count, evaluation = functions["slice_window"](self.data, window, 7)
        self.assertEqual(count, 7)
        self.assertEqual(len(evaluation), 21)
        self.assertEqual(len(frame), 28)
        self.assertEqual(frame.index[-1], self.data.index[60])
        self.assertEqual(frame.index[0], self.data.index[33])

    def test_generalization_windows_must_not_overlap(self):
        functions = runpy.run_path(str(ROOT / "finrobot_autogen/experiments/run_generalization.py"))
        spec = json.loads((ROOT / "finrobot_autogen/experiments/generalization_config.json").read_text(encoding="utf-8"))
        spec["windows"][1]["start"] = "2022-01-01"
        with self.assertRaises(ValueError):
            functions["validate_config"](spec)

    def test_window_rejects_insufficient_warmup(self):
        functions = runpy.run_path(str(ROOT / "finrobot_autogen/experiments/run_generalization.py"))
        window = {"start": str(self.data.index[3].date()), "end": str(self.data.index[60].date())}
        with self.assertRaises(ValueError):
            functions["slice_window"](self.data, window, 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
