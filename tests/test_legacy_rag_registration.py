"""The legacy portfolio example registers its retrieval callable, without APIs."""
import contextlib
import importlib.util
import io
import json
import runpy
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import autogen

import finrobot.functional as functional
from finrobot.agents.workflow import MultiAssistantWithLeader


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "finrobot_autogen"


class OfflineRetriever:
    """Replace only the retrieval backend; keep the real RAG function factory."""
    def __init__(self, **kwargs):
        self.update_context = False

    def _check_update_context(self, message):
        return False, False

    def message_generator(self, sender, recipient, context):
        return f"Fixture context: {context['problem']} ({context['n_results']} results)"


class LegacyRAGRegistrationTests(unittest.TestCase):
    def test_portfolio_specialists_can_register_and_execute_the_retrieval_tool(self):
        backend_name = "autogen.agentchat.contrib.retrieve_user_proxy_agent"
        backend = types.ModuleType(backend_name)
        backend.RetrieveUserProxyAgent = OfflineRetriever
        spec = importlib.util.spec_from_file_location(
            "_legacy_rag_fixture", SOURCE / "finrobot/functional/rag.py"
        )
        rag_module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {backend_name: backend}):
            spec.loader.exec_module(rag_module)

        # The example mutates its group configuration, so load a fresh copy.
        investment = types.ModuleType("investment_group")
        investment.group_config = runpy.run_path(str(SOURCE / "experiments/investment_group.py"))["group_config"]
        config = [{"model": "gpt-4-0125-preview", "api_key": "offline-test-key"}]
        with patch.dict(functional.__dict__, {"get_rag_function": rag_module.get_rag_function}), \
             patch.dict(sys.modules, {"investment_group": investment}), \
             patch("autogen.config_list_from_json", return_value=config), \
             patch("finrobot.utils.register_keys_from_json"), \
             patch.object(MultiAssistantWithLeader, "chat") as chat, \
             patch("autogen.agentchat.conversable_agent.OpenAIWrapper") as model_client, \
             patch("autogen.register_function", wraps=autogen.register_function) as register, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            namespace = runpy.run_path(str(SOURCE / "experiments/portfolio_optimization.py"))
            self.assertGreater(register.call_count, 0)
            for call in register.call_args_list:
                function = call.args[0]
                self.assertTrue(callable(function))
                caller, executor = call.kwargs["caller"], call.kwargs["executor"]
                self.assertIn("retrieve_content", [tool["function"]["name"] for tool in caller.llm_config["tools"]])
                success, result = executor.execute_function({
                    "name": "retrieve_content",
                    "arguments": json.dumps({"message": "report risk", "n_results": 2}),
                })
                self.assertTrue(success)
                self.assertEqual(result["content"], "Fixture context: report risk (2 results)")
            chat.assert_called_once()
            model_client.return_value.create.assert_not_called()
        self.assertIsInstance(namespace["rag_assistant"], OfflineRetriever)


if __name__ == "__main__":
    unittest.main()
