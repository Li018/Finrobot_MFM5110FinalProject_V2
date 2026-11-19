"""Persistence, evidence provenance and bounded context without external APIs."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from finrobot.agents.memory import MAX_CONTEXT_CHARS, MAX_EVIDENCE_CHARS, ResearchMemory


ROOT = Path(__file__).resolve().parents[1]


def tool_chat(payload, name="prepare_quant_experiment"):
    return [{"role": "Quant_Analyst", "history": [
        {"role": "assistant", "tool_calls": [{"id": "call_1", "function": {"name": name, "arguments": "{}"}}]},
        {"role": "tool", "tool_responses": [
            {"role": "tool", "tool_call_id": "call_1", "content": json.dumps(payload, ensure_ascii=False)}
        ]},
    ]}]


class ResearchMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "memory" / "research.sqlite3"
        self.memory = ResearchMemory(self.path)

    def save(self, question="回测000001", answer="已完成回测", **kwargs):
        return self.memory.save_turn(question, answer, ["Quant_Analyst"], [], **kwargs)

    def test_verbatim_text_survives_reopen_and_recent_order(self):
        question = "  600519：原始问题\n" + "量化" * 1000
        answer = "答案\n" + "历史证据" * 4000
        first = self.save(question, answer)
        second = self.save("再比较买入持有", "第二轮")
        recovered = ResearchMemory(self.path, session_id=self.memory.session_id)
        turns = recovered.recent_turns()
        self.assertEqual([turn["id"] for turn in turns], [first, second])
        self.assertEqual(turns[0]["question"], question)
        self.assertEqual(turns[0]["answer"], answer)
        self.assertEqual(turns[0]["roles"], ["Quant_Analyst"])
        self.assertEqual(turns[0]["status"], "completed")
        self.assertTrue(turns[0]["created_at"].endswith("+00:00"))
        self.assertEqual(recovered.sessions()[0]["title"], question[:80])

    def test_fresh_process_restores_saved_session_and_notes(self):
        self.save("继续000651", "已记录原参数")
        self.memory.remember("默认使用中文回答")
        script = (
            "import json,sys; from finrobot.agents.memory import ResearchMemory; "
            "m=ResearchMemory(sys.argv[1],session_id=sys.argv[2]); "
            "print(json.dumps(m.context('继续'),ensure_ascii=False))"
        )
        result = subprocess.run([sys.executable, "-c", script, str(self.path), self.memory.session_id],
                                env={**os.environ, "PYTHONPATH": str(ROOT / "finrobot_autogen")},
                                capture_output=True, text=True, encoding="utf-8", check=True)
        context = json.loads(result.stdout)
        self.assertEqual(context["recent_turns"][0]["question"], "继续000651")
        self.assertEqual(context["notes"][0]["text"], "默认使用中文回答")

    def test_new_session_isolated_but_explicit_notes_are_shared(self):
        self.save("600519旧研究", "旧会话的答案")
        self.memory.remember("回测滑点默认五个基点")
        other = ResearchMemory(self.path)
        self.assertNotEqual(other.session_id, self.memory.session_id)
        self.assertEqual(other.recent_turns(), [])
        context = other.context("600519")
        self.assertEqual(context["recent_turns"], [])
        self.assertEqual(context["notes"][0]["text"], "回测滑点默认五个基点")
        recalled = other.recall("600519")
        self.assertEqual(recalled["turns"][0]["session_id"], self.memory.session_id)
        self.assertEqual(other.context("600519")["recent_turns"], [])

    def test_incompatible_resume_does_not_modify_existing_session(self):
        self.save()
        with self.assertRaisesRegex(ValueError, "different mode or role"):
            ResearchMemory(self.path, session_id=self.memory.session_id, mode="single", role="Quant_Analyst")
        single = ResearchMemory(self.path, mode="single", role="Quant_Analyst")
        with self.assertRaisesRegex(ValueError, "different mode or role"):
            ResearchMemory(self.path, session_id=single.session_id, mode="single", role="Market_Analyst")
        self.assertEqual(ResearchMemory(self.path, session_id=self.memory.session_id).recent_turns()[0]["answer"], "已完成回测")

    def test_failed_and_partial_turns_are_recorded_but_not_recalled_as_completed(self):
        complete = self.save("000001完成", "完成的结果")
        self.save("000001部分失败", "只有部分证据", status="partial")
        self.save("000001请求失败", "", status="failed")
        self.assertEqual([row["status"] for row in self.memory.recent_turns()], ["completed", "partial", "failed"])
        self.assertEqual([row["id"] for row in self.memory.recall("000001")["turns"]], [complete])
        self.assertEqual([row["id"] for row in self.memory.context("继续")["recent_turns"]], [complete])
        with self.assertRaisesRegex(ValueError, "must have an answer"):
            self.save(answer="  ")

    def test_explicit_notes_are_verbatim_and_deletions_persist(self):
        text = " \n优先研究消费股；不要据此推断当前持仓。\n" + "偏好" * 3000
        note = self.memory.remember(text)
        self.assertEqual(note["text"], text)
        recovered = ResearchMemory(self.path, session_id=self.memory.session_id)
        self.assertEqual(recovered.recall("消费股")["notes"][0]["text"], text)
        self.assertTrue(recovered.forget(note["id"]))
        self.assertFalse(self.memory.forget(note["id"]))
        self.assertEqual(self.memory.context("继续")["notes"], [])
        self.assertEqual(self.memory.recall("")["notes"], [])

    def test_sql_and_wildcard_characters_are_literal_data(self):
        text = "literal % _ '; DROP TABLE turns; --"
        note = self.memory.remember(text)
        self.save(text, "仍然能保存")
        self.assertIn(note["id"], [row["id"] for row in self.memory.recall(text)["notes"]])
        self.assertEqual(len(self.memory.recall("%_")["turns"]), 0)
        self.assertEqual(len(self.memory.recent_turns()), 1)
        self.assertTrue(self.memory.forget(note["id"]))
        with self.assertRaises(ValueError):
            self.memory.forget("1 OR 1=1")

    def test_stock_recall_does_not_expand_to_every_matching_generic_keyword(self):
        self.save("回测000001", "第一次")
        self.save("回测600519", "第二次")
        found = self.memory.recall("600519 回测")["turns"]
        self.assertEqual([turn["question"] for turn in found], ["回测600519"])
        self.memory.remember("回测默认滑点为五个基点")
        self.assertEqual(len(self.memory.recall("我的滑点设置")["notes"]), 1)

    def test_only_tool_results_supply_evidence_and_credentials_are_not_stored(self):
        experiment_id = "0123456789abcdef"
        payload = {
            "ok": True, "experiment_id": experiment_id,
            "config": {"symbol": "000651", "start_date": "2023-01-01", "end_date": "2024-12-31", "fast": 10, "slow": 30,
                       "api_key": "DO_NOT_STORE_SECRET", "llm_config": {"api_key": "NESTED_SECRET"}},
            "source": {"path": "data/quant_dialogue/0123456789abcdef/prices.csv",
                       "metadata": {"fetched_at": "2026-11-19T10:00:00+00:00", "actual_start": "2023-01-03"}},
            "metrics": {"sma": {"total_return": 0.06, "sharpe": 0.8}},
            "equity": [{"secret": "LARGE_TABLE"}] * 1000,
        }
        chats = tool_chat(payload)
        chats[0]["history"].insert(0, {"role": "user", "content": '{"experiment_id":"invented_user_id"}'})
        chats[0]["history"].append({"role": "assistant", "tool_calls": None,
                                   "content": '{"experiment_id":"invented_assistant_id"}'})
        self.memory.save_turn("准备实验", "实验准备完成", ["Quant_Analyst"], chats)
        evidence = self.memory.recent_turns()[0]["evidence"]
        self.assertEqual(evidence[0]["experiment_id"], experiment_id)
        self.assertEqual(evidence[0]["tool"], "prepare_quant_experiment")
        self.assertEqual(evidence[0]["config"]["symbol"], "000651")
        self.assertEqual(evidence[0]["config"]["start_date"], "2023-01-01")
        self.assertEqual(evidence[0]["metrics"]["sma"]["total_return"], 0.06)
        serialized = json.dumps(evidence)
        for excluded in ("DO_NOT_STORE_SECRET", "NESTED_SECRET", "LARGE_TABLE", "invented_user_id", "invented_assistant_id"):
            self.assertNotIn(excluded, serialized)
        self.assertEqual(self.memory.recall("000651")["turns"][0]["evidence"], evidence)

    def test_plain_report_path_and_legacy_function_results_are_retained(self):
        report = "artifacts/reports/报告 2024.pdf"
        chats = [{"role": "Expert_Investor", "history": [
            {"role": "function", "name": "build_annual_report", "content": report},
            {"role": "assistant", "content": "invented.pdf"},
        ]}]
        self.memory.save_turn("生成报告", "报告完成", ["Expert_Investor"], chats)
        self.assertEqual(self.memory.recent_turns()[0]["evidence"],
                         [{"role": "Expert_Investor", "tool": "build_annual_report", "result_path": report}])

    def test_evidence_and_context_budgets_keep_complete_experiment_identifiers(self):
        expected_ids = []
        for i in range(3):
            experiment_id = f"{i:016x}"
            expected_ids.append(experiment_id)
            chats = tool_chat({"experiment_id": experiment_id, "config": {"symbol": "600519", "start_date": "2021-01-01", "end_date": "2025-12-31"},
                               "metrics": {f"metric_{k}": k / 100 for k in range(2000)}})
            self.memory.save_turn("原始问题" * 10000, "原始答案" * 10000, ["Quant_Analyst"], chats)
        for i in range(20):
            self.memory.remember(f"偏好{i}：" + "使用中文并标记数据日期" * 1000)
        for turn in self.memory.recent_turns():
            self.assertLessEqual(len(json.dumps(turn["evidence"], ensure_ascii=False)), MAX_EVIDENCE_CHARS)
            self.assertEqual(len(turn["answer"]), 40000)
        context = self.memory.context("继续")
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), MAX_CONTEXT_CHARS)
        self.assertEqual(len(context["recent_turns"]), 3)
        self.assertEqual([turn["evidence"][0]["experiment_id"] for turn in context["recent_turns"]], expected_ids)
        self.assertEqual(context["recent_turns"][0]["evidence"][0]["config"]["start_date"], "2021-01-01")
        self.assertEqual(self.memory.context("继续", include_recent=False)["recent_turns"], [])

    def test_many_tool_results_remain_bounded_without_partial_ids(self):
        chats = []
        expected = set()
        for index in range(40):
            identifier = f"{index:016x}"
            expected.add(identifier)
            chats.extend(tool_chat({"experiment_id": identifier, "metrics": {f"metric_{n}": n * 0.01 for n in range(50)}}))
        self.memory.save_turn("多个实验", "多次执行完成", ["Quant_Analyst"], chats)
        evidence = self.memory.recent_turns()[0]["evidence"]
        self.assertLessEqual(len(json.dumps(evidence, ensure_ascii=False)), MAX_EVIDENCE_CHARS)
        self.assertTrue(evidence)
        self.assertTrue(all(row["experiment_id"] in expected for row in evidence))

    def test_save_turn_and_session_metadata_are_one_transaction(self):
        connection = sqlite3.connect(str(self.path))
        try:
            connection.execute("CREATE TRIGGER reject_session_update BEFORE UPDATE ON sessions BEGIN SELECT RAISE(ABORT, 'blocked update'); END")
            connection.commit()
            with self.assertRaisesRegex(sqlite3.IntegrityError, "blocked update"):
                self.save()
            self.assertEqual(self.memory.recent_turns(), [])
            self.assertEqual(self.memory.sessions()[0]["title"], "")
            connection.execute("DROP TRIGGER reject_session_update")
            connection.commit()
            self.save()
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
