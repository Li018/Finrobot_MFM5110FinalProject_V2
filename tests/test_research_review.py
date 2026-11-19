"""Current-evidence debate, real response parsing and explicit partial failures."""
from copy import deepcopy
import json
import unittest

from finrobot.agents.research_review import (
    MAX_SOURCE_CHARS, REVIEW_ROLE_NAMES, run_research_review,
)


def side(claim="在成本和样本口径不变时支持该解释", ids=None):
    return {"answer": json.dumps({"arguments": [{"claim": claim, "evidence_ids": ids or ["S1"]}]}, ensure_ascii=False)}


def judge():
    return {"answer": json.dumps({
        "contested_claims": [{"claim": "样本外是否仍成立尚不清楚", "evidence_ids": ["S1", "T1"]}],
        "required_evidence": ["相同成本口径的独立区间回测"],
        "conditional_synthesis": "现有结果只支持历史区间的条件判断，暂不推断未来收益。",
        "change_my_mind": "若独立区间和费用敏感性结果一致，再提高对该解释的信任。",
    }, ensure_ascii=False)}


class ResearchReviewTests(unittest.TestCase):
    def setUp(self):
        self.results = {"Quant_Analyst": {"answer": "000651 / 2024：总收益 0.10636990016150194。\n待核实成本敏感性。"}}
        self.tool = {"role": "Quant_Analyst", "tool": "compare_quant_strategies",
                     "experiment_id": "000651_2024_5_20_exact_reference",
                     "config": {"symbol": "000651", "fast": 5, "slow": 20,
                                "start_date": "2024-01-01", "end_date": "2024-12-31"},
                     "metrics": {"total_return": 0.10636990016150194},
                     "result_path": "artifacts/quant_dialogue/000651/result.json"}

    def callback(self, outputs):
        calls = []
        remaining = iter(outputs)
        def reply(role, prompt):
            calls.append((role, prompt))
            value = next(remaining)
            if isinstance(value, Exception):
                raise value
            return value
        return calls, reply

    def test_full_review_preserves_sources_and_keeps_sides_independent(self):
        before = deepcopy((self.results, self.tool))
        calls, reply = self.callback([side("多方需要原假设成立"), side("空方关注成本敏感性"), judge()])
        result = run_research_review("复核这个回测结论", self.results, reply, tool_evidence=[self.tool])
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["stage_calls"], 3)
        self.assertEqual(REVIEW_ROLE_NAMES, {call[0]["name"] for call in calls})
        self.assertTrue(all(call[0]["toolkits"] == [] for call in calls))
        payloads = [json.loads(call[1].split("本轮材料（JSON）：\n", 1)[1]) for call in calls]
        self.assertEqual(payloads[0], payloads[1])
        self.assertNotIn("sides", payloads[1])
        self.assertIn("sides", payloads[2])
        self.assertEqual(payloads[2]["sources"], payloads[0]["sources"])
        self.assertEqual(result["sources"][0]["content"], self.results["Quant_Analyst"]["answer"])
        self.assertEqual(result["sources"][1]["content"], self.tool)
        self.assertEqual((self.results, self.tool), before)
        self.assertIn("条件结论", result["answer"])
        self.assertIn("仍需补充的证据", result["answer"])
        self.assertIn("尚未独立核实", result["answer"])

    def test_no_completed_answers_skips_all_calls_even_with_tool_results(self):
        calls, reply = self.callback([])
        result = run_research_review("复核", {"Quant_Analyst": {"error": "ConnectionError"}},
                                     reply, tool_evidence=[self.tool])
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "no_completed_sources")
        self.assertEqual(result["stage_calls"], 0)
        self.assertEqual(calls, [])

    def test_partial_source_is_not_promoted_to_completed_evidence(self):
        calls, reply = self.callback([])
        result = run_research_review("复核", {"Quant_Analyst": {"answer": "正在查询", "status": "partial"}}, reply)
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["stage_calls"], 0)
        self.assertIn("Quant_Analyst", result["source_failures"])

    def test_excessive_source_payload_skips_without_truncating_original(self):
        text = "原始材料" * MAX_SOURCE_CHARS
        calls, reply = self.callback([])
        result = run_research_review("复核", {"A_Share_Analyst": {"answer": text}}, reply)
        self.assertEqual(result["reason"], "source_budget_exceeded")
        self.assertEqual(result["sources"][0]["content"], text)
        self.assertEqual(calls, [])

    def test_unknown_source_id_fails_side_and_skips_judge(self):
        calls, reply = self.callback([side(ids=["S999"]), side()])
        result = run_research_review("复核", self.results, reply)
        self.assertEqual(result["stages"]["bull"]["error"], "unknown_source_id")
        self.assertEqual(result["stages"]["bear"]["status"], "completed")
        self.assertEqual(result["stages"]["judge"]["status"], "skipped")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("conditional_synthesis", result["stages"]["judge"])

    def test_missing_or_invalid_citations_fail_without_model_repair_calls(self):
        for refs in ([], "S1", [1]):
            with self.subTest(refs=refs):
                invalid = {"answer": json.dumps({"arguments": [{"claim": "无法引用的观点", "evidence_ids": refs}]})}
                calls, reply = self.callback([invalid, side()])
                result = run_research_review("复核", self.results, reply)
                self.assertEqual(result["stages"]["bull"]["error"], "invalid_schema")
                self.assertEqual(result["stage_calls"], 2)

    def test_callback_failure_is_explicit_and_does_not_expose_exception_body(self):
        calls, reply = self.callback([ConnectionError("private request secret"), side()])
        result = run_research_review("复核", self.results, reply)
        self.assertEqual(result["stages"]["bull"]["error"], "ConnectionError")
        self.assertNotIn("private request secret", json.dumps(result))
        self.assertIn("未完成", result["answer"])
        self.assertEqual(result["stage_calls"], 2)

    def test_two_bad_sides_are_failed_not_successful(self):
        calls, reply = self.callback([{"answer": "非 JSON"}, {"error": "TimeoutError"}])
        result = run_research_review("复核", self.results, reply)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stages"]["bull"]["error"], "invalid_json")
        self.assertEqual(len(calls), 2)

    def test_failed_judge_preserves_both_valid_cases(self):
        calls, reply = self.callback([side("多方有效观点"), side("空方有效观点"), {"answer": "{}"}])
        result = run_research_review("复核", self.results, reply)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["stages"]["judge"]["error"], "invalid_schema")
        self.assertIn("多方有效观点", result["answer"])
        self.assertIn("空方有效观点", result["answer"])
        self.assertIn("裁定阶段未完成", result["answer"])

    def test_judge_cannot_introduce_unknown_evidence(self):
        value = json.loads(judge()["answer"])
        value["contested_claims"][0]["evidence_ids"] = ["UNSEEN"]
        calls, reply = self.callback([side(), side(), {"answer": json.dumps(value)}])
        result = run_research_review("复核", self.results, reply, tool_evidence=[self.tool])
        self.assertEqual(result["stages"]["judge"]["error"], "unknown_source_id")
        self.assertFalse(result["ok"])

    def test_empty_side_cases_and_fenced_json_are_accepted(self):
        value = json.loads(judge()["answer"])
        value["contested_claims"] = []
        value["required_evidence"] = []
        calls, reply = self.callback([
            {"answer": '```json\n{"arguments": []}\n```\nTERMINATE'},
            {"answer": '{"arguments": []}'}, {"answer": json.dumps(value)},
        ])
        result = run_research_review("复核", self.results, reply)
        self.assertTrue(result["ok"])
        self.assertIn("不足以形成", result["answer"])
        self.assertEqual(result["stage_calls"], 3)

    def test_unavailable_source_remains_visible_to_judge_and_user(self):
        self.results["Market_Analyst"] = {"error": "TimeoutError"}
        calls, reply = self.callback([side(), side(), judge()])
        result = run_research_review("复核", self.results, reply, tool_evidence=[self.tool])
        self.assertIn("Market_Analyst", calls[2][1])
        self.assertEqual(result["source_failures"], {"Market_Analyst": "TimeoutError"})
        self.assertIn("未完成的研究分项：Market_Analyst", result["answer"])


if __name__ == "__main__":
    unittest.main()
