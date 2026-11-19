"""Offline checks for the multi-case stability runner."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "finrobot_autogen"))

from experiments import run_stability


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def spec():
    return {
        "name": "fixture_stability",
        "model": "deepseek-flash",
        "repeats": 3,
        "cases": [{"name": "case_a", "symbol": "000001", "start": "2024-01-01",
                   "end": "2024-12-31", "fast": 3, "slow": 7}],
        "design": "fixture",
        "limits": "fixture",
    }


class StabilityTests(unittest.TestCase):
    def test_rate_and_latency(self):
        rows = [{"x": True}, {"x": False}, {"x": None}, {}]
        self.assertEqual(run_stability._rate(rows, "x"), 0.25)
        self.assertEqual(run_stability._rate(rows, "x", evaluated_only=True), 0.5)
        stats = run_stability._latency([1.0, 3.0])
        self.assertEqual(stats["median"], 2.0)
        self.assertEqual(stats["count"], 2)

    def test_evaluate_counts_success_and_failure(self):
        spec_value = spec()
        with tempfile.TemporaryDirectory() as temp:
            out = run_stability.output_root(spec_value, Path(temp))
            case_dir = out / "case_a"
            write(case_dir / "generation_01" / "response.json", {"code": "x"})
            write(case_dir / "generation_01" / "execution.json", {
                "ok": True,
                "checks": {"ast_passed": True, "fixed_rule_equal": True,
                           "prefix_causality_passed": True, "trusted_engine_equal": True},
                "result": {"metrics": {"total_return": 0.1, "max_drawdown": 0.2}},
            })
            write(case_dir / "generation_01" / "timing.json", {"seconds": 2.0})
            write(case_dir / "generation_02" / "execution.json", {
                "ok": False, "error_type": "JSONDecodeError", "error": "bad json"})
            self.assertFalse(run_stability.evaluate(spec_value, Path(temp)))
            summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["attempts"], 3)
            self.assertEqual(summary["passed"], 1)
            self.assertEqual(summary["pass_rate"], 0.3333)
            self.assertEqual(summary["json_valid_rate"], 0.3333)
            for key in ("rule_consistency_rate", "prefix_causality_rate", "trusted_engine_equality_rate"):
                self.assertEqual(summary[key], 0.3333)
                self.assertEqual(summary["conditional_check_rates"][key], 1.0)
            self.assertEqual(summary["failure_taxonomy"],
                             {"json_decode_or_schema": 1, "missing_attempt": 1})
            self.assertEqual(summary["per_case"]["case_a"]["passed"], 1)
            self.assertEqual(summary["per_case"]["case_a"]["attempts"], 3)
            self.assertEqual(summary["latency_seconds"]["median"], 2.0)

    def test_missing_attempts_cannot_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            spec_value = spec()
            self.assertFalse(run_stability.evaluate(spec_value, Path(temp)))
            out = run_stability.output_root(spec_value, Path(temp))
            summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["attempts"], 3)
            self.assertEqual(summary["rule_consistency_rate"], 0.0)
            self.assertIsNone(summary["conditional_check_rates"]["rule_consistency_rate"])

    def test_prepare_case_rejects_changed_config(self):
        dates = pd.bdate_range("2024-01-01", periods=40)
        close = 10 + np.sin(np.arange(40) / 4)
        frame = pd.DataFrame({"Open": close, "High": close + 0.2, "Low": close - 0.2,
                              "Close": close, "Volume": 100000.0}, index=dates)
        frame.index.name = "Date"
        cfg = {"symbol": "000001", "fast": 3, "slow": 7, "evaluation_start": "2024-01-15"}
        case = {"name": "case", "symbol": "000001", "start": "2024-01-01",
                "end": "2024-12-31", "fast": 3, "slow": 7}
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "case"
            run_stability.prepare_case(folder, case, frame, frame, cfg, spec())
            changed = {**cfg, "fast": 5}
            with self.assertRaises(ValueError):
                run_stability.prepare_case(folder, case, frame, frame, changed, spec())


if __name__ == "__main__":
    unittest.main()
