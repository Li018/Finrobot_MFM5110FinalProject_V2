"""LLM factor-idea generation with an out-of-sample replay.

Stage ``generate`` asks the model for factor expressions using only the
ideation window. Stage ``evaluate`` replays the frozen expressions on an
untouched evaluation window and compares them with SMA and buy-and-hold.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ["DEBUG"] = "false"
os.environ["NLTK_DATA"] = str(ROOT / ".cache/nltk_data")

import pandas as pd

from finrobot.agents.prompts import quant_factor_ideation_prompt
from finrobot.functional.factor_dsl import history_requirements, signal_from_expression, validate_ideas
from finrobot.functional.quantitative import (
    audit_backtest,
    reference_signals,
    run_signal_backtest,
    save_backtest,
)

DATA_ROOT = ROOT / "data/quant_generalization"
OUT_ROOT = ROOT / "artifacts/factor_ideation"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dataset_folder(spec, symbol):
    return DATA_ROOT / (
        f"{symbol}_{pd.Timestamp(spec['data_start']):%Y%m%d}_"
        f"{pd.Timestamp(spec['data_end']):%Y%m%d}_{spec['adjust'] or 'raw'}"
    )


def load_snapshot(spec, symbol):
    folder = dataset_folder(spec, symbol)
    meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    if digest(folder / "prices.csv") != meta["clean_sha256"]:
        raise ValueError("Snapshot hash mismatch: " + symbol)
    data = pd.read_csv(folder / "prices.csv", index_col="Date", parse_dates=True)
    return data, meta


def slice_window(data, window, warmup, *, use_all_history=False):
    evaluated = data.loc[window["start"]:window["end"]]
    if len(evaluated) < 2:
        raise ValueError("Too few evaluation observations")
    history = data.loc[data.index < evaluated.index[0]]
    if len(history) < warmup:
        raise ValueError(f"Insufficient historical warm-up: need {warmup}, got {len(history)}")
    if not use_all_history:
        history = history.tail(warmup)
    return pd.concat([history, evaluated]), evaluated


def data_summary(spec, symbol, data):
    """Only the ideation window is described; evaluation data is never shown."""
    window = spec["ideation_window"]
    view = data.loc[window["start"]:window["end"]]
    close = view["Close"].astype(float)
    return {
        "symbol": symbol,
        "ideation_window": {"requested": window, "actual_start": str(view.index[0].date()),
                            "actual_end": str(view.index[-1].date()), "rows": len(view)},
        "columns": list(view.columns),
        "price_summary": {
            "close_min": round(float(close.min()), 4),
            "close_max": round(float(close.max()), 4),
            "close_mean": round(float(close.mean()), 4),
            "close_std": round(float(close.std()), 4),
            "volume_mean": round(float(view["Volume"].mean()), 2),
        },
        "backtest_assumptions": spec["base_config"],
        "constraints": {
            "data_available": "daily OHLCV only, forward-adjusted",
            "signal_semantics": "True means long, False means flat; execution is next observed open",
            "no_future_data": True,
            "no_parameter_search": "The model proposes a small fixed set of economically motivated ideas, not an optimized grid.",
        },
        "evaluation_policy": "The evaluation window and symbols are withheld from the model and are used only for frozen replay.",
    }


def build_prompt(spec, data):
    summary = data_summary(spec, spec["ideation_symbol"], data)
    return quant_factor_ideation_prompt.format(
        idea_count=spec["idea_count"],
        data_summary=json.dumps(summary, ensure_ascii=False),
    )


def output_root(spec, prompt, out_root=OUT_ROOT):
    identity = json.dumps({"spec": spec, "prompt": prompt, "model": spec["model"]}, sort_keys=True)
    return Path(out_root) / (spec["name"] + "_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12])


def replay_root(spec, prompt, out_root=OUT_ROOT):
    """Use the frozen run when only the generation prompt has evolved."""
    latest_path = Path(out_root) / "latest.json"
    if latest_path.exists():
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
        run = (ROOT / latest["run"]).resolve()
        if run.parent != Path(out_root).resolve():
            raise ValueError("Latest factor run points outside the selected output directory")
        inputs = json.loads((run / "inputs.json").read_text(encoding="utf-8"))
        if inputs.get("spec") == spec:
            return run
    return output_root(spec, prompt, out_root).resolve()


def redact(message, configs):
    for entry in configs:
        message = message.replace(entry.get("api_key", "") or "<unused>", "[REDACTED]")
    return message


def validate_spec(spec):
    if spec["repeats"] < 1 or spec["idea_count"] < 1:
        raise ValueError("Repeats and ideas per request must be positive")
    if not spec["evaluation_symbols"] or len(set(spec["evaluation_symbols"])) != len(spec["evaluation_symbols"]):
        raise ValueError("Evaluation symbols must be nonempty and unique")
    for window in (spec["ideation_window"], spec["evaluation_window"]):
        if pd.Timestamp(window["start"]) > pd.Timestamp(window["end"]):
            raise ValueError("Window start must not follow its end")
    if pd.Timestamp(spec["ideation_window"]["end"]) >= pd.Timestamp(spec["evaluation_window"]["start"]):
        raise ValueError("Out-of-sample evaluation must start after the ideation window")


def generate(spec, resume, out_root=OUT_ROOT):
    validate_spec(spec)
    import autogen
    from finrobot.agents.workflow import SingleAssistant

    data, _ = load_snapshot(spec, spec["ideation_symbol"])
    # Expressions are not known until the response arrives. Retain all past
    # observations for their validation, including a stable EMA initialization.
    frame, evaluated = slice_window(data, spec["ideation_window"], spec["warmup_observations"],
                                    use_all_history=True)
    warmup = len(frame) - len(evaluated)
    prompt = build_prompt(spec, data)
    out = output_root(spec, prompt, out_root)
    if resume and replay_root(spec, prompt, out_root) != out.resolve():
        raise ValueError("Generation prompt changed; use evaluate for frozen replay or a new --out-root")
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "inputs.json", {"spec": spec, "prompt": prompt, "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()})
    request_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    configs = None
    outcomes = []
    for number in range(1, spec["repeats"] + 1):
        folder = out / f"ideation_{number:02d}"
        if resume and (folder / "ideas.json").exists():
            saved = json.loads((folder / "request.json").read_text(encoding="utf-8"))
            if saved.get("request_sha256") != request_sha256:
                raise ValueError("Resume inputs changed; use a new experiment directory")
            outcomes.append(json.loads((folder / "ideas.json").read_text(encoding="utf-8"))["ok"])
            print("resume: preserving", folder.name, flush=True)
            continue
        if (folder / "ideas.json").exists():
            raise FileExistsError("Attempt already exists; use --resume to preserve it")
        if configs is None:
            configs = autogen.config_list_from_json(
                str(ROOT / "finrobot_autogen/OAI_CONFIG_LIST"),
                filter_dict={"model": [spec["model"]]},
            )
            if not configs or "REPLACE_ME" in configs[0].get("api_key", ""):
                raise ValueError("Configure a local API key first")
        folder.mkdir(parents=True, exist_ok=True)
        write_json(folder / "request.json", {"model": spec["model"], "temperature": 0, "cache_seed": None,
                   "max_tokens": 3000, "prompt": prompt, "request_sha256": request_sha256})
        started = time.perf_counter()
        try:
            agent = SingleAssistant("Quant_Analyst", llm_config={"config_list": configs, "cache_seed": None,
                "temperature": 0, "max_tokens": 3000, "timeout": 90, "max_retries": 0,
                "tool_choice": "none", "response_format": {"type": "json_object"}}, code_execution_config=False)
            response = agent.assistant.generate_reply(messages=[{"role": "user", "content": prompt}])
            raw = response if isinstance(response, str) else response.get("content", "")
            (folder / "raw_response.txt").write_text(raw or "", encoding="utf-8")
            answer = json.loads(raw)
            ideas = answer.get("ideas")
            if not isinstance(ideas, list) or not ideas:
                raise ValueError("Response does not contain a non-empty ideas array")
            validated, errors = validate_ideas(ideas, frame, warmup)
            write_json(folder / "ideas.json", {"ok": len(validated) == spec["idea_count"] and not errors,
                       "returned_ideas": len(ideas),
                       "valid_ideas": len(validated), "ideas": validated, "errors": errors})
            usage = agent.assistant.client.actual_usage_summary or {}
            write_json(folder / "usage.json", {key: {k: v for k, v in value.items() if k != "cost"}
                       for key, value in usage.items() if isinstance(value, dict)})
        except Exception as exc:
            write_json(folder / "ideas.json", {"ok": False, "error_type": type(exc).__name__,
                       "error": redact(str(exc), configs)})
        saved = json.loads((folder / "ideas.json").read_text(encoding="utf-8"))
        outcomes.append(saved["ok"])
        write_json(folder / "timing.json", {"seconds": round(time.perf_counter() - started, 3)})
        print(json.dumps({"attempt": number, "ok": saved["ok"],
                          "valid_ideas": saved.get("valid_ideas", 0)}), flush=True)
    return all(outcomes)


def unique_ideas(out, spec, frame, warmup):
    """Revalidate expected attempts; missing attempts cannot disappear from the summary."""
    unique = {}
    duplicates = 0
    attempts = []
    for number in range(1, spec["repeats"] + 1):
        name = f"ideation_{number:02d}"
        path = out / name / "ideas.json"
        row = {"attempt": name, "completed": path.exists(), "ok": False,
               "returned_ideas": 0, "valid_ideas": 0, "errors": []}
        attempts.append(row)
        if not path.exists():
            row["errors"] = [{"error": "missing_attempt"}]
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            validated, errors = validate_ideas(payload.get("ideas", []), frame, warmup)
            row["returned_ideas"] = payload.get("returned_ideas", len(payload.get("ideas", [])))
            row["valid_ideas"] = len(validated)
            row["errors"] = payload.get("errors", []) + errors
            if payload.get("error"):
                row["errors"].append({"error": payload["error"]})
            row["ok"] = (bool(payload.get("ok")) and not row["errors"]
                         and row["returned_ideas"] == spec["idea_count"]
                         and len(validated) == spec["idea_count"])
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            row["errors"] = [{"error_type": type(exc).__name__, "error": str(exc)}]
            continue
        for idea in validated:
            key = " ".join(idea["signal"].split())
            if key in unique:
                unique[key]["generated_times"] += 1
                duplicates += 1
            else:
                unique[key] = {**idea, "generated_times": 1, "source_attempts": []}
            unique[key]["source_attempts"].append(name)
    return list(unique.values()), duplicates, attempts


def case_config(spec, symbol, evaluated):
    return {**spec["base_config"], "symbol": symbol,
            "evaluation_start": str(evaluated.index[0].date())}


def run_case(expression, data, window, symbol, spec, folder):
    requirements = history_requirements(expression)
    warmup = max(spec["warmup_observations"], spec["base_config"]["slow"] - 1,
                 requirements["minimum_observations"])
    # EMA has an infinite recursive tail. Use the full frozen snapshot prefix
    # rather than silently reseeding it at each evaluation-window boundary.
    frame, evaluated = slice_window(data, window, warmup, use_all_history=requirements["uses_ema"])
    cfg = case_config(spec, symbol, evaluated)
    signal = signal_from_expression(expression, frame, evaluation_start=evaluated.index[0])
    result = run_signal_backtest(frame, signal, cfg)
    audit = audit_backtest(result, cfg, frame)
    sma = run_signal_backtest(frame, reference_signals(frame, cfg["fast"], cfg["slow"]), cfg)
    hold = run_signal_backtest(frame, pd.Series(1, index=frame.index), cfg)
    save_backtest(result, folder / "idea")
    save_backtest(sma, folder / "sma")
    save_backtest(hold, folder / "buy_hold")
    return {
        "symbol": symbol,
        "window": window,
        "observations": int(len(evaluated)),
        "history": {
            "required_observations": requirements["minimum_observations"],
            "used_observations": len(frame) - len(evaluated),
            "start": str(frame.index[0].date()),
            "ema_initialization": "first valid observation of the full snapshot; adjust=False"
                                  if requirements["uses_ema"] else None,
        },
        "idea": result["metrics"],
        "sma": sma["metrics"],
        "buy_hold": hold["metrics"],
        "audit": audit,
        "long_fraction": round(float(signal.loc[evaluated.index].mean()), 4),
    }


def slug(name, expression):
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")[:40] or "idea"
    return safe + "_" + hashlib.sha256(expression.encode("utf-8")).hexdigest()[:8]


def evaluate(spec, out_root=OUT_ROOT):
    validate_spec(spec)
    data_ideation, _ = load_snapshot(spec, spec["ideation_symbol"])
    prompt = build_prompt(spec, data_ideation)
    out = replay_root(spec, prompt, out_root)
    frame_ideation, evaluated_ideation = slice_window(
        data_ideation, spec["ideation_window"], spec["warmup_observations"], use_all_history=True)
    ideas, duplicates, attempts = unique_ideas(
        out, spec, frame_ideation, len(frame_ideation) - len(evaluated_ideation))
    write_json(out / "idea_revalidation.json", {"attempts": attempts, "api_calls_requested": 0})

    evaluation = out / "evaluation"
    baseline_cases = {}
    for symbol in spec["evaluation_symbols"]:
        data, _ = load_snapshot(spec, symbol)
        frame, evaluated = slice_window(data, spec["evaluation_window"],
                                        max(spec["warmup_observations"], spec["base_config"]["slow"] - 1))
        cfg = case_config(spec, symbol, evaluated)
        sma = run_signal_backtest(frame, reference_signals(frame, cfg["fast"], cfg["slow"]), cfg)
        hold = run_signal_backtest(frame, pd.Series(1, index=frame.index), cfg)
        baseline_cases[symbol] = {"sma": sma["metrics"], "buy_hold": hold["metrics"],
                                  "observations": int(len(evaluated))}
        save_backtest(sma, evaluation / "_baselines" / symbol / "sma")
        save_backtest(hold, evaluation / "_baselines" / symbol / "buy_hold")

    per_idea = []
    for idea in ideas:
        name = slug(idea["name"], idea["signal"])
        in_sample = run_case(idea["signal"], data_ideation, spec["ideation_window"],
                             spec["ideation_symbol"], spec, evaluation / name / "in_sample")
        out_of_sample = {}
        for symbol in spec["evaluation_symbols"]:
            data, _ = load_snapshot(spec, symbol)
            out_of_sample[symbol] = run_case(idea["signal"], data, spec["evaluation_window"],
                                             symbol, spec, evaluation / name / symbol)
        returns = [row["idea"]["total_return"] for row in out_of_sample.values()]
        beats_hold = sum(row["idea"]["total_return"] > row["buy_hold"]["total_return"]
                         for row in out_of_sample.values())
        beats_sma = sum(row["idea"]["total_return"] > row["sma"]["total_return"]
                        for row in out_of_sample.values())
        per_idea.append({
            "name": idea["name"],
            "signal": idea["signal"],
            "hypothesis": idea["hypothesis"],
            "parameters": idea["parameters"],
            "risk_notes": idea["risk_notes"],
            "generated_times": idea["generated_times"],
            "in_sample": in_sample,
            "out_of_sample": out_of_sample,
            "mean_out_of_sample_return": round(sum(returns) / len(returns), 6),
            "beats_buy_hold_cases": beats_hold,
            "beats_sma_cases": beats_sma,
            "evaluated_cases": len(out_of_sample),
        })

    cases = len(spec["evaluation_symbols"])
    comparisons = len(per_idea) * cases
    wins_hold = sum(idea["beats_buy_hold_cases"] for idea in per_idea)
    wins_sma = sum(idea["beats_sma_cases"] for idea in per_idea)
    returned = sum(row["returned_ideas"] for row in attempts)
    valid = sum(row["valid_ideas"] for row in attempts)
    successful = sum(row["ok"] for row in attempts)
    complete = successful == spec["repeats"]
    summary = {
        "ok": complete and bool(per_idea) and all(
            idea["in_sample"]["audit"]["ok"] and all(case["audit"]["ok"] for case in idea["out_of_sample"].values())
            for idea in per_idea),
        "design": spec["design"],
        "limits": spec["limits"],
        "model": spec["model"],
        "repeats": spec["repeats"],
        "completed_generations": sum(row["completed"] for row in attempts),
        "successful_generations": successful,
        "generation_success_rate": round(successful / spec["repeats"], 4),
        "generation_complete": complete,
        "missing_attempts": [row["attempt"] for row in attempts if not row["completed"]],
        "failed_attempts": [row["attempt"] for row in attempts if row["completed"] and not row["ok"]],
        "idea_count_requested": spec["idea_count"],
        "total_ideas_requested": spec["repeats"] * spec["idea_count"],
        "returned_ideas": returned,
        "valid_ideas": valid,
        "idea_validation_rate": round(valid / returned, 4) if returned else None,
        "idea_delivery_rate": round(valid / (spec["repeats"] * spec["idea_count"]), 4),
        "unique_valid_signals": len(ideas),
        "duplicate_ideas": duplicates,
        "duplicate_rate": round(duplicates / valid, 4) if valid else None,
        "ideation": {"symbol": spec["ideation_symbol"], "window": spec["ideation_window"]},
        "evaluation": {"symbols": spec["evaluation_symbols"], "window": spec["evaluation_window"],
                       "cases_per_idea": cases, "baselines": baseline_cases},
        "out_of_sample_comparisons": comparisons,
        "wins_vs_buy_hold": wins_hold,
        "wins_vs_sma": wins_sma,
        "out_of_sample_win_rate_vs_buy_hold": round(wins_hold / comparisons, 4) if comparisons else None,
        "out_of_sample_win_rate_vs_sma": round(wins_sma / comparisons, 4) if comparisons else None,
        "per_idea": per_idea,
        "scope": "Finite-case out-of-sample feasibility check of LLM-proposed factors; not evidence of investment alpha.",
        "evaluation_time": datetime.now(timezone.utc).isoformat(),
    }
    write_json(out / "summary.json", summary)
    rows = []
    for idea in per_idea:
        row = {"name": idea["name"], "signal": idea["signal"],
               "generated_times": idea["generated_times"],
               "in_sample_return": idea["in_sample"]["idea"]["total_return"],
               "mean_out_of_sample_return": idea["mean_out_of_sample_return"],
               "beats_buy_hold_cases": idea["beats_buy_hold_cases"],
               "beats_sma_cases": idea["beats_sma_cases"]}
        for symbol, case in idea["out_of_sample"].items():
            row[f"{symbol}_return"] = case["idea"]["total_return"]
            row[f"{symbol}_buy_hold"] = case["buy_hold"]["total_return"]
            row[f"{symbol}_sma"] = case["sma"]["total_return"]
        rows.append(row)
    pd.DataFrame(rows).to_csv(out / "comparison.csv", index=False)
    plot_results(out, per_idea, baseline_cases, spec)
    write_json(Path(out_root) / "latest.json", {"run": relative_label(out),
               "summary_sha256": digest(out / "summary.json")})
    print(json.dumps({key: value for key, value in summary.items()
                      if key not in ("per_idea", "evaluation")}), flush=True)
    print("Results:", relative_label(out), flush=True)
    return summary["ok"]


def relative_label(path):
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def plot_results(out, per_idea, baselines, spec):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    labels = [idea["name"] for idea in per_idea]
    values = [idea["mean_out_of_sample_return"] * 100 for idea in per_idea]
    hold = [baselines[symbol]["buy_hold"]["total_return"] * 100 for symbol in spec["evaluation_symbols"]]
    sma = [baselines[symbol]["sma"]["total_return"] * 100 for symbol in spec["evaluation_symbols"]]
    y = range(len(labels))
    fig, ax = plt.subplots(figsize=(11, max(4, 0.7 * len(labels) + 2)))
    ax.barh(list(y), values, color="#4C78A8", label="LLM factor, mean OOS")
    if not per_idea:
        ax.text(0.5, 0.5, "No valid factors; generation incomplete", transform=ax.transAxes, ha="center")
    ax.axvline(sum(hold) / len(hold), color="#E45756", linestyle="--", label="Buy-and-hold mean")
    ax.axvline(sum(sma) / len(sma), color="#54A24B", linestyle=":", label="SMA mean")
    ax.set_yticks(list(y), labels)
    ax.axvline(0, color="gray", linewidth=0.7)
    ax.set_xlabel("Out-of-sample total return (%)")
    ax.set_title("LLM-proposed factors: ideation 2021-2023, evaluation 2024-2025")
    ax.grid(axis="x", alpha=0.25)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out / "comparison.png", dpi=160)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["generate", "evaluate", "all"])
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("factor_ideation_config.json"))
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    spec = json.loads(args.config.read_text(encoding="utf-8"))
    validate_spec(spec)
    started = time.perf_counter()
    ok = True
    args.out_root.mkdir(parents=True, exist_ok=True)
    write_json(args.out_root / "declared_design.json", spec)
    if args.stage in ("generate", "all"):
        ok = generate(spec, args.resume, args.out_root)
    if args.stage in ("evaluate", "all"):
        ok = evaluate(spec, args.out_root) and ok
    print("Elapsed seconds:", round(time.perf_counter() - started, 3), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
