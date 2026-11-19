"""Aggregate existing evidence into report-ready methodology metrics.

This module only reads saved artifacts. It never calls a model and never
recomputes a backtest, so its output cannot silently change experimental
results.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {} if default is None else default


def rate(numerator: int | float, denominator: int | float) -> float | None:
    return round(float(numerator) / float(denominator), 4) if denominator else None


def latency_stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "mean": None, "median": None, "min": None, "max": None}
    return {
        "count": len(values),
        "mean": round(statistics.mean(values), 3),
        "median": round(statistics.median(values), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
    }


def _failure_reason(attempt: dict) -> str:
    if not attempt.get("initial_json_valid", False):
        return "json_decode_or_schema"
    if not attempt.get("initial_execution_ok", False):
        error = str(attempt.get("initial_error", ""))
        if "Disallowed" in error:
            return "validator_rejection_investigated"
        return "execution_error"
    return "none"


def generation_metrics(artifacts_root: Path) -> dict[str, Any]:
    root = Path(artifacts_root) / "quant_v2"
    evaluation = read_json(root / "evaluation.json", {})
    attempts = evaluation.get("attempts", [])
    timings = []
    for path in sorted(root.glob("generation_*/timing.json")):
        payload = read_json(path, {})
        if isinstance(payload.get("seconds"), (int, float)):
            timings.append(float(payload["seconds"]))
    tokens = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for path in sorted(root.glob("generation_*/usage.json")):
        payload = read_json(path, {})
        for usage in payload.values():
            if not isinstance(usage, dict):
                continue
            for key in tokens:
                value = usage.get(key)
                if isinstance(value, int):
                    tokens[key] += value
    checks = [attempt.get("checks", {}) for attempt in attempts]
    failures = Counter(_failure_reason(attempt) for attempt in attempts)
    failures.pop("none", None)
    completed = len(attempts)
    return {
        "attempts": completed,
        "initial_json_valid_rate": evaluation.get("initial_json_valid_rate", rate(
            sum(a.get("initial_json_valid", False) for a in attempts), completed)),
        "initial_execution_success_rate": evaluation.get("initial_execution_success_rate", rate(
            sum(a.get("initial_execution_ok", False) for a in attempts), completed)),
        "without_model_repair_success_rate": evaluation.get("without_model_repair_success_rate", rate(
            sum(a.get("without_model_repair_ok", False) for a in attempts), completed)),
        "final_validated_success_rate": evaluation.get("validated_success_rate", rate(
            sum(a.get("ok", False) for a in attempts), completed)),
        "model_repair_requests": evaluation.get("model_repair_requests", sum(
            int(a.get("model_repairs", 0)) for a in attempts)),
        "rule_consistency_rate": rate(sum(bool(c.get("fixed_rule_equal")) for c in checks), len(checks)),
        "prefix_causality_rate": rate(sum(bool(c.get("prefix_causality_passed")) for c in checks), len(checks)),
        "trusted_engine_equality_rate": rate(sum(bool(c.get("trusted_engine_equal")) for c in checks), len(checks)),
        "all_audits_passed": evaluation.get("all_audits_passed"),
        "failure_taxonomy": dict(sorted(failures.items())),
        "latency_seconds": latency_stats(timings),
        "tokens": tokens,
        "evidence": "artifacts/quant_v2/evaluation.json",
    }


def numeric_fidelity(artifacts_root: Path) -> dict[str, Any]:
    root = Path(artifacts_root) / "quant_v2"
    payload = read_json(root / "agent_tool_validation.json", {})
    checks = payload.get("numeric_checks", {})
    return {
        "numeric_checks": checks,
        "passed": sum(bool(value) for value in checks.values()),
        "total": len(checks),
        "accuracy": rate(sum(bool(value) for value in checks.values()), len(checks)),
        "tool_calls": payload.get("tool_calls"),
        "answer": payload.get("answer"),
    }


def dialogue_metrics(artifacts_root: Path) -> dict[str, Any]:
    root = Path(artifacts_root) / "quant_v2"
    payload = read_json(root / "dialogue_online_validation.json", {})
    turns = payload.get("turns", [])
    calls = [call for turn in turns for call in turn.get("tool_calls", [])]
    experiment_ids = [turn.get("answer", {}).get("experiment_id") for turn in turns]
    reuse = bool(len(experiment_ids) >= 2 and experiment_ids[0] and experiment_ids[0] == experiment_ids[1])
    switched = bool(len(experiment_ids) >= 3 and experiment_ids[2] and experiment_ids[2] != experiment_ids[0])
    return {
        "turns": len(turns),
        "successful_turns": sum(bool(turn.get("ok")) for turn in turns),
        "turn_success_rate": rate(sum(bool(turn.get("ok")) for turn in turns), len(turns)),
        "tool_calls": len(calls),
        "tool_names": sorted(set(calls)),
        "cross_turn_experiment_reuse": reuse,
        "parameter_switch_detected": switched,
        "scenario": payload.get("scenario"),
    }


def generalization_metrics(artifacts_root: Path) -> dict[str, Any]:
    root = Path(artifacts_root) / "quant_generalization"
    latest = read_json(root / "latest.json", {})
    run = latest.get("run")
    if not run:
        return {"available": False}
    summary = read_json(Path(artifacts_root).parent / run / "summary.json", {})
    return {
        "available": True,
        "run": run,
        "cases": summary.get("cases"),
        "symbols": summary.get("symbols"),
        "windows": summary.get("windows"),
        "frozen_strategies": summary.get("frozen_strategies"),
        "replays": summary.get("replays"),
        "passed_replays": summary.get("passed_replays"),
        "new_llm_requests": summary.get("new_llm_requests"),
        "investment_alpha_tested": summary.get("investment_alpha_tested"),
        "scope": summary.get("scope"),
    }


def usage_totals(folder: Path) -> dict[str, int]:
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for path in sorted(Path(folder).rglob("usage.json")):
        payload = read_json(path, {})
        for usage in payload.values():
            if not isinstance(usage, dict):
                continue
            for key in totals:
                value = usage.get(key)
                if isinstance(value, int):
                    totals[key] += value
    return totals


def stability_metrics(artifacts_root: Path) -> dict[str, Any]:
    root = Path(artifacts_root) / "stability"
    latest = read_json(root / "latest.json", {})
    run = latest.get("run")
    if not run:
        return {"available": False}
    summary = read_json(Path(artifacts_root).parent / run / "summary.json", {})
    return {
        "available": True,
        "run": run,
        "cases": summary.get("cases"),
        "repeats_per_case": summary.get("repeats_per_case"),
        "attempts": summary.get("attempts"),
        "passed": summary.get("passed"),
        "pass_rate": summary.get("pass_rate"),
        "json_valid_rate": summary.get("json_valid_rate"),
        "rule_consistency_rate": summary.get("rule_consistency_rate"),
        "prefix_causality_rate": summary.get("prefix_causality_rate"),
        "trusted_engine_equality_rate": summary.get("trusted_engine_equality_rate"),
        "rate_denominator": summary.get("rate_denominator"),
        "conditional_check_rates": summary.get("conditional_check_rates"),
        "offline_verification": read_json(Path(artifacts_root).parent / run / "verification_summary.json", {}),
        "latency_seconds": summary.get("latency_seconds"),
        "tokens": usage_totals(Path(artifacts_root).parent / run),
        "failure_taxonomy": summary.get("failure_taxonomy"),
        "per_case": summary.get("per_case"),
        "scope": summary.get("scope"),
    }


def factor_ideation_metrics(artifacts_root: Path) -> dict[str, Any]:
    root = Path(artifacts_root) / "factor_ideation"
    latest = read_json(root / "latest.json", {})
    run = latest.get("run")
    if not run:
        return {"available": False}
    summary = read_json(Path(artifacts_root).parent / run / "summary.json", {})
    per_idea = [
        {"name": idea.get("name"), "signal": idea.get("signal"),
         "generated_times": idea.get("generated_times"),
         "mean_out_of_sample_return": idea.get("mean_out_of_sample_return"),
         "beats_buy_hold_cases": idea.get("beats_buy_hold_cases"),
         "beats_sma_cases": idea.get("beats_sma_cases")}
        for idea in summary.get("per_idea", [])
    ]
    return {
        "available": True,
        "run": run,
        "ideation": summary.get("ideation"),
        "evaluation": {"symbols": (summary.get("evaluation") or {}).get("symbols"),
                       "window": (summary.get("evaluation") or {}).get("window"),
                       "cases_per_idea": (summary.get("evaluation") or {}).get("cases_per_idea")},
        "repeats": summary.get("repeats"),
        "completed_generations": summary.get("completed_generations"),
        "successful_generations": summary.get("successful_generations"),
        "generation_success_rate": summary.get("generation_success_rate"),
        "generation_complete": summary.get("generation_complete"),
        "missing_attempts": summary.get("missing_attempts"),
        "failed_attempts": summary.get("failed_attempts"),
        "total_ideas_requested": summary.get("total_ideas_requested"),
        "idea_delivery_rate": summary.get("idea_delivery_rate"),
        "returned_ideas": summary.get("returned_ideas"),
        "valid_ideas": summary.get("valid_ideas"),
        "idea_validation_rate": summary.get("idea_validation_rate"),
        "unique_valid_signals": summary.get("unique_valid_signals"),
        "duplicate_rate": summary.get("duplicate_rate"),
        "out_of_sample_win_rate_vs_buy_hold": summary.get("out_of_sample_win_rate_vs_buy_hold"),
        "out_of_sample_win_rate_vs_sma": summary.get("out_of_sample_win_rate_vs_sma"),
        "out_of_sample_comparisons": summary.get("out_of_sample_comparisons"),
        "wins_vs_buy_hold": summary.get("wins_vs_buy_hold"),
        "wins_vs_sma": summary.get("wins_vs_sma"),
        "per_idea": per_idea,
        "tokens": usage_totals(Path(artifacts_root).parent / run),
        "scope": summary.get("scope"),
    }


def data_metrics(artifacts_root: Path) -> dict[str, Any]:
    project = Path(artifacts_root).parent
    fixed = read_json(project / "data/quant_v2/metadata.json", {})
    symbols = []
    for path in sorted((project / "data/quant_generalization").glob("*/metadata.json")):
        meta = read_json(path, {})
        symbols.append({"symbol": meta.get("symbol"), "rows": meta.get("output_rows"),
                        "actual_start": meta.get("actual_start"), "actual_end": meta.get("actual_end"),
                        "provider": (meta.get("provider") or {}).get("source"),
                        "clean_sha256": meta.get("clean_sha256")})
    return {
        "fixed_case": {
            "symbol": fixed.get("symbol"),
            "rows": fixed.get("output_rows"),
            "actual_start": fixed.get("actual_start"),
            "actual_end": fixed.get("actual_end"),
            "adjust": fixed.get("adjust"),
            "provider": (fixed.get("provider") or {}).get("source"),
            "duplicate_dates_removed": fixed.get("duplicate_dates_removed"),
            "missing_rows_removed": fixed.get("missing_rows_removed"),
            "missing_dates_policy": fixed.get("missing_dates"),
            "volume_conversion": fixed.get("volume_conversion"),
        },
        "generalization_symbols": symbols,
    }


def build_metrics(artifacts_root: str | Path) -> dict[str, Any]:
    root = Path(artifacts_root)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "saved artifacts only; no new model requests and no backtest recomputation",
        "generation": generation_metrics(root),
        "numeric_fidelity": numeric_fidelity(root),
        "dialogue": dialogue_metrics(root),
        "generalization": generalization_metrics(root),
        "stability": stability_metrics(root),
        "factor_ideation": factor_ideation_metrics(root),
        "data": data_metrics(root),
        "interpretation": {
            "reliability": "Generation metrics measure implementation reliability, not investment alpha.",
            "validity": "Rule-consistency and trusted-engine checks show the generated code matches the specified rule.",
            "generalization": "Replays test frozen code on finite unseen cases; they are not new independent generations.",
            "alpha": "The V2 design does not establish that LLM output improves returns.",
        },
    }


def _pct(value: Any) -> str:
    return "N/A" if value is None else f"{float(value) * 100:.2f}%"


def render_markdown(metrics: dict[str, Any]) -> str:
    generation = metrics["generation"]
    numeric = metrics["numeric_fidelity"]
    dialogue = metrics["dialogue"]
    generalization = metrics["generalization"]
    data = metrics["data"]
    lines = [
        "# V2 智能体评估指标汇总",
        "",
        f"生成时间：{metrics['generated_at']}",
        "",
        "> 本表只汇总仓库中已保存的产物，不发起新的模型请求，也不重算回测。",
        "",
        "## 1. 代码生成可靠性（固定 SMA 任务）",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 独立生成次数 | {generation['attempts']} |",
        f"| 首次 JSON 合格率 | {_pct(generation['initial_json_valid_rate'])} |",
        f"| 首次完整执行成功率 | {_pct(generation['initial_execution_success_rate'])} |",
        f"| 无需模型修正成功率 | {_pct(generation['without_model_repair_success_rate'])} |",
        f"| 修正后最终成功率 | {_pct(generation['final_validated_success_rate'])} |",
        f"| 模型修正请求数 | {generation['model_repair_requests']} |",
        f"| 固定规则一致率 | {_pct(generation['rule_consistency_rate'])} |",
        f"| 前缀因果性通过率 | {_pct(generation['prefix_causality_rate'])} |",
        f"| 与可信引擎一致率 | {_pct(generation['trusted_engine_equality_rate'])} |",
        f"| 账本审计全部通过 | {generation['all_audits_passed']} |",
        "",
        "失败分类：",
        "",
        "| 类型 | 次数 |",
        "| --- | --- |",
    ]
    for name, count in generation["failure_taxonomy"].items():
        lines.append(f"| {name} | {count} |")
    latency = generation["latency_seconds"]
    lines += [
        "",
        f"生成延迟（秒）：均值 {latency['mean']}，中位数 {latency['median']}，"
        f"最小 {latency['min']}，最大 {latency['max']}（n={latency['count']}）。",
        f"累计 token：prompt {generation['tokens']['prompt_tokens']}，"
        f"completion {generation['tokens']['completion_tokens']}，"
        f"total {generation['tokens']['total_tokens']}。",
        "",
        "## 2. 数值引用忠实度",
        "",
        f"- 数值核对通过：{numeric['passed']}/{numeric['total']}（{_pct(numeric['accuracy'])}）。",
        f"- 工具调用次数：{numeric['tool_calls']}。",
        "",
        "## 3. 多轮对话行为",
        "",
        f"- 轮次：{dialogue['turns']}，成功：{dialogue['successful_turns']}（{_pct(dialogue['turn_success_rate'])}）。",
        f"- 工具调用总数：{dialogue['tool_calls']}；工具：{', '.join(dialogue['tool_names'])}。",
        f"- 跨轮实验复用：{dialogue['cross_turn_experiment_reuse']}；参数切换：{dialogue['parameter_switch_detected']}。",
        "",
        "## 4. 冻结代码的有限案例泛化",
        "",
    ]
    if generalization.get("available"):
        lines += [
            f"- 案例数：{generalization['cases']}；标的：{', '.join(generalization['symbols'] or [])}。",
            f"- 冻结代码数：{generalization['frozen_strategies']}；回放次数：{generalization['replays']}；"
            f"通过：{generalization['passed_replays']}。",
            f"- 新增 LLM 请求：{generalization['new_llm_requests']}。",
            f"- 是否检验投资 alpha：{generalization['investment_alpha_tested']}。",
        ]
    else:
        lines.append("- 未找到泛化结果。")
    stability = metrics["stability"]
    factor = metrics["factor_ideation"]
    lines += ["", "## 5. 多案例生成稳定性", ""]
    if stability.get("available"):
        latency = stability.get("latency_seconds") or {}
        lines += [
            "- 总体通过率分母为全部计划尝试，失败和缺失均计入；条件通过率单独保存于 JSON 的 conditional_check_rates。",
            f"- 案例数：{stability['cases']}；每案例重复：{stability['repeats_per_case']}；总尝试：{stability['attempts']}。",
            f"- 通过：{stability['passed']}/{stability['attempts']}（{_pct(stability['pass_rate'])}）；"
            f"JSON 合格率 {_pct(stability['json_valid_rate'])}。",
            f"- 规则一致率 {_pct(stability['rule_consistency_rate'])}；"
            f"前缀因果性 {_pct(stability['prefix_causality_rate'])}；"
            f"可信引擎一致 {_pct(stability['trusted_engine_equality_rate'])}。",
            f"- 延迟（秒）：均值 {latency.get('mean')}，中位数 {latency.get('median')}，"
            f"最小 {latency.get('min')}，最大 {latency.get('max')}（n={latency.get('count')}）。",
            f"- 累计 token：prompt {stability['tokens']['prompt_tokens']}，"
            f"completion {stability['tokens']['completion_tokens']}，"
            f"total {stability['tokens']['total_tokens']}。",
            f"- 失败分类：{stability.get('failure_taxonomy') or '无'}。",
            "",
            "| 案例 | 标的 | 参数 | 尝试 | 通过 | 通过率 | JSON 合格率 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for name, row in (stability.get("per_case") or {}).items():
            lines.append(
                f"| {name} | {row.get('symbol')} | {row.get('fast')}/{row.get('slow')} | "
                f"{row.get('attempts')} | {row.get('passed')} | "
                f"{_pct(row.get('pass_rate'))} | {_pct(row.get('json_valid_rate'))} |")
    else:
        lines.append("- 未找到稳定性结果。")
    lines += ["", "## 6. LLM 因子思路与样本外回放", ""]
    if factor.get("available"):
        ideation = factor.get("ideation") or {}
        evaluation = factor.get("evaluation") or {}
        symbols = ", ".join(evaluation.get("symbols") or [])
        window = evaluation.get("window") or {}
        ideation_window = ideation.get("window") or {}
        lines += [
            f"- 计划生成 {factor['repeats']} 次；已完成 {factor.get('completed_generations')}，"
            f"成功 {factor.get('successful_generations')}，完整性 {factor.get('generation_complete')}；"
            f"缺失 {factor.get('missing_attempts') or []}，失败 {factor.get('failed_attempts') or []}。",
            f"- 返回 {factor['returned_ideas']}，"
            f"有效 {factor['valid_ideas']}（合格率 {_pct(factor['idea_validation_rate'])}），"
            f"去重后 {factor['unique_valid_signals']} 个（重复率 {_pct(factor['duplicate_rate'])}）。",
            f"- 请求想法总数 {factor.get('total_ideas_requested')}；有效想法交付率 "
            f"{_pct(factor.get('idea_delivery_rate'))}；合格率仅以实际返回想法为分母。",
            f"- 想法窗口：{ideation.get('symbol')} / "
            f"{ideation_window.get('start')} 至 {ideation_window.get('end')}；"
            f"回放：{symbols} / {window.get('start')} 至 {window.get('end')}。",
            f"- 样本外相对买入持有胜率：{_pct(factor['out_of_sample_win_rate_vs_buy_hold'])}；"
            f"相对 SMA 胜率：{_pct(factor['out_of_sample_win_rate_vs_sma'])}。",
            f"- 因子与标的比较共 {factor.get('out_of_sample_comparisons')} 个；"
            f"胜买入持有 {factor.get('wins_vs_buy_hold')} 个，胜 SMA {factor.get('wins_vs_sma')} 个。",
            f"- 累计 token：prompt {factor['tokens']['prompt_tokens']}，"
            f"completion {factor['tokens']['completion_tokens']}，"
            f"total {factor['tokens']['total_tokens']}。",
            "",
            "| 因子 | 表达式 | 生成次数 | 平均样本外收益 | 胜买入持有 | 胜SMA |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for idea in factor.get("per_idea", []):
            lines.append(
                f"| {idea.get('name')} | `{idea.get('signal')}` | {idea.get('generated_times')} | "
                f"{_pct(idea.get('mean_out_of_sample_return'))} | "
                f"{idea.get('beats_buy_hold_cases')} | {idea.get('beats_sma_cases')} |")
    else:
        lines.append("- 未找到因子思路结果。")
    fixed = data["fixed_case"]
    lines += [
        "",
        "## 7. 数据覆盖与预处理记录",
        "",
        "| 项目 | 数值 |",
        "| --- | --- |",
        f"| 固定案例标的 | {fixed['symbol']} |",
        f"| 固定案例观测数 | {fixed['rows']} |",
        f"| 固定案例区间 | {fixed['actual_start']} 至 {fixed['actual_end']} |",
        f"| 复权方式 | {fixed['adjust']} |",
        f"| 数据来源 | {fixed['provider']} |",
        f"| 去重行数 | {fixed['duplicate_dates_removed']} |",
        f"| 删除不完整行 | {fixed['missing_rows_removed']} |",
        f"| 缺失日期策略 | {fixed['missing_dates_policy']} |",
        "",
        "## 8. 报告写法提醒",
        "",
        "- 上述“首次成功率”与“修正后成功率”必须分开写，不能只写最终 100%。",
        "- 代码生成指标证明的是实现可靠性，不是投资收益。",
        "- 45 次回放是冻结代码的有限案例复现，不是 45 次独立生成。",
        "- 泛化结果与买入持有互有胜负，不能据此宣称 LLM 产生 alpha。",
        "",
    ]
    return "\n".join(lines)
