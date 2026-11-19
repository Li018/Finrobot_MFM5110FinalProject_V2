"""Opt-in review integration retains the original research and real AutoGen boundaries."""
import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from autogen import Agent
from finrobot.agents import research_router as router
from finrobot.agents.memory import ResearchMemory
from finrobot.agents.research_archive import ResearchArchive

ROOT = Path(__file__).resolve().parents[1]
API = runpy.run_path(str(ROOT / "finrobot_autogen/run_agent.py"))


def side(claim="在假设成立时支持该观点", source="S1"):
    return {"answer": json.dumps({"arguments": [{"claim": claim, "evidence_ids": [source]}]}, ensure_ascii=False)}


def judge():
    return {"answer": json.dumps({"contested_claims": [{"claim": "假设仍待核实", "evidence_ids": ["S1"]}],
        "required_evidence": ["补充最新披露"], "conditional_synthesis": "证据不足以形成确定结论",
        "change_my_mind": "当有新的可核实资料时更新判断"}, ensure_ascii=False)}


class ReviewCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "memory.sqlite3"
        self.memory = ResearchMemory(self.path)

    def test_default_path_adds_no_review_stages(self):
        research = router.AutoResearch({}, memory=self.memory)
        with patch.object(research, "_reply", return_value={"answer": "正常分析"}) as reply:
            answer = research.answer("600519基本面", ("A_Share_Analyst",))
        self.assertEqual(answer, "正常分析")
        self.assertEqual(reply.call_count, 1)
        self.assertNotIn("review", research.last_run)

    def test_review_receives_current_evidence_and_saves_one_combined_result(self):
        self.memory.save_turn("分析000651", "OLD_RESEARCH_MARKER", ["Quant_Analyst"], [])
        research = router.AutoResearch({}, memory=self.memory, review=True)
        prompts = []

        def reply(role, prompt):
            if isinstance(role, str):
                research._current_chats.append({"role": role, "history": [{"role": "tool",
                    "name": "compare_quant_strategies", "content": json.dumps({
                        "experiment_id": "CURRENT_EXPERIMENT", "symbol": "600519",
                        "metrics": {"strategy": {"total_return": 0.1}}})}]})
                return {"answer": "本轮已完成研究"}
            prompts.append(prompt)
            return judge() if role["name"] == "Review_Judge" else side()

        with patch.object(research, "_reply", side_effect=reply):
            answer = research.answer("600519基本面", ("A_Share_Analyst",))
        self.assertIn("本轮已完成研究", answer)
        self.assertIn("多空审查", answer)
        self.assertTrue(research.last_run["ok"])
        self.assertEqual(research.last_run["review"]["stage_calls"], 3)
        self.assertTrue(all("CURRENT_EXPERIMENT" in prompt for prompt in prompts))
        self.assertTrue(all("OLD_RESEARCH_MARKER" not in prompt for prompt in prompts))
        report = ResearchArchive(self.path).get(research.last_run["report_id"])
        self.assertEqual(report["answer"], answer)
        self.assertEqual(report["roles"], ["A_Share_Analyst"])
        self.assertEqual(len(ResearchArchive(self.path).find()["reports"]), 1)

    def test_failed_review_keeps_the_original_answer_and_partial_status(self):
        research = router.AutoResearch({}, memory=self.memory, review=True)
        with patch.object(research, "_reply", side_effect=[{"answer": "可用原始分析"}, side(source="FAKE"), side()]):
            answer = research.answer("600519基本面", ("A_Share_Analyst",))
        self.assertIn("可用原始分析", answer)
        self.assertIn("审查未完成", answer)
        self.assertFalse(research.last_run["ok"])
        self.assertEqual(research.last_run["review"]["stage_calls"], 2)
        self.assertEqual(self.memory.recent_turns()[-1]["status"], "partial")
        self.assertEqual(ResearchArchive(self.path).find()["reports"], [])

    def test_successful_review_does_not_hide_a_failed_original_specialist(self):
        research = router.AutoResearch({}, memory=self.memory, review=True)
        responses = [{"answer": "可用财务分项"}, {"error": "ConnectionError"},
                     {"answer": "仅汇总已取得的财务证据"}, side(), side(), judge()]
        with patch.object(research, "_reply", side_effect=responses):
            research.answer("600519基本面和新闻", ("A_Share_Analyst", "Market_Analyst"))
        self.assertTrue(research.last_run["review"]["ok"])
        self.assertFalse(research.last_run["ok"])
        self.assertIn("Market_Analyst", research.last_run["review"]["source_failures"])

    def test_all_failed_sources_do_not_trigger_reviewer_calls(self):
        research = router.AutoResearch({}, review=True)
        with patch.object(research, "_reply", return_value={"error": "ConnectionError"}) as reply:
            research.answer("600519基本面", ("A_Share_Analyst",))
        self.assertEqual(reply.call_count, 1)
        self.assertEqual(research.last_run["review"]["stage_calls"], 0)

    def test_real_autogen_review_is_one_turn_without_tools_or_code_execution(self):
        original = router.SingleAssistant
        created = []

        def factory(*args, **kwargs):
            agent = original(*args, **kwargs)
            agent.assistant.register_reply([Agent, None],
                lambda recipient, messages=None, sender=None, config=None:
                    (True, '{"arguments": []}\nTERMINATE'), position=0)
            created.append(agent)
            return agent

        llm = {"config_list": [{"model": "offline", "api_key": "offline"}],
               "tools": [{"type": "function", "function": {"name": "unexpected_tool", "parameters": {"type": "object"}}}]}
        with patch("autogen.agentchat.conversable_agent.OpenAIWrapper") as client, \
                patch.object(router, "SingleAssistant", side_effect=factory), \
                contextlib.redirect_stdout(io.StringIO()):
            result = router.AutoResearch(llm, memory=self.memory, review=True)._reply(
                {"name": "Bull_Reviewer", "profile": "Review only supplied material.", "toolkits": []}, "current sources")
        self.assertEqual(json.loads(result["answer"]), {"arguments": []})
        self.assertEqual(len(created[0].user_proxy.chat_messages[created[0].assistant]), 2)
        self.assertFalse(created[0].assistant.llm_config.get("tools"))
        self.assertEqual(created[0].user_proxy.function_map, {})
        self.assertFalse(created[0].user_proxy._code_execution_config)
        client.return_value.create.assert_not_called()

    def test_cli_review_preserves_fixed_role_and_single_session_mode(self):
        argv = ["run_agent.py", "分析000651", "--role", "Quant_Analyst", "--review",
                "--session", "review-single", "--memory-db", str(self.path)]
        runner = Mock()
        with patch.object(sys, "argv", argv), patch.dict(API["main"].__globals__,
                {"run_auto": runner, "register_keys_from_json": lambda *a: None, "describe_provider": lambda: "offline"}), \
                patch("autogen.config_list_from_json", return_value=[{"model": "offline", "api_key": "offline"}]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            API["main"]()
        self.assertEqual(runner.call_args.kwargs["fixed_role"], "Quant_Analyst")
        self.assertTrue(runner.call_args.kwargs["review"])
        resumed = ResearchMemory(self.path, session_id="review-single", mode="single", role="Quant_Analyst")
        self.assertEqual(resumed.session_id, "review-single")
        self.assertIn("--session review-single --role Quant_Analyst --review", output.getvalue())

    def test_actual_provider_config_strips_tool_settings_only_for_review(self):
        original = router.SingleAssistant
        settings = {
            "tools": [{"type": "function", "function": {"name": "old_retrieval", "parameters": {"type": "object"}}}],
            "functions": [{"name": "legacy_retrieval", "parameters": {"type": "object"}}],
            "tool_choice": "required",
            "function_call": {"name": "legacy_retrieval"},
            "parallel_tool_calls": True,
        }
        llm = {**deepcopy(settings), "temperature": 0.7, "config_list": [
            {"model": "offline", "api_key": "offline", **deepcopy(settings)},
            {"model": "fallback", "api_key": "offline", "temperature": 0.2, **deepcopy(settings)},
        ]}
        unchanged = deepcopy(llm)
        for role_name in ("Bull_Reviewer", "Plain_Analyst"):
            with self.subTest(role=role_name):
                created = []
                def factory(*args, **kwargs):
                    agent = original(*args, **kwargs)
                    agent.assistant.register_reply([Agent, None],
                        lambda recipient, messages=None, sender=None, config=None:
                            (True, '{"arguments": []}\nTERMINATE'), position=0)
                    created.append(agent)
                    return agent

                # Keep OpenAIWrapper's real top-level/config-list merge. Only
                # replace SDK construction and reply generation; no API calls.
                with patch("autogen.oai.client.OpenAI") as sdk, \
                        patch.object(router, "SingleAssistant", side_effect=factory), \
                        contextlib.redirect_stdout(io.StringIO()):
                    result = router.AutoResearch(llm, review=True)._reply(
                        {"name": role_name, "profile": "Current material only.", "toolkits": []}, "sources")
                self.assertEqual(json.loads(result["answer"]), {"arguments": []})
                provider_configs = created[0].assistant.client._config_list
                self.assertEqual(len(provider_configs), 2)
                self.assertEqual([config["temperature"] for config in provider_configs], [0.7, 0.2])
                for config in [created[0].assistant.llm_config, *provider_configs]:
                    for key in settings:
                        if role_name == "Bull_Reviewer":
                            self.assertNotIn(key, config)
                        else:
                            self.assertEqual(config[key], settings[key])
                self.assertEqual(llm, unchanged)
                sdk.return_value.chat.completions.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
