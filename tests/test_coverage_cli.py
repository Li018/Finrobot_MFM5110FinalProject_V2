"""Watchlist commands and model context must share the real persistent store."""
import contextlib
import io
import json
from pathlib import Path
import runpy
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from autogen import ConversableAgent, UserProxyAgent
from finrobot.agents.coverage import ResearchCoverage
from finrobot.agents.coverage_tools import coverage_context, handle_coverage_command
from finrobot.agents.memory import ResearchMemory
from finrobot.agents.memory_tools import register_memory_tool
from finrobot.agents.research_router import AutoResearch


ROOT = Path(__file__).resolve().parents[1]
API = runpy.run_path(str(ROOT / "finrobot_autogen/run_agent.py"))


class CoverageCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "memory.sqlite3"
        self.memory = ResearchMemory(self.path)

    def command(self, text):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertTrue(API["handle_local_command"](text, self.memory))
        return output.getvalue()

    def test_add_update_and_list_work_without_model_configuration(self):
        argv = ["run_agent.py", "/watch sh600519 消费研究", "--memory-db", str(self.path)]
        with patch.object(sys, "argv", argv), patch.dict(API["main"].__globals__,
                {"register_keys_from_json": Mock(side_effect=AssertionError("must not load keys"))}), \
                contextlib.redirect_stdout(io.StringIO()):
            API["main"]()
        self.command("/thesis 600519 现金流持续改善")
        self.command('/thesis 600519 {"catalysts":"年报披露","invalidation":"经营现金流恶化","priority":4}')
        row = ResearchCoverage(self.path).get("600519")
        self.assertEqual(row["thesis"], "现金流持续改善")
        self.assertEqual(row["note"], "消费研究")
        self.assertEqual(row["priority"], 4)
        self.assertIn("现金流持续改善", self.command("/watchlist 600519"))
        self.assertIn("未完成", self.command('/thesis 600519 {"symbol":"AAPL"}'))
        self.assertIsNone(ResearchCoverage(self.path).get("AAPL"))

    def test_no_memory_never_opens_the_coverage_database(self):
        with patch("finrobot.agents.coverage_tools.ResearchCoverage", side_effect=AssertionError("must not open")), \
                contextlib.redirect_stdout(io.StringIO()):
            for command in ("/watch 600519", "/watchlist", "/thesis 600519 test", "/unwatch 600519"):
                self.assertTrue(handle_coverage_command(command, None))

    def test_coverage_updates_clear_retained_single_role_tool_history(self):
        actor = SimpleNamespace(assistant=Mock(), user_proxy=Mock())
        with contextlib.redirect_stdout(io.StringIO()):
            API["handle_local_command"]("/watch 000651 空调", self.memory, assistant=actor)
            API["handle_local_command"]("/unwatch 000651", self.memory, assistant=actor)
        self.assertEqual(actor.assistant.clear_history.call_count, 2)
        self.assertEqual(actor.user_proxy.clear_history.call_count, 2)
        self.assertTrue(actor.memory_reset_history)

    def test_context_is_relevant_bounded_and_keeps_routing_unchanged(self):
        store = ResearchCoverage(self.path)
        store.add("600519", thesis="财报基本面长期现金流" * 2000)
        store.add("000651", thesis="无关记录标记")
        context = coverage_context(self.memory, "600519为什么涨")
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 6000)
        self.assertTrue(context["truncated"])
        self.assertNotIn("无关记录标记", json.dumps(context, ensure_ascii=False))
        self.assertEqual(AutoResearch({}, memory=self.memory).plan("600519为什么涨"), ("Market_Analyst",))
        self.assertEqual(len(store.get("600519")["thesis"]), len("财报基本面长期现金流" * 2000))

    def test_real_autogen_tools_are_read_only_and_return_the_saved_thesis(self):
        ResearchCoverage(self.path).add("000651", thesis="待验证的空调需求假设")
        with patch("autogen.agentchat.conversable_agent.OpenAIWrapper"):
            caller = ConversableAgent("analyst", llm_config={"config_list": [{"model": "offline", "api_key": "offline"}]})
            executor = UserProxyAgent("user", llm_config=False, code_execution_config=False)
            register_memory_tool(SimpleNamespace(assistant=caller, user_proxy=executor), self.memory)
        names = {tool["function"]["name"] for tool in caller.llm_config["tools"]}
        self.assertTrue({"get_coverage", "list_coverage", "recall_research"} <= names)
        self.assertFalse({"add", "update", "remove", "remember", "forget"} & names)
        with contextlib.redirect_stdout(io.StringIO()):
            ok, result = executor.execute_function({"name": "get_coverage", "arguments": '{"symbol":"000651"}'})
        self.assertTrue(ok)
        self.assertIn("待验证的空调需求假设", result["content"])

    def test_auto_expert_receives_only_matching_user_research_context(self):
        ResearchCoverage(self.path).add("600519", thesis="用户观点记录_389")
        research = AutoResearch({}, memory=self.memory)
        with patch.object(research, "_reply", return_value={"answer": "本轮结论"}) as reply:
            research.answer("600519基本面", ("A_Share_Analyst",))
        self.assertIn("用户观点记录_389", reply.call_args.args[1])
        self.assertEqual(self.memory.recent_turns()[-1]["question"], "600519基本面")


if __name__ == "__main__":
    unittest.main()
