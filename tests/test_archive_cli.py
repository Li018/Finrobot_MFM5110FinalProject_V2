"""Archive integration uses real saved turn IDs and preserves existing output."""
import contextlib
import io
import json
from pathlib import Path
import runpy
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from autogen import ConversableAgent, UserProxyAgent
from finrobot.agents.archive_tools import handle_archive_command, save_research_turn
from finrobot.agents.memory import ResearchMemory
from finrobot.agents.memory_tools import register_memory_tool
from finrobot.agents.research_archive import ResearchArchive
from finrobot.agents.research_router import AutoResearch

ROOT = Path(__file__).resolve().parents[1]
API = runpy.run_path(str(ROOT / "finrobot_autogen/run_agent.py"))


class ArchiveCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "memory.sqlite3"
        self.memory = ResearchMemory(self.path)

    def test_completed_auto_answer_is_archived_with_its_exact_source_turn(self):
        research = AutoResearch({}, memory=self.memory)
        with patch.object(research, "_reply", return_value={"answer": "原始结论"}):
            research.answer("分析600519", ("A_Share_Analyst",))
        report = ResearchArchive(self.path).get(research.last_run["report_id"])
        self.assertEqual(report["source_turn_id"], research.last_run["turn_id"])
        self.assertEqual(report["answer"], "原始结论")
        self.assertEqual(report["requested_symbols"], ["600519"])

    def test_partial_turn_and_memory_write_failure_do_not_archive(self):
        with patch("finrobot.agents.archive_tools.ResearchArchive", side_effect=AssertionError("must not archive")):
            result = save_research_turn(self.memory, "分析600519", "未完成", ["A_Share_Analyst"], [], "partial")
            self.assertIn("turn_id", result)
            self.assertNotIn("report_id", result)
            with patch.object(self.memory, "save_turn", side_effect=sqlite3.OperationalError("locked")):
                result = save_research_turn(self.memory, "分析600519", "完成", ["A_Share_Analyst"], [], "completed")
            self.assertEqual(result["memory_error"], "OperationalError")

    def test_archive_failure_preserves_the_saved_answer_and_can_be_retried(self):
        with patch("finrobot.agents.archive_tools.ResearchArchive", side_effect=sqlite3.OperationalError("locked")):
            result = save_research_turn(self.memory, "600519", "可用原文", ["A_Share_Analyst"], [], "completed")
        self.assertEqual(result["archive_error"], "OperationalError")
        self.assertEqual(self.memory.recent_turns()[-1]["answer"], "可用原文")
        with contextlib.redirect_stdout(io.StringIO()):
            API["handle_local_command"](f"/archive {result['turn_id']}", self.memory)
        self.assertEqual(len(ResearchArchive(self.path).find()["reports"]), 1)

    def test_local_report_commands_work_without_model_configuration(self):
        saved = save_research_turn(self.memory, "600519", "可查看原文", ["A_Share_Analyst"], [], "completed")
        argv = ["run_agent.py", f"/report {saved['report_id']}", "--memory-db", str(self.path)]
        with patch.object(sys, "argv", argv), patch.dict(API["main"].__globals__,
                {"register_keys_from_json": Mock(side_effect=AssertionError("must not load keys"))}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            API["main"]()
        self.assertIn("可查看原文", output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            API["handle_local_command"]("/reports 600519", self.memory)
        self.assertIn('"requested_symbols"', output.getvalue())

    def test_disabled_memory_never_opens_an_archive(self):
        with patch("finrobot.agents.archive_tools.ResearchArchive", side_effect=AssertionError("must not open")), \
                contextlib.redirect_stdout(io.StringIO()):
            for command in ("/reports", "/report 1", "/compare 1 2", "/archive 1"):
                self.assertTrue(handle_archive_command(command, None))

    def test_real_autogen_report_tools_read_and_compare_existing_snapshots(self):
        first = save_research_turn(self.memory, "600519", "旧结论", ["A_Share_Analyst"], [], "completed")
        second = save_research_turn(self.memory, "600519", "新结论", ["A_Share_Analyst"], [], "completed")
        with patch("autogen.agentchat.conversable_agent.OpenAIWrapper"):
            caller = ConversableAgent("analyst", llm_config={"config_list": [{"model": "offline", "api_key": "offline"}]})
            executor = UserProxyAgent("user", llm_config=False, code_execution_config=False)
            register_memory_tool(SimpleNamespace(assistant=caller, user_proxy=executor), self.memory)
        with contextlib.redirect_stdout(io.StringIO()):
            ok, result = executor.execute_function({"name": "diff_reports", "arguments": json.dumps(
                {"a_id": first["report_id"], "b_id": second["report_id"]})})
        self.assertTrue(ok)
        comparison = json.loads(result["content"])
        self.assertIn("旧结论", comparison["answer_diff"])
        self.assertIn("新结论", comparison["answer_diff"])
        self.assertEqual(comparison["metrics"], [])


if __name__ == "__main__":
    unittest.main()
