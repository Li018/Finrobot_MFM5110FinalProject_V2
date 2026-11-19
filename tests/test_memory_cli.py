"""Memory must survive CLI restarts without changing routing or inventing evidence."""
import contextlib
import io
import json
import os
import runpy
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autogen import ConversableAgent, UserProxyAgent
from finrobot.agents.memory import ResearchMemory
from finrobot.agents.memory_tools import recall_context, register_memory_tool
from finrobot.agents.research_router import AutoResearch

ROOT = Path(__file__).resolve().parents[1]
API = runpy.run_path(str(ROOT / "finrobot_autogen/run_agent.py"))


class MemoryCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.db = self.folder / "memory.sqlite3"
        self.base = self.folder / "finrobot_autogen"
        self.base.mkdir()
        self.identifier = "000651_20230301_20230731_5_20_5f1d28b7"

    def seed(self, memory):
        payload = {"experiment_id": self.identifier, "config": {"symbol": "000651", "fast": 5,
            "slow": 20, "start_date": "2023-03-01", "end_date": "2023-07-31"}}
        memory.save_turn("回测000651的5/20均线", "已完成，详见回测文件。", ["Quant_Analyst"],
            [{"role": "Quant_Analyst", "history": [{"role": "tool", "name": "prepare_quant_experiment",
                "content": json.dumps(payload)}]}])

    def test_auto_restores_role_and_verified_experiment_after_restart(self):
        self.seed(ResearchMemory(self.db, session_id="quant-session"))
        memory = ResearchMemory(self.db, session_id="quant-session")
        research = AutoResearch({}, memory=memory)
        question = "继续生成并检查风险"
        roles = research.plan(question)
        self.assertEqual(roles, ("Quant_Analyst",))
        with patch.object(research, "_reply", return_value={"answer": "本轮完成"}) as reply:
            research.answer(question, roles)
        self.assertIn(self.identifier, reply.call_args.args[1])
        self.assertEqual(memory.recent_turns()[-1]["question"], question)
        self.assertEqual(memory.recent_turns()[-1]["answer"], "本轮完成")
        self.assertNotIn("memory_context", memory.recent_turns()[-1]["question"])

    def test_new_session_does_not_inject_other_conversations(self):
        self.seed(ResearchMemory(self.db, session_id="prior"))
        research = AutoResearch({}, memory=ResearchMemory(self.db, session_id="new"))
        with patch.object(research, "_reply", return_value={"answer": "新回答"}) as reply:
            research.answer("分析600519的基本面", ("A_Share_Analyst",))
        self.assertNotIn(self.identifier, reply.call_args.args[1])
        self.assertNotIn("000651", reply.call_args.args[1])

    def test_notes_do_not_change_question_routing(self):
        memory = ResearchMemory(self.db)
        memory.remember("研究报告 PDF、动量回测，关注000651。")
        research = AutoResearch({}, memory=memory)
        self.assertEqual(research.plan("600519为什么涨"), ("Market_Analyst",))

    def test_failed_expert_is_not_a_successful_memory(self):
        memory = ResearchMemory(self.db)
        research = AutoResearch({}, memory=memory)
        with patch.object(research, "_reply", return_value={"error": "ConnectionError"}):
            research.answer("回测000651", ("Quant_Analyst",))
        self.assertFalse(research.last_run["ok"])
        self.assertEqual(memory.recent_turns()[-1]["status"], "partial")
        self.assertEqual(memory.recall("000651")["turns"], [])

    def test_write_error_preserves_answer_and_reports_failure(self):
        memory = ResearchMemory(self.db)
        research = AutoResearch({}, memory=memory)
        with patch.object(research, "_reply", return_value={"answer": "可用分析"}), \
                patch.object(memory, "save_turn", side_effect=sqlite3.OperationalError("disk full")):
            answer = research.answer("看600519基本面", ("A_Share_Analyst",))
        self.assertEqual(answer, "可用分析")
        self.assertEqual(research.last_run["memory_error"], "OperationalError")

    def test_read_error_does_not_discard_current_request(self):
        memory = ResearchMemory(self.db)
        research = AutoResearch({}, memory=memory)
        with patch.object(research, "_reply", return_value={"answer": "当前分析"}), \
                patch.object(memory, "context", side_effect=sqlite3.OperationalError("locked")):
            answer = research.answer("看600519基本面", ("A_Share_Analyst",))
        self.assertEqual(answer, "当前分析")
        self.assertEqual(research.last_run["memory_read_error"], "OperationalError")

    def test_single_resumes_without_replaying_tool_messages_and_forgets_notes(self):
        memory = ResearchMemory(self.db, session_id="single", mode="single", role="Quant_Analyst")
        self.seed(memory)
        memory = ResearchMemory(self.db, session_id="single", mode="single", role="Quant_Analyst")
        note = memory.remember("用户备注代号ALPHA_NOTE_389")

        class Analyst:
            name = "Quant_Analyst"
            system_message = "Original system instruction."
            def update_system_message(self, text):
                self.system_message = text

        class Proxy:
            def __init__(self):
                self.chat_messages = {}
                self.sent = []
            def initiate_chat(self, analyst, message, clear_history, **kwargs):
                history = [] if clear_history else self.chat_messages.get(analyst, [])[:]
                self.sent.append({"system": analyst.system_message, "message": message, "history": history[:]})
                history.extend([{"role": "user", "name": "User_Proxy", "content": message},
                                {"role": "assistant", "name": analyst.name, "content": "本轮完成\nTERMINATE"}])
                self.chat_messages[analyst] = history
                return SimpleNamespace(chat_history=history)

        proxy = Proxy()
        actor = SimpleNamespace(assistant=Analyst(), user_proxy=proxy, show_debug=False, research_memory=memory)
        with patch.dict(API["ask"].__globals__, {"BASE_DIR": self.base}), contextlib.redirect_stdout(io.StringIO()):
            API["ask"](actor, "继续这个实验", clear_history=True)
            memory.forget(note["id"])
            API["ask"](actor, "再解释风险", clear_history=False)
        self.assertIn(self.identifier, proxy.sent[0]["system"])
        self.assertEqual(proxy.sent[0]["history"], [])
        self.assertIn("ALPHA_NOTE_389", proxy.sent[0]["system"])
        self.assertNotIn("ALPHA_NOTE_389", json.dumps(proxy.sent[1]))
        self.assertNotIn("memory_context", proxy.sent[0]["message"])
        self.assertEqual(memory.recent_turns()[-1]["question"], "再解释风险")

    def test_local_commands_work_without_api_configuration(self):
        argv = ["run_agent.py", "/remember 本金十万", "--memory-db", str(self.db), "--session", "local"]
        with patch.object(sys, "argv", argv), patch.dict(API["main"].__globals__,
                {"register_keys_from_json": lambda *a: self.fail("Must not load keys")}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            API["main"]()
        self.assertIn("已保存笔记", output.getvalue())
        memory = ResearchMemory(self.db, session_id="local")
        self.assertEqual(memory.recall("本金")["notes"][0]["text"], "本金十万")

    def test_no_memory_does_not_initialize_or_read_database(self):
        argv = ["run_agent.py", "/memory", "--no-memory", "--memory-db", str(self.db)]
        with patch.object(sys, "argv", argv), patch.dict(API["main"].__globals__,
                {"ResearchMemory": lambda *a, **k: self.fail("Memory must remain unopened")}), \
                contextlib.redirect_stdout(io.StringIO()):
            API["main"]()
        self.assertFalse(self.db.exists())

    def test_relative_database_path_is_independent_of_launcher_working_directory(self):
        argv = ["run_agent.py", "/remember 独立项目记忆", "--memory-db", "data/memory/project.sqlite3"]
        previous = Path.cwd()
        try:
            # The Windows launcher changes into finrobot_autogen before Python.
            os.chdir(self.base)
            with patch.object(sys, "argv", argv), patch.dict(API["main"].__globals__,
                    {"BASE_DIR": self.base}), contextlib.redirect_stdout(io.StringIO()):
                API["main"]()
        finally:
            os.chdir(previous)
        expected = self.folder / "data/memory/project.sqlite3"
        self.assertTrue(expected.is_file())
        self.assertFalse((self.base / "data/memory/project.sqlite3").exists())
        self.assertEqual(ResearchMemory(expected).recall("独立项目")["notes"][0]["text"], "独立项目记忆")

    def test_recall_tool_is_registered_and_reads_previous_research(self):
        self.seed(ResearchMemory(self.db, session_id="old"))
        memory = ResearchMemory(self.db, session_id="new")
        with patch("autogen.agentchat.conversable_agent.OpenAIWrapper") as client:
            caller = ConversableAgent("analyst", llm_config={"config_list": [{"model": "offline", "api_key": "offline"}]})
            executor = UserProxyAgent("user", llm_config=False, code_execution_config=False)
            register_memory_tool(SimpleNamespace(assistant=caller, user_proxy=executor), memory)
            client.return_value.create.assert_not_called()
        ok, result = executor.execute_function({"name": "recall_research",
            "arguments": json.dumps({"query": "000651"})})
        self.assertTrue(ok)
        self.assertIn(self.identifier, result["content"])

    def test_recall_tool_bounds_previews_without_changing_stored_text(self):
        memory = ResearchMemory(self.db)
        self.seed(memory)
        original = "长答案" * 10000
        memory.save_turn("000651历史长答案", original, ["Quant_Analyst"], [])
        result = recall_context(memory, "000651", 10)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 12000)
        self.assertTrue(result["truncated"])
        self.assertIn(self.identifier, json.dumps(result))
        self.assertEqual(memory.recall("000651")["turns"][0]["answer"], original)

    def test_forget_removes_retrieved_notes_from_live_autogen_tool_history(self):
        memory = ResearchMemory(self.db)
        note = memory.remember("用户备注 ALPHA_TOOL_NOTE_389")
        with patch("autogen.agentchat.conversable_agent.OpenAIWrapper"):
            caller = ConversableAgent("analyst", llm_config={"config_list": [{"model": "offline", "api_key": "offline"}]})
            executor = UserProxyAgent("user", llm_config=False, code_execution_config=False)
            actor = SimpleNamespace(assistant=caller, user_proxy=executor)
            register_memory_tool(actor, memory)
        ok, result = executor.execute_function({"name": "recall_research",
            "arguments": json.dumps({"query": "ALPHA_TOOL_NOTE_389"})})
        self.assertTrue(ok)
        caller.send({"tool_calls": [{"id": "recall_1", "type": "function", "function": {
            "name": "recall_research", "arguments": "{}"}}], "content": None},
            executor, request_reply=False, silent=True)
        executor.send({"role": "tool", "tool_responses": [{"tool_call_id": "recall_1",
            "role": "tool", "content": result["content"]}], "content": result["content"]},
            caller, request_reply=False, silent=True)
        self.assertIn("ALPHA_TOOL_NOTE_389", json.dumps(caller.chat_messages[executor]))
        with contextlib.redirect_stdout(io.StringIO()):
            API["handle_memory_command"](f"/forget {note['id']}", memory, assistant=actor)
        self.assertFalse(caller.chat_messages)
        self.assertFalse(executor.chat_messages)
        self.assertTrue(actor.memory_reset_history)
        self.assertEqual(memory.recall("ALPHA_TOOL_NOTE_389")["notes"], [])

    def test_single_session_resume_hint_includes_the_saved_role(self):
        argv = ["run_agent.py", "/memory", "--memory-db", str(self.db),
                "--session", "quant-session", "--role", "Quant_Analyst"]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()) as output:
            API["main"]()
        self.assertIn("--session quant-session --role Quant_Analyst", output.getvalue())
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            API["main"]()

    def test_single_unfinished_answer_stays_visible_but_is_not_recalled(self):
        memory = ResearchMemory(self.db, mode="single", role="Quant_Analyst")
        analyst = SimpleNamespace(name="Quant_Analyst", system_message="Original system instruction.",
                                  update_system_message=lambda text: None)
        result = SimpleNamespace(chat_history=[
            {"role": "user", "name": "User_Proxy", "content": "回测000651"},
            {"role": "assistant", "name": "Quant_Analyst", "content": "还需要检查运行结果"},
        ])
        proxy = SimpleNamespace(chat_messages={}, initiate_chat=lambda *a, **k: result)
        actor = SimpleNamespace(assistant=analyst, user_proxy=proxy, research_memory=memory)
        with patch.dict(API["ask"].__globals__, {"BASE_DIR": self.base}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            API["ask"](actor, "回测000651", clear_history=True)
        self.assertIn("还需要检查运行结果", output.getvalue())
        self.assertEqual(memory.recent_turns()[-1]["status"], "partial")
        self.assertEqual(memory.recall("000651")["turns"], [])


if __name__ == "__main__":
    unittest.main()
