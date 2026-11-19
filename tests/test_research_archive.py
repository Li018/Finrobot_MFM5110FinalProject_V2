"""Research snapshots must retain provenance and refuse misleading numeric deltas."""

from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from finrobot.agents.memory import ResearchMemory
from finrobot.agents.research_archive import MAX_MODEL_CHARS, ResearchArchive


ROOT = Path(__file__).resolve().parents[1]


def chat(payload, tool="compare_quant_strategies"):
    return {"role": "Quant_Analyst", "history": [
        {"role": "assistant", "tool_calls": [{"id": "tool_1", "function": {"name": tool, "arguments": "{}"}}]},
        {"role": "tool", "tool_responses": [{"role": "tool", "tool_call_id": "tool_1", "content": json.dumps(payload)}]},
    ]}


class ResearchArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.db = self.folder / "memory.sqlite3"
        self.memory = ResearchMemory(self.db, session_id="first")
        self.archive = ResearchArchive(self.db)
        self.config = json.loads((ROOT / "finrobot_autogen/experiments/quant_v2_config.json").read_text())
        self.config["symbol"] = "000651"

    def payload(self, value=0.1, **overrides):
        return {"ok": True, "experiment_id": "a1b2c3d4e5f60718", "config": self.config.copy(),
                "data_sha256": "d" * 64, "metrics": {"sma": {"total_return": value, "final_equity": 100000 * (1 + value),
                "max_drawdown": 0.1, "sharpe": 1.2, "executed_orders": 4}},
                "plot_path": "artifacts/quant_dialogue/a1b2c3d4e5f60718/comparison.png", **overrides}

    def save(self, payload=None, *, question="回测000651", answer="完成回测。", chats=None, memory=None):
        source = memory or self.memory
        turn_id = source.save_turn(question, answer, ["Quant_Analyst"],
            chats if chats is not None else [chat(self.payload() if payload is None else payload)])
        return self.archive.archive_turn(turn_id)

    def test_snapshot_is_idempotent_immutable_and_survives_a_new_process(self):
        first = self.save(answer="原始结论\n完整保留")
        self.assertEqual(self.archive.archive_turn(first["source_turn_id"]), first)
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE turns SET answer = ? WHERE id = ?", ("源轮次后来被修改", first["source_turn_id"]))
        self.assertEqual(self.archive.get(first["id"])["answer"], "原始结论\n完整保留")
        command = "import json,sys; from finrobot.agents.research_archive import ResearchArchive; print(json.dumps(ResearchArchive(sys.argv[1]).get(int(sys.argv[2])),ensure_ascii=False))"
        result = subprocess.run([sys.executable, "-c", command, str(self.db), str(first["id"])], check=True,
            env={**os.environ, "PYTHONPATH": str(ROOT / "finrobot_autogen")}, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout), first)

    def test_only_completed_existing_turns_are_archived(self):
        for status in ("failed", "partial"):
            turn_id = self.memory.save_turn("000651", "未完成", ["Quant_Analyst"], [], status=status)
            with self.assertRaisesRegex(ValueError, "completed"):
                self.archive.archive_turn(turn_id)
        with self.assertRaisesRegex(ValueError, "not found"):
            self.archive.archive_turn(10000)
        with self.assertRaises(ValueError):
            self.archive.archive_turn(True)
        with self.assertRaisesRegex(ValueError, "same database"):
            ResearchArchive(self.folder / "empty.sqlite3").archive_turn(1)
        self.assertEqual(self.archive.find()["reports"], [])

    def test_requested_and_verified_symbols_are_distinct_and_searchable(self):
        item = self.save(question="分析sh600519与$AAPL", answer="口头提到了TSLA和600031")
        self.assertEqual(item["symbols"], ["000651"])
        self.assertEqual(item["requested_symbols"], ["600519", "AAPL"])
        for code in ("SZ000651", "600519.SH", "aapl"):
            self.assertEqual(self.archive.find(symbol=code)["reports"][0]["id"], item["id"])
        self.assertEqual(self.archive.find(symbol="TSLA")["reports"], [])
        self.assertEqual(self.archive.find(symbol="600031")["reports"], [])
        plain = self.save(question="为000001生成PDF", answer="PDF已生成", chats=[])
        self.assertEqual(plain["symbols"], [])
        self.assertEqual(self.archive.find(symbol="000001")["reports"][0]["id"], plain["id"])

    def test_query_is_literal_and_history_crosses_sessions_explicitly(self):
        self.save(question="报告标记 %_'; DROP TABLE turns; --")
        other = ResearchMemory(self.db, session_id="second")
        latest = self.save(memory=other, question="第二个000651会话")
        found = self.archive.find(symbol="000651", limit=1)
        self.assertTrue(found["has_more"])
        self.assertEqual(found["reports"][0]["id"], latest["id"])
        self.assertEqual(len(self.archive.find(query="%_'; DROP TABLE turns; --")["reports"]), 1)
        self.assertEqual(self.archive.find(query="not present %_")["reports"], [])
        self.assertIsNone(self.archive.get(10000))
        for value in (True, 0, 101):
            with self.assertRaises(ValueError):
                self.archive.find(limit=value)
        with self.assertRaises(ValueError):
            self.archive.compare(1, 10000)

    def test_numeric_changes_have_explicit_units_and_prose_diff(self):
        first = self.save(answer="第一版：暂时观察")
        second = self.save(self.payload(0.2), answer="第二版：补充风险说明")
        comparison = self.archive.compare(first["id"], second["id"])
        self.assertIn("-第一版：暂时观察", comparison["answer_diff"])
        self.assertIn("+第二版：补充风险说明", comparison["answer_diff"])
        metrics = {item["metric"]: item for item in comparison["metrics"]}
        self.assertTrue(all(item["comparable"] for item in metrics.values()))
        self.assertAlmostEqual(metrics["total_return"]["delta"], 10.0)
        self.assertEqual(metrics["total_return"]["delta_unit"], "percentage_points")
        self.assertAlmostEqual(metrics["final_equity"]["delta"], 10000.0)
        self.assertEqual(metrics["final_equity"]["unit"], "account_cash_unit")
        self.assertEqual(metrics["executed_orders"]["delta"], 0)

    def test_changed_backtest_scope_never_produces_a_numeric_delta(self):
        first = self.save()
        for field, value in (("start_date", "2022-01-01"), ("end_date", "2024-12-31"),
                             ("fast", 10), ("initial_cash", 200000), ("commission", 0.001),
                             ("adjust", "hfq"), ("symbol", "600519")):
            with self.subTest(field=field):
                payload = self.payload(0.2)
                payload["config"][field] = value
                second = self.save(payload)
                comparison = self.archive.compare(first["id"], second["id"])
                self.assertTrue(comparison["scope_changes"])
                self.assertTrue(all(not row["comparable"] and row["delta"] is None for row in comparison["metrics"]))
        changed = self.save(self.payload(0.2, data_sha256="e" * 64))
        self.assertTrue(all(row["delta"] is None for row in self.archive.compare(first["id"], changed["id"])["metrics"]))

    def test_distinct_ids_with_verified_identical_scope_can_be_compared(self):
        first = self.save()
        second = self.save(self.payload(0.2, experiment_id="different-identifier"))
        comparison = self.archive.compare(first["id"], second["id"])
        self.assertTrue(all(row["comparable"] for row in comparison["metrics"]))
        self.assertTrue(any(row["field"] == "experiment_id" for row in comparison["scope_changes"]))

    def test_unknown_data_or_incomplete_config_is_explicitly_incomparable(self):
        original = self.payload()
        original.pop("data_sha256")
        first = self.save(original)
        altered = copy.deepcopy(original)
        altered["experiment_id"] = "different-data-unverified"
        second = self.save(altered)
        comparison = self.archive.compare(first["id"], second["id"])
        self.assertTrue(all("unverified_data_snapshot" in item["reasons"] for item in comparison["metrics"]))
        incomplete = self.payload(0.2)
        incomplete["config"].pop("commission")
        third = self.save(incomplete)
        self.assertTrue(all("incomplete_backtest_scope" in item["reasons"] for item in self.archive.compare(first["id"], third["id"])["metrics"]))

    def test_missing_values_are_not_zero_and_unknown_metrics_are_not_guessed(self):
        first = self.save()
        missing = self.payload(0.2)
        missing["metrics"]["sma"].pop("total_return")
        missing["metrics"]["sma"].update(sharpe=None, unknown_money=99.0)
        second = self.save(missing)
        metrics = {item["metric"]: item for item in self.archive.compare(first["id"], second["id"])["metrics"]}
        for metric in ("total_return", "sharpe"):
            self.assertIsNone(metrics[metric]["new"])
            self.assertIsNone(metrics[metric]["delta"])
            self.assertIn("missing_value", metrics[metric]["reasons"])
        self.assertNotIn("unknown_money", metrics)

    def test_partial_config_can_supply_context_but_conflicting_results_cannot(self):
        full = self.payload()
        partial = {"experiment_id": full["experiment_id"], "config": {"symbol": "000651", "start_date": full["config"]["start_date"]}}
        first = self.save(chats=[chat(partial, "prepare_quant_experiment"), chat(full)])
        second = self.save(self.payload(0.2))
        self.assertTrue(all(row["comparable"] for row in self.archive.compare(first["id"], second["id"])["metrics"]))
        conflict = copy.deepcopy(full)
        conflict["config"]["fast"] = 10
        ambiguous = self.save(chats=[chat(full), chat(conflict)])
        self.assertTrue(all(not row["comparable"] for row in self.archive.compare(ambiguous["id"], second["id"])["metrics"]))

    def test_plain_answers_and_file_paths_are_not_used_as_numeric_evidence(self):
        source = self.folder / "unread.json"
        source.write_text("This is deliberately not valid JSON")
        first = self.save(question="回测000651", answer="收益率80%", chats=[])
        second = self.save(answer="收益率100%", chats=[chat({"result_path": str(source)})])
        comparison = self.archive.compare(first["id"], second["id"])
        self.assertEqual(comparison["metrics"], [])
        self.assertEqual(second["artifact_paths"], [str(source)])
        self.assertEqual(source.read_text(), "This is deliberately not valid JSON")

    def test_model_projections_are_bounded_and_full_text_is_unchanged(self):
        original = "\x01" * 20000 + "完整原文"
        first = self.save(answer=original)
        second = self.save(self.payload(0.2), answer="\x02" * 20000)
        projected = self.archive.get(first["id"], for_model=True)
        self.assertLessEqual(len(json.dumps(projected, ensure_ascii=False)), MAX_MODEL_CHARS)
        self.assertTrue(projected["truncated"])
        self.assertEqual(self.archive.get(first["id"])["answer"], original)
        comparison = self.archive.compare(first["id"], second["id"], for_model=True)
        self.assertLessEqual(len(json.dumps(comparison, ensure_ascii=False)), MAX_MODEL_CHARS)
        self.assertTrue(comparison["truncated"])
        self.assertLessEqual(len(json.dumps(self.archive.find(for_model=True), ensure_ascii=False)), MAX_MODEL_CHARS)

    def test_snapshot_and_symbol_index_commit_atomically(self):
        turn_id = self.memory.save_turn("000651", "完成", ["Quant_Analyst"], [chat(self.payload())])
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TRIGGER reject_symbol BEFORE INSERT ON research_artifact_symbols BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.archive.archive_turn(turn_id)
        self.assertEqual(self.archive.find()["reports"], [])
        self.assertEqual(self.memory.recent_turns()[-1]["id"], turn_id)
        with sqlite3.connect(self.db) as db:
            db.execute("DROP TRIGGER reject_symbol")
        with ThreadPoolExecutor(max_workers=4) as pool:
            records = list(pool.map(lambda _: ResearchArchive(self.db).archive_turn(turn_id), range(8)))
        self.assertEqual(len({row["id"] for row in records}), 1)
        self.assertEqual(len(self.archive.find()["reports"]), 1)

    def test_real_quant_tool_results_supply_comparable_archived_metrics(self):
        from finrobot.functional import quant_dialogue
        data, output = self.folder / "experiments", self.folder / "outputs"
        with patch.object(quant_dialogue, "DATA_ROOT", data), patch.object(quant_dialogue, "OUT_ROOT", output), \
                patch.object(quant_dialogue, "plot_comparison", return_value=str(output / "comparison.png")):
            prepared = json.loads(quant_dialogue.QuantDialogueTools.prepare_quant_experiment(
                "000651", "2023-01-01", "2024-12-31", fast=10, slow=30))
            compared = json.loads(quant_dialogue.QuantDialogueTools.compare_quant_strategies(prepared["experiment_id"]))
        history = [chat(prepared, "prepare_quant_experiment"), chat(compared)]
        first, second = self.save(chats=history), self.save(chats=history)
        diff = self.archive.compare(first["id"], second["id"])
        self.assertTrue(diff["metrics"])
        self.assertTrue(all(row["comparable"] and row["delta"] == 0 for row in diff["metrics"]))
        self.assertEqual(first["experiment_ids"], [prepared["experiment_id"]])
        self.assertTrue(first["artifact_paths"])


if __name__ == "__main__":
    unittest.main()
