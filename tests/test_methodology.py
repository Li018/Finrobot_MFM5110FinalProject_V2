"""Offline checks for the methodology aggregation layer."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "methodology_under_test", ROOT / "finrobot_autogen/finrobot/functional/methodology.py")
methodology = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(methodology)
build_metrics = methodology.build_metrics
render_markdown = methodology.render_markdown


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class MethodologyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        artifacts = self.root / "artifacts"
        quant = artifacts / "quant_v2"
        write(quant / "evaluation.json", {
            "attempts": [
                {"attempt": "generation_01", "ok": True, "initial_execution_ok": False,
                 "initial_json_valid": True, "without_model_repair_ok": True, "model_repairs": 0,
                 "initial_error": "Disallowed attribute: index",
                 "checks": {"fixed_rule_equal": True, "prefix_causality_passed": True,
                            "trusted_engine_equal": True}},
                {"attempt": "generation_02", "ok": True, "initial_execution_ok": True,
                 "initial_json_valid": True, "without_model_repair_ok": True, "model_repairs": 0,
                 "checks": {"fixed_rule_equal": True, "prefix_causality_passed": True,
                            "trusted_engine_equal": True}},
                {"attempt": "generation_03", "ok": True, "initial_execution_ok": False,
                 "initial_json_valid": False, "without_model_repair_ok": False, "model_repairs": 1,
                 "initial_error": "Expecting ',' delimiter",
                 "checks": {"fixed_rule_equal": True, "prefix_causality_passed": True,
                            "trusted_engine_equal": True}},
            ],
            "initial_json_valid_rate": 0.6667,
            "initial_execution_success_rate": 0.3333,
            "without_model_repair_success_rate": 0.6667,
            "validated_success_rate": 1.0,
            "model_repair_requests": 1,
            "all_audits_passed": True,
        })
        write(quant / "generation_01/timing.json", {"seconds": 10.0})
        write(quant / "generation_02/timing.json", {"seconds": 12.0})
        write(quant / "generation_03/timing.json", {"seconds": 8.0})
        write(quant / "generation_01/usage.json", {"deepseek": {"prompt_tokens": 100,
              "completion_tokens": 50, "total_tokens": 150}})
        write(quant / "agent_tool_validation.json", {"ok": True, "tool_calls": 4,
              "numeric_checks": {"a": True, "b": True, "c": False, "d": True}})
        write(quant / "dialogue_online_validation.json", {"turns": [
            {"turn": 1, "ok": True, "tool_calls": ["prepare", "compare"],
             "answer": {"experiment_id": "aaa"}},
            {"turn": 2, "ok": True, "tool_calls": ["execute", "risk"],
             "answer": {"experiment_id": "aaa"}},
            {"turn": 3, "ok": True, "tool_calls": ["prepare", "compare"],
             "answer": {"experiment_id": "bbb"}},
        ]})
        run = "quant_generalization/run_abc"
        write(artifacts / "quant_generalization/latest.json", {"run": run})
        write(self.root / run / "summary.json", {"cases": 9, "symbols": ["000001"],
              "windows": [{"name": "w"}], "frozen_strategies": 3, "replays": 27,
              "passed_replays": 27, "new_llm_requests": 0, "investment_alpha_tested": False})
        stability_run = "artifacts/stability/run_stab"
        write(artifacts / "stability/latest.json", {"run": stability_run})
        write(self.root / stability_run / "summary.json", {
            "cases": 3, "repeats_per_case": 5, "attempts": 15, "passed": 15,
            "pass_rate": 1.0, "json_valid_rate": 1.0, "rule_consistency_rate": 1.0,
            "prefix_causality_rate": 1.0, "trusted_engine_equality_rate": 1.0,
            "latency_seconds": {"count": 15, "mean": 7.9, "median": 7.5, "min": 6.7, "max": 11.2},
            "failure_taxonomy": {}, "per_case": {"c": {"symbol": "000001", "fast": 20,
                                                     "slow": 60, "attempts": 5, "passed": 5,
                                                     "pass_rate": 1.0, "json_valid_rate": 1.0}}})
        factor_run = "artifacts/factor_ideation/run_factor"
        write(artifacts / "factor_ideation/latest.json", {"run": factor_run})
        write(self.root / factor_run / "summary.json", {
            "repeats": 3, "returned_ideas": 12, "valid_ideas": 12, "idea_validation_rate": 1.0,
            "unique_valid_signals": 9, "duplicate_rate": 0.25,
            "out_of_sample_win_rate_vs_buy_hold": 0.0, "out_of_sample_win_rate_vs_sma": 0.4815,
            "ideation": {"symbol": "000001", "window": {"start": "2021-01-01", "end": "2023-12-31"}},
            "evaluation": {"symbols": ["000001"], "window": {"start": "2024-01-01", "end": "2025-12-31"},
                           "cases_per_idea": 1},
            "per_idea": [{"name": "momentum", "signal": "sma(close, 5) > sma(close, 20)",
                          "generated_times": 3, "mean_out_of_sample_return": 0.1,
                          "beats_buy_hold_cases": 0, "beats_sma_cases": 2}]})
        write(self.root / "data/quant_v2/metadata.json", {"symbol": "000001", "output_rows": 1212,
              "actual_start": "2021-01-04", "actual_end": "2025-12-31", "adjust": "qfq",
              "provider": {"source": "akshare_tencent"}, "duplicate_dates_removed": 0,
              "missing_rows_removed": 0, "missing_dates": "none", "volume_conversion": "x100"})
        write(self.root / "data/quant_generalization/000001_x/metadata.json", {"symbol": "000001",
              "output_rows": 1455, "provider": {"source": "akshare_tencent"}})

    def tearDown(self):
        self.temp.cleanup()

    def test_build_metrics(self):
        metrics = build_metrics(self.root / "artifacts")
        generation = metrics["generation"]
        self.assertEqual(generation["attempts"], 3)
        self.assertEqual(generation["initial_json_valid_rate"], 0.6667)
        self.assertEqual(generation["failure_taxonomy"],
                         {"json_decode_or_schema": 1, "validator_rejection_investigated": 1})
        self.assertEqual(generation["latency_seconds"]["median"], 10.0)
        self.assertEqual(generation["tokens"]["total_tokens"], 150)
        self.assertEqual(metrics["numeric_fidelity"]["accuracy"], 0.75)
        self.assertTrue(metrics["dialogue"]["cross_turn_experiment_reuse"])
        self.assertTrue(metrics["dialogue"]["parameter_switch_detected"])
        self.assertEqual(metrics["generalization"]["cases"], 9)
        self.assertEqual(metrics["stability"]["passed"], 15)
        self.assertEqual(metrics["factor_ideation"]["valid_ideas"], 12)
        self.assertEqual(metrics["data"]["fixed_case"]["rows"], 1212)

    def test_markdown_keeps_honest_labels(self):
        text = render_markdown(build_metrics(self.root / "artifacts"))
        self.assertIn("首次 JSON 合格率", text)
        self.assertIn("修正后最终成功率", text)
        self.assertIn("不是 45 次独立生成", text)
        self.assertIn("不能据此宣称 LLM 产生 alpha", text)
        self.assertIn("多案例生成稳定性", text)
        self.assertIn("LLM 因子思路与样本外回放", text)


if __name__ == "__main__":
    unittest.main()
