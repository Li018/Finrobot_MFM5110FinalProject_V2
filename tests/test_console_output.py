"""Console regressions based on AutoGen's observed proxy-relative message roles."""
import contextlib
import io
import logging
import os
import runpy
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
os.environ["DEBUG"] = "false"
os.environ["NLTK_DATA"] = str(ROOT / ".cache/nltk_data")
API = runpy.run_path(str(ROOT / "finrobot_autogen/run_agent.py"))


def result(messages):
    return SimpleNamespace(chat_history=messages)


class Analyst:
    name = "Quant_Analyst"


class ConsoleTests(unittest.TestCase):
    def test_proxy_relative_final_answer(self):
        history = [
            {"role": "assistant", "name": "User_Proxy", "content": "用户的问题"},
            {"role": "assistant", "content": "我先取数", "tool_calls": [{"function": {"name": "prepare_quant_experiment"}}]},
            {"role": "tool", "name": "User_Proxy", "content": '{"data":"raw"}'},
            {"role": "user", "name": "Quant_Analyst", "content": "本轮分析\n收益对比表\nTERMINATE"},
        ]
        self.assertEqual(API["_final_answer"](result(history)), "本轮分析\n收益对比表")

    def test_standard_assistant_with_empty_tool_calls(self):
        history = [{"role": "assistant", "content": "直接回答", "tool_calls": []}]
        self.assertEqual(API["_final_answer"](result(history)), "直接回答")

    def test_do_not_repeat_user_question_or_old_answer(self):
        history = [{"role": "user", "name": "Quant_Analyst", "content": "上一轮分析"},
                   {"role": "assistant", "name": "User_Proxy", "content": "本轮提问"}]
        self.assertEqual(API["_final_answer"](result(history), start_index=1), "")

    def test_unfinished_tool_run_has_no_fake_final(self):
        history = [{"role": "assistant", "content": "接下来计算", "tool_calls": [{"function": {"name": "compare_quant_strategies"}}]},
                   {"role": "tool", "content": "raw metrics"}]
        self.assertEqual(API["_final_answer"](result(history)), "")

    def test_explicit_json_answer_preserved(self):
        text = '{"total_return":0.1,"termination":"TERMINATE"}'
        self.assertEqual(API["_final_answer"](result([{"role": "user", "name": "Quant_Analyst", "content": text}])), text)

    def test_default_console_only_shows_analysis(self):
        actor = Analyst()
        logger = logging.getLogger("autogen.console_regression")
        bound_console = io.StringIO()
        old = logger.handlers[:], logger.propagate
        handler = logging.StreamHandler(bound_console)
        logger.handlers, logger.propagate = [handler], False

        class Proxy:
            chat_messages = {}
            def initiate_chat(self, *args, **kwargs):
                assert kwargs["silent"] is True
                print("EXECUTING FUNCTION and raw tool JSON")
                print("yellow stderr warning", file=sys.stderr)
                logger.warning("yellow price warning")
                return result([{"role": "assistant", "name": "User_Proxy", "content": "用户提问"},
                    {"role": "user", "name": "Quant_Analyst", "content": "本轮结论：依据真实回测进行比较。\nTERMINATE"}])

        folder = ROOT / "tmp/console_tests" / datetime.now().strftime("%Y%m%d_%H%M%S%f") / "finrobot_autogen"
        folder.mkdir(parents=True)
        globals_ = API["ask"].__globals__
        assistant = SimpleNamespace(assistant=actor, user_proxy=Proxy(), show_debug=False)
        console = io.StringIO()
        try:
            with patch.dict(globals_, {"BASE_DIR": folder}), contextlib.redirect_stdout(console):
                API["ask"](assistant, "用户提问", clear_history=True)
            output = console.getvalue()
            self.assertIn("本轮结论", output)
            for noise in ("EXECUTING FUNCTION", "raw tool JSON", "yellow", "已调用工具", "TERMINATE", "\x1b"):
                self.assertNotIn(noise, output)
            self.assertEqual(bound_console.getvalue(), "")
            logs = list((folder.parent / "artifacts/quant_dialogue/chats").glob("*.log"))
            self.assertEqual(len(logs), 1)
            text = logs[0].read_text(encoding="utf-8")
            self.assertIn("EXECUTING FUNCTION", text)
            self.assertIn("yellow price warning", text)
            self.assertEqual(logger.handlers, [handler])
            self.assertFalse(logger.propagate)
        finally:
            logger.handlers, logger.propagate = old

    def test_failure_is_visible_without_traceback_noise(self):
        class Proxy:
            chat_messages = {}
            def initiate_chat(self, *args, **kwargs):
                print("internal tool details")
                raise RuntimeError("internal error details")
        folder = ROOT / "tmp/console_tests" / datetime.now().strftime("%Y%m%d_%H%M%S%f") / "finrobot_autogen"
        folder.mkdir(parents=True)
        assistant = SimpleNamespace(assistant=Analyst(), user_proxy=Proxy(), show_debug=False)
        console = io.StringIO()
        with patch.dict(API["ask"].__globals__, {"BASE_DIR": folder}), contextlib.redirect_stdout(console):
            with self.assertRaises(RuntimeError):
                API["ask"](assistant, "question", clear_history=True)
        self.assertIn("本轮分析未完成", console.getvalue())
        self.assertNotIn("Traceback", console.getvalue())
        self.assertNotIn("internal", console.getvalue())

    def test_tool_summary_is_limited_to_current_turn(self):
        history = [{"tool_calls": [{"function": {"name": "old_tool"}}]},
                   {"tool_calls": [{"function": {"name": "new_tool"}}, {"function": {"name": "new_tool"}}]}]
        self.assertEqual(API["_called_tools"](result(history), start_index=1), ["new_tool"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
