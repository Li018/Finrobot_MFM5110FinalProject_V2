"""Offline regression checks for complete and partial factor experiments."""
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "finrobot_autogen"))
from experiments import run_factor_ideation as runner


class FactorExperimentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.spec = {
            "name": "fixture", "model": "fixture", "repeats": 2, "idea_count": 2,
            "ideation_symbol": "000001", "evaluation_symbols": ["000001"],
            "data_start": "2020-10-01", "data_end": "2021-06-30", "adjust": "qfq",
            "ideation_window": {"start": "2021-01-01", "end": "2021-03-31"},
            "evaluation_window": {"start": "2021-04-01", "end": "2021-06-30"},
            "warmup_observations": 10,
            "base_config": {"fast": 3, "slow": 7, "initial_cash": 10000,
                "max_position": .8, "lot_size": 100, "commission": .0003,
                "min_commission": 5, "slippage": .0005, "annual_periods": 252,
                "risk_free_rate": 0},
            "design": "Test fixtures only", "limits": "Not market data",
        }
        data_patch = patch.object(runner, "DATA_ROOT", self.root / "data")
        data_patch.start()
        self.addCleanup(data_patch.stop)
        plot_patch = patch.object(runner, "plot_results")
        plot_patch.start()
        self.addCleanup(plot_patch.stop)
        dates = pd.bdate_range("2020-10-01", "2021-06-30")
        close = 10 + np.sin(np.arange(len(dates)) / 5)
        self.data = pd.DataFrame({"Open": close, "High": close + .2, "Low": close - .2,
            "Close": close, "Volume": 100000}, index=dates)
        self.data.index.name = "Date"
        folder = runner.dataset_folder(self.spec, "000001")
        folder.mkdir(parents=True)
        self.data.to_csv(folder / "prices.csv")
        (folder / "metadata.json").write_text(json.dumps({"clean_sha256":
            hashlib.sha256((folder / "prices.csv").read_bytes()).hexdigest()}), encoding="utf-8")
        self.out = self.root / "artifacts"
        self.run = runner.output_root(self.spec, runner.build_prompt(self.spec, self.data), self.out)
        self.run.mkdir(parents=True)
        (self.run / "inputs.json").write_text(json.dumps({"spec": self.spec}), encoding="utf-8")

    def write_attempt(self, number, constant=False):
        expressions = ["sma(close, 3) > sma(close, 7)", "ret(close, 3) > 0"]
        if constant:
            expressions[0] = "sma(close, 3) > 0"
        ideas = [{"name": f"idea_{i}", "hypothesis": "fixture", "signal": expression,
            "parameters": {}, "risk_notes": []} for i, expression in enumerate(expressions)]
        folder = self.run / f"ideation_{number:02d}"
        folder.mkdir(parents=True)
        (folder / "ideas.json").write_text(json.dumps({"ok": True, "returned_ideas": 2,
            "valid_ideas": 2, "ideas": ideas, "errors": []}), encoding="utf-8")

    def evaluate(self):
        with contextlib.redirect_stdout(io.StringIO()):
            ok = runner.evaluate(self.spec, self.out)
        return ok, json.loads((self.run / "summary.json").read_text(encoding="utf-8"))

    def test_complete_run_counts_duplicate_and_oos_cases(self):
        self.write_attempt(1)
        self.write_attempt(2)
        ok, summary = self.evaluate()
        self.assertTrue(ok)
        self.assertEqual(summary["successful_generations"], 2)
        self.assertEqual(summary["unique_valid_signals"], 2)
        self.assertEqual(summary["out_of_sample_comparisons"], 2)
        self.assertEqual(summary["duplicate_ideas"], 2)

    def test_missing_attempt_is_in_generation_denominator(self):
        self.write_attempt(1)
        ok, summary = self.evaluate()
        self.assertFalse(ok)
        self.assertEqual(summary["completed_generations"], 1)
        self.assertEqual(summary["generation_success_rate"], .5)
        self.assertEqual(summary["idea_delivery_rate"], .5)
        self.assertEqual(summary["missing_attempts"], ["ideation_02"])

    def test_no_attempts_write_failed_summary(self):
        ok, summary = self.evaluate()
        self.assertFalse(ok)
        self.assertEqual(summary["completed_generations"], 0)
        self.assertEqual(summary["generation_success_rate"], 0)
        self.assertIsNone(summary["out_of_sample_win_rate_vs_sma"])

    def test_saved_validation_is_rechecked(self):
        self.write_attempt(1, constant=True)
        self.write_attempt(2)
        ok, summary = self.evaluate()
        self.assertFalse(ok)
        self.assertEqual(summary["valid_ideas"], 3)
        self.assertEqual(summary["failed_attempts"], ["ideation_01"])

    def test_overlapping_windows_are_rejected(self):
        self.spec["evaluation_window"]["start"] = "2021-03-01"
        with self.assertRaisesRegex(ValueError, "after the ideation"):
            runner.evaluate(self.spec, self.out)

    def test_updated_prompt_replays_the_frozen_run(self):
        self.write_attempt(1)
        self.write_attempt(2)
        self.assertTrue(self.evaluate()[0])
        with patch.object(runner, "quant_factor_ideation_prompt", runner.quant_factor_ideation_prompt + "\nUpdated constraints"):
            self.assertTrue(self.evaluate()[0])


if __name__ == "__main__":
    unittest.main()
