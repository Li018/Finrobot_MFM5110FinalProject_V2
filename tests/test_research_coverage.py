"""Persistent, explicit research coverage without changing other user memory."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from finrobot.agents.coverage import ResearchCoverage, extract_symbols, normalize_symbol
from finrobot.agents.memory import ResearchMemory


ROOT = Path(__file__).resolve().parents[1]


class ResearchCoverageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite3"
        self.coverage = ResearchCoverage(self.path)

    def test_symbols_normalize_without_guessing_stock_names(self):
        for value, expected in ((" sh600519 ", "600519"), ("000001.sz", "000001"),
                                ("BJ430047", "430047"), ("aapl", "AAPL"),
                                ("brk.b", "BRK.B"), ("brk-b", "BRK-B")):
            self.assertEqual(normalize_symbol(value), expected)
        for value in ("", "../aapl", "贵州茅台", "12345", "1234567", "AAPL;DELETE", None):
            with self.assertRaises(ValueError):
                normalize_symbol(value)
        self.assertEqual(extract_symbols("分析sh600519、600519.SH和000001；PDF JSON SMA AAPL，比较 $AAPL 与 NASDAQ:MSFT、ticker=BRK.B。"),
                         ["600519", "000001", "AAPL", "MSFT", "BRK.B"])
        self.assertEqual(extract_symbols("20260101，序列1234567，PDF JSON SMA AAPL"), [])

    def test_explicit_thesis_survives_a_new_process(self):
        thesis = "  原始论点\n" + "现金流改善" * 4000
        item = self.coverage.add("600519", note="消费研究", thesis=thesis,
                                 catalysts="年报披露", invalidation="经营现金流持续下降", priority=4)
        code = "import json,sys; from finrobot.agents.coverage import ResearchCoverage; print(json.dumps(ResearchCoverage(sys.argv[1]).get('SH600519'),ensure_ascii=False))"
        result = subprocess.run([sys.executable, "-c", code, str(self.path)],
                                env={**os.environ, "PYTHONPATH": str(ROOT / "finrobot_autogen")},
                                check=True, capture_output=True, text=True, encoding="utf-8")
        loaded = json.loads(result.stdout)
        self.assertEqual(loaded, item)
        self.assertEqual(loaded["thesis"], thesis)
        self.assertTrue(loaded["created_at"].endswith("+00:00"))
        self.assertEqual(loaded["created_at"], loaded["updated_at"])

    def test_updates_preserve_omitted_fields_and_clear_only_explicit_text(self):
        first = self.coverage.add("000651", note="空调", thesis="需求恢复", catalysts="换新补贴",
                                  invalidation="收入持续下降", priority=3)
        updated = self.coverage.update("SZ000651", catalysts="", status=" RESEARCHING ")
        self.assertEqual(updated["thesis"], first["thesis"])
        self.assertEqual(updated["note"], first["note"])
        self.assertEqual(updated["invalidation"], first["invalidation"])
        self.assertEqual(updated["priority"], 3)
        self.assertEqual(updated["catalysts"], "")
        self.assertEqual(updated["status"], "researching")
        self.assertEqual(updated["created_at"], first["created_at"])
        self.assertEqual(self.coverage.update("000651"), updated)

    def test_invalid_or_duplicate_writes_leave_prior_content_unchanged(self):
        original = self.coverage.add("AAPL", note="original", priority=2)
        for changes in ({"thesis": "discard", "priority": True}, {"status": "purchased"},
                        {"note": None}, {"symbol": "MSFT"}, {"priority": 6}):
            with self.assertRaises(ValueError):
                self.coverage.update("AAPL", **changes)
            self.assertEqual(self.coverage.get("AAPL"), original)
        with self.assertRaisesRegex(ValueError, "already tracked"):
            self.coverage.add("aapl", note="overwrite")
        self.assertEqual(self.coverage.get("AAPL"), original)
        with self.assertRaisesRegex(ValueError, "not tracked"):
            self.coverage.update("MSFT", note="never created")
        self.assertIsNone(self.coverage.get("MSFT"))

    def test_filtering_is_literal_and_priority_order_is_persistent(self):
        self.coverage.add("000651", note="50%_budget", priority=2)
        self.coverage.add("600519", thesis="O'Reilly research", status="研究", priority=5)
        self.coverage.add("AAPL", note="independent", status="归档", priority=1)
        self.assertEqual([x["symbol"] for x in self.coverage.list()], ["600519", "000651", "AAPL"])
        self.assertEqual([x["symbol"] for x in self.coverage.list(status=" researching ")], ["600519"])
        self.assertEqual([x["symbol"] for x in self.coverage.list(query="50%_")], ["000651"])
        self.assertEqual([x["symbol"] for x in self.coverage.list(query="O'Reilly")], ["600519"])
        self.assertEqual([x["symbol"] for x in self.coverage.list(query="sz000651")], ["000651"])
        self.assertEqual(self.coverage.list(query="' OR 1=1 --"), [])
        for limit in (0, 101, True, 1.5):
            with self.assertRaises(ValueError):
                self.coverage.list(limit=limit)

    def test_store_shares_database_without_changing_memory_or_sessions(self):
        memory = ResearchMemory(self.path, session_id="kept")
        memory.remember("原笔记")
        memory.save_turn("600519研究", "已完成", ["A_Share_Analyst"], [])
        self.coverage.add("600519", thesis="用户论点")
        self.coverage.update("600519", status="archived")
        self.assertTrue(self.coverage.remove("SH600519"))
        self.assertFalse(self.coverage.remove("600519"))
        reopened = ResearchMemory(self.path, session_id="kept")
        self.assertEqual(reopened.recall("原笔记")["notes"][0]["text"], "原笔记")
        self.assertEqual(reopened.recent_turns()[0]["answer"], "已完成")
        self.assertEqual(ResearchCoverage(self.path).list(), [])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_reading_coverage_does_not_mutate_annotation_times(self):
        item = self.coverage.add("BRK.B", thesis="用户明确保存的论点")
        self.coverage.get("BRK.B")
        self.coverage.list(query="BRK.B")
        self.assertEqual(self.coverage.get("BRK.B"), item)


if __name__ == "__main__":
    unittest.main()
