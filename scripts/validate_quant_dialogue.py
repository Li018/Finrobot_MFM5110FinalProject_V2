"""Offline tool validation or opt-in real multi-turn CLI validation."""
import argparse
import contextlib
import json
import os
import re
import runpy
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["DEBUG"] = "false"
os.environ["NLTK_DATA"] = str(ROOT / ".cache/nltk_data")


def parse_answer(history, required):
    for message in reversed(history):
        text = message.get("content") or ""
        try:
            value = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        except (AttributeError, ValueError):
            continue
        if required in value:
            return value
    raise ValueError("Model did not return the requested final JSON")


def validate_offline():
    from finrobot.functional.quant_dialogue import QuantDialogueTools as Tools, OUT_ROOT
    first = json.loads(Tools.prepare_quant_experiment("000651", "2023-01-01", "2024-12-31", 10, 30, 100000.0, .6))
    identifier = first["experiment_id"]
    comparison = json.loads(Tools.compare_quant_strategies(identifier))
    risk = json.loads(Tools.check_quant_risk(identifier, "sma"))
    source = ROOT / "artifacts/quant_v2/generation_01/strategy.py"
    generated = json.loads(Tools.execute_quant_code(identifier, source.read_text(encoding="utf-8")))
    if not generated["ok"]:
        raise RuntimeError("Saved strategy failed in dialogue worker: " + generated.get("error", "unknown"))
    generated_risk = json.loads(Tools.check_quant_risk(identifier, generated["strategy"]))
    assert comparison["metrics"]["sma"] == generated["metrics"]
    assert risk["ok"] and generated_risk["ok"]
    second = json.loads(Tools.prepare_quant_experiment("600031", "2025-01-01", "2025-12-31", 20, 60, 100000.0, .5))
    assert second["experiment_id"] != identifier
    second_comparison = json.loads(Tools.compare_quant_strategies(second["experiment_id"]))
    second_risk = json.loads(Tools.check_quant_risk(second["experiment_id"], "sma"))
    assert second_risk["ok"]
    result = {"ok": True, "api_calls_requested": 0, "reused_model_source": str(source.relative_to(ROOT)),
              "scope": "Offline replay of the dialogue tools; not a new model generation or conversation.",
              "cases": [{"experiment_id": identifier, "symbol": "000651", "fast": 10, "slow": 30,
                         "metrics": comparison["metrics"], "generated_equal": True, "risk_ok": True},
                        {"experiment_id": second["experiment_id"], "symbol": "600031", "fast": 20, "slow": 60,
                         "metrics": second_comparison["metrics"], "risk_ok": True}]}
    path = OUT_ROOT / "validation/offline_validation.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result), flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--online", action="store_true", help="Explicitly allow real DeepSeek requests")
    parser.add_argument("--offline", action="store_true", help="Replay dialogue tools with saved data/code; no API key needed")
    args = parser.parse_args()
    if args.offline and not args.online:
        return validate_offline()
    if not args.online or args.offline:
        parser.error("Choose exactly one of --offline or --online")
    import autogen
    from finrobot.agents.workflow import SingleAssistant
    from finrobot.functional.quant_dialogue import DATA_ROOT, OUT_ROOT, load_experiment
    ask = runpy.run_path(str(ROOT / "finrobot_autogen/run_agent.py"))["ask"]
    configs = autogen.config_list_from_json(str(ROOT / "finrobot_autogen/OAI_CONFIG_LIST"), filter_dict={"model": ["deepseek-flash"]})
    agent = SingleAssistant("Quant_Analyst", llm_config={"config_list": configs, "temperature": 0, "cache_seed": None,
        "max_tokens": 3500, "timeout": 90, "max_retries": 0}, code_execution_config=False,
        max_consecutive_auto_reply=10, is_termination_msg=lambda x: "TERMINATE" in (x.get("content") or ""))
    folder = OUT_ROOT / "validation" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    folder.mkdir(parents=True)
    prompts = [
        "请用000651、2023-01-01至2024-12-31、10/30均线、本金100000、开仓预算60%，在对话中准备新实验，比较SMA和买入持有，调用工具核查SMA风险。最后只返回JSON，字段experiment_id、sma_total_return、buy_hold_total_return、risk_ok、termination（值TERMINATE）。收益填工具返回的原始小数。",
        "继续刚才的实验。请现在生成并运行同规则Python策略代码，然后调用工具核查生成策略风险。最后只返回JSON，字段experiment_id、generated_strategy、generated_total_return、risk_ok、termination（值TERMINATE）。收益填原始小数，不要复用以前保存的生成文件。",
        "现在换成600031、2025-01-01至2025-12-31、20/60均线、本金100000、开仓预算50%。请准备另一个实验、比较SMA和买入持有并检查SMA风险。最后只返回JSON，字段experiment_id、sma_total_return、buy_hold_total_return、risk_ok、termination（值TERMINATE）。"
    ]
    results = []
    first_id = None
    previous_length = 0
    for number, prompt in enumerate(prompts, 1):
        print(json.dumps({"turn": number, "status": "calling model"}), flush=True)
        with (folder / f"turn_{number}.log").open("w", encoding="utf-8") as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            chat = ask(agent, prompt, clear_history=number == 1)
        (folder / f"turn_{number}_history.json").write_text(json.dumps(chat.chat_history, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        calls = [call["function"]["name"] for message in chat.chat_history[previous_length:]
                 for call in message.get("tool_calls", [])]
        previous_length = len(chat.chat_history)
        required = {"execute_quant_code", "check_quant_risk"} if number == 2 else {"prepare_quant_experiment", "compare_quant_strategies", "check_quant_risk"}
        assert required.issubset(calls), calls
        answer = parse_answer(chat.chat_history, "generated_strategy" if number == 2 else "sma_total_return")
        identifier = answer["experiment_id"]
        _, meta = load_experiment(identifier)
        cfg = meta["config"]
        if number == 1:
            first_id = identifier
            assert (cfg["symbol"], cfg["fast"], cfg["slow"], cfg["max_position"], cfg["start_date"], cfg["end_date"]) == ("000651", 10, 30, .6, "2023-01-01", "2024-12-31")
        elif number == 2:
            assert identifier == first_id
        else:
            assert identifier != first_id
            assert (cfg["symbol"], cfg["fast"], cfg["slow"], cfg["max_position"]) == ("600031", 20, 60, .5)
        if number != 2:
            for name in ("sma", "buy_hold"):
                actual = json.loads((OUT_ROOT / identifier / name / "result.json").read_text())["metrics"]["total_return"]
                assert abs(answer[name + "_total_return"] - actual) < 1e-8
        else:
            name = answer["generated_strategy"]
            actual = json.loads((OUT_ROOT / identifier / name / "result.json").read_text())["metrics"]["total_return"]
            assert abs(answer["generated_total_return"] - actual) < 1e-8
            assert (OUT_ROOT / identifier / name / "strategy.py").exists()
        assert answer["risk_ok"] is True
        results.append({"turn": number, "ok": True, "tool_calls": calls, "answer": answer})
        print(json.dumps(results[-1], ensure_ascii=True), flush=True)
    output = {"ok": True, "turns": results, "scenario": "User parameters, multi-turn code generation, then a different stock and period through run_agent.ask"}
    (folder / "validation.json").write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (OUT_ROOT / "validation/latest.json").write_text(json.dumps({"path": str((folder / "validation.json").relative_to(ROOT))}, indent=2) + "\n", encoding="utf-8")
    print("Evidence:", folder, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
