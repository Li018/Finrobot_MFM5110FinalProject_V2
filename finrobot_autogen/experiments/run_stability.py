"""Repeat the fixed-SMA generation task across cases for stability evidence.

Every attempt is an independent model request. The runner stores raw responses,
timing, usage, generated code and execution results, then reports JSON validity,
first-pass execution, repair-free success and failure taxonomy per case.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ["DEBUG"] = "false"
os.environ["NLTK_DATA"] = str(ROOT / ".cache/nltk_data")

import pandas as pd

from finrobot.agents.prompts import quant_generation_prompt

DATA_ROOT = ROOT / "data/quant_generalization"
OUT_ROOT = ROOT / "artifacts/stability"


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
    return pd.read_csv(folder / "prices.csv", index_col="Date", parse_dates=True)


def case_frame(data, case, spec):
    evaluated = data.loc[case["start"]:case["end"]]
    if len(evaluated) < 2:
        raise ValueError("Too few evaluation observations: " + case["name"])
    history = data.loc[data.index < evaluated.index[0]].tail(case["slow"])
    if len(history) < case["slow"]:
        raise ValueError("Insufficient warm-up: " + case["name"])
    frame = pd.concat([history, evaluated])
    frame.index.name = "Date"
    cfg = {**spec["base_config"], "symbol": case["symbol"], "start_date": case["start"],
           "end_date": case["end"], "fast": case["fast"], "slow": case["slow"],
           "evaluation_start": str(evaluated.index[0].date())}
    return frame, evaluated, cfg


def prepare_case(case_dir, case, frame, evaluated, cfg, spec):
    case_dir.mkdir(parents=True, exist_ok=True)
    data_path = case_dir / "data.csv"
    config_path = case_dir / "config.json"
    if not data_path.exists():
        frame.to_csv(data_path, float_format="%.12g")
    elif data_path.read_text(encoding="utf-8").replace("\r\n", "\n") != frame.to_csv(float_format="%.12g").replace("\r\n", "\n"):
        raise ValueError("Case data changed; use a new output directory: " + case["name"])
    if not config_path.exists():
        write_json(config_path, cfg)
    recorded = json.loads((case_dir / "config.json").read_text(encoding="utf-8"))
    if recorded != cfg:
        raise ValueError("Case config changed; use a new output directory: " + case["name"])
    summary = {
        "metadata": {"symbol": case["symbol"], "rows": int(len(evaluated)),
                     "actual_start": str(evaluated.index[0].date()),
                     "actual_end": str(evaluated.index[-1].date()),
                     "adjust": cfg.get("adjust", "qfq")},
        "columns": list(frame.columns),
        "config": cfg,
        "assumptions": spec.get("assumptions", []),
    }
    write_json(case_dir / "summary.json", summary)
    return {"summary": summary, "data_sha256": digest(data_path)}


def output_root(spec, out_root=OUT_ROOT):
    identity = json.dumps({"spec": spec, "model": spec["model"]}, sort_keys=True)
    return Path(out_root) / (spec["name"] + "_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12])


def redact(message, configs):
    for entry in configs:
        message = message.replace(entry.get("api_key", "") or "<unused>", "[REDACTED]")
    return message


def generate(spec, resume, out_root=OUT_ROOT):
    import autogen
    from finrobot.agents.workflow import SingleAssistant

    out = output_root(spec, out_root)
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "inputs.json", {"spec": spec, "model": spec["model"],
               "design": spec.get("design"), "limits": spec.get("limits")})
    snapshots = {case["symbol"]: load_snapshot(spec, case["symbol"]) for case in spec["cases"]}
    configs = None
    outcomes = []
    for case in spec["cases"]:
        frame, evaluated, cfg = case_frame(snapshots[case["symbol"]], case, spec)
        case_dir = out / case["name"]
        prepared = prepare_case(case_dir, case, frame, evaluated, cfg, spec)
        prompt = quant_generation_prompt.format(
            data_summary=json.dumps(prepared["summary"], ensure_ascii=False))
        request_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        for number in range(1, spec["repeats"] + 1):
            folder = case_dir / f"generation_{number:02d}"
            if resume and (folder / "execution.json").exists():
                request = json.loads((folder / "request.json").read_text(encoding="utf-8"))
                if request.get("request_sha256") != request_sha256 or request.get("data_sha256") != prepared["data_sha256"]:
                    raise ValueError("Resume inputs changed; use a new output directory")
                outcomes.append(json.loads((folder / "execution.json").read_text(encoding="utf-8"))["ok"])
                print("resume:", case["name"], number, flush=True)
                continue
            if (folder / "execution.json").exists():
                raise FileExistsError("Attempt already exists; use --resume: " + str(folder))
            if configs is None:
                configs = autogen.config_list_from_json(
                    str(ROOT / "finrobot_autogen/OAI_CONFIG_LIST"),
                    filter_dict={"model": [spec["model"]]})
                if not configs or "REPLACE_ME" in configs[0].get("api_key", ""):
                    raise ValueError("Configure a local API key first")
            folder.mkdir(parents=True, exist_ok=True)
            write_json(folder / "request.json", {"model": spec["model"], "temperature": 0,
                       "cache_seed": None, "max_tokens": 3000, "prompt": prompt,
                       "request_sha256": request_sha256, "data_sha256": prepared["data_sha256"]})
            started = time.perf_counter()
            try:
                agent = SingleAssistant("Quant_Analyst", llm_config={"config_list": configs,
                    "cache_seed": None, "temperature": 0, "max_tokens": 3000, "timeout": 90,
                    "max_retries": 0, "tool_choice": "none",
                    "response_format": {"type": "json_object"}}, code_execution_config=False)
                response = agent.assistant.generate_reply(messages=[{"role": "user", "content": prompt}])
                raw = response if isinstance(response, str) else response.get("content", "")
                (folder / "raw_response.txt").write_text(raw or "", encoding="utf-8")
                answer = json.loads(raw)
                if not isinstance(answer.get("reasoning"), str):
                    raise ValueError("Missing strategy reasoning")
                if not isinstance(answer.get("risk_notes"), list):
                    raise ValueError("Missing risk notes")
                if not isinstance(answer.get("code"), str) or not answer["code"].strip():
                    raise ValueError("Missing generated code")
                write_json(folder / "response.json", answer)
                (folder / "strategy.py").write_text(answer["code"], encoding="utf-8")
                usage = agent.assistant.client.actual_usage_summary or {}
                write_json(folder / "usage.json", {key: {k: v for k, v in value.items() if k != "cost"}
                           for key, value in usage.items() if isinstance(value, dict)})
                proc = subprocess.run([sys.executable, "-I", "-X", "utf8",
                                       str(ROOT / "finrobot_autogen/experiments/execute_stability_strategy.py"),
                                       str(case_dir), str(folder)], capture_output=True, text=True,
                                      encoding="utf-8", timeout=120, cwd=ROOT)
                (folder / "executor.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
                if not (folder / "execution.json").exists():
                    raise RuntimeError("Strategy worker did not return a result")
            except Exception as exc:
                write_json(folder / "execution.json", {"ok": False, "error_type": type(exc).__name__,
                           "error": redact(str(exc), configs)})
            execution = json.loads((folder / "execution.json").read_text(encoding="utf-8"))
            outcomes.append(execution["ok"])
            write_json(folder / "timing.json", {"seconds": round(time.perf_counter() - started, 3)})
            print(json.dumps({"case": case["name"], "attempt": number, "ok": execution["ok"],
                              "seconds": round(time.perf_counter() - started, 2)}), flush=True)
    return all(outcomes)


def verify_saved(spec, out_root=OUT_ROOT):
    """Re-execute saved code without a model call or overwriting original executions."""
    out = output_root(spec, out_root)
    checks = []
    for case in spec["cases"]:
        case_dir = out / case["name"]
        frame, evaluated, cfg = case_frame(load_snapshot(spec, case["symbol"]), case, spec)
        prepared = prepare_case(case_dir, case, frame, evaluated, cfg, spec)
        for number in range(1, spec["repeats"] + 1):
            folder = case_dir / f"generation_{number:02d}"
            row = {"case": case["name"], "attempt": number, "ok": False}
            try:
                request = json.loads((folder / "request.json").read_text(encoding="utf-8"))
                response = json.loads((folder / "response.json").read_text(encoding="utf-8"))
                if request.get("data_sha256") != prepared["data_sha256"]:
                    raise ValueError("Saved strategy data hash mismatch")
                if (folder / "strategy.py").read_text(encoding="utf-8") != response["code"]:
                    raise ValueError("Saved code differs from the recorded model response")
                proc = subprocess.run([sys.executable, "-I", "-X", "utf8",
                    str(ROOT / "finrobot_autogen/experiments/execute_stability_strategy.py"),
                    str(case_dir), str(folder), "--verify"], cwd=ROOT,
                    capture_output=True, text=True, encoding="utf-8", timeout=120)
                (folder / "verification.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
                verification = json.loads((folder / "verification.json").read_text(encoding="utf-8"))
                row["ok"] = proc.returncode == 0 and verification["ok"]
                if not row["ok"]:
                    row["error"] = verification.get("error", "Strategy verification failed")
            except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
                row["error"] = str(exc)
            checks.append(row)
            print(json.dumps(row), flush=True)
    result = {"ok": bool(checks) and all(row["ok"] for row in checks),
              "attempts": len(checks), "passed": sum(row["ok"] for row in checks),
              "api_calls_requested": 0, "checks": checks}
    write_json(out / "verification_summary.json", result)
    return result["ok"]


def evaluate(spec, out_root=OUT_ROOT):
    out = output_root(spec, out_root)
    rows, failures, latencies = [], Counter(), []
    per_case = {}
    for case in spec["cases"]:
        case_dir = out / case["name"]
        case_rows = []
        for number in range(1, spec["repeats"] + 1):
            folder = case_dir / f"generation_{number:02d}"
            response = folder / "response.json"
            execution_path = folder / "execution.json"
            if not execution_path.exists():
                failures["missing_attempt"] += 1
                row = {"case": case["name"], "attempt": number, "ok": False,
                       "json_valid": False, "symbol": case["symbol"],
                       "fast": case["fast"], "slow": case["slow"],
                       "error": "missing execution"}
                rows.append(row)
                case_rows.append(row)
                continue
            execution = json.loads(execution_path.read_text(encoding="utf-8"))
            json_valid = response.exists()
            if not json_valid:
                failures["json_decode_or_schema"] += 1
            elif not execution["ok"]:
                error = str(execution.get("error", ""))
                if "Disallowed" in error:
                    failures["validator_rejection"] += 1
                else:
                    failures["execution_error"] += 1
            timing = folder / "timing.json"
            if timing.exists() and json_valid:
                latencies.append(float(json.loads(timing.read_text(encoding="utf-8"))["seconds"]))
            row = {"case": case["name"], "attempt": number, "ok": execution["ok"],
                   "json_valid": json_valid, "symbol": case["symbol"],
                   "fast": case["fast"], "slow": case["slow"],
                   "error_type": execution.get("error_type"),
                   "error": execution.get("error")}
            if execution["ok"]:
                checks = execution.get("checks", {})
                row.update({key: checks.get(key) for key in
                            ("ast_passed", "fixed_rule_equal", "prefix_causality_passed",
                             "trusted_engine_equal")})
                row["total_return"] = execution["result"]["metrics"]["total_return"]
                row["max_drawdown"] = execution["result"]["metrics"]["max_drawdown"]
            rows.append(row)
            case_rows.append(row)
        attempts = len(case_rows)
        passed = sum(bool(row["ok"]) for row in case_rows)
        json_valid = sum(bool(row["json_valid"]) for row in case_rows)
        per_case[case["name"]] = {
            "symbol": case["symbol"], "fast": case["fast"], "slow": case["slow"],
            "attempts": attempts, "passed": passed,
            "pass_rate": round(passed / attempts, 4) if attempts else None,
            "json_valid_rate": round(json_valid / attempts, 4) if attempts else None,
        }
    attempts = len(rows)
    passed = sum(bool(row["ok"]) for row in rows)
    json_valid = sum(bool(row["json_valid"]) for row in rows)
    summary = {
        "ok": attempts == len(spec["cases"]) * spec["repeats"] and passed == attempts,
        "design": spec.get("design"),
        "limits": spec.get("limits"),
        "model": spec["model"],
        "repeats_per_case": spec["repeats"],
        "cases": len(spec["cases"]),
        "attempts": attempts,
        "passed": passed,
        "pass_rate": round(passed / attempts, 4) if attempts else None,
        "json_valid_rate": round(json_valid / attempts, 4) if attempts else None,
        "rule_consistency_rate": _rate(rows, "fixed_rule_equal"),
        "prefix_causality_rate": _rate(rows, "prefix_causality_passed"),
        "trusted_engine_equality_rate": _rate(rows, "trusted_engine_equal"),
        "rate_denominator": "all planned attempts, including failed and missing attempts",
        "conditional_check_rates": {
            "rule_consistency_rate": _rate(rows, "fixed_rule_equal", evaluated_only=True),
            "prefix_causality_rate": _rate(rows, "prefix_causality_passed", evaluated_only=True),
            "trusted_engine_equality_rate": _rate(rows, "trusted_engine_equal", evaluated_only=True),
        },
        "failure_taxonomy": dict(sorted(failures.items())),
        "latency_seconds": _latency(latencies),
        "per_case": per_case,
        "scope": "Bounded generation-stability check across three fixed cases; not evidence of investment alpha.",
        "evaluation_time": datetime.now(timezone.utc).isoformat(),
    }
    write_json(out / "summary.json", summary)
    pd.DataFrame(rows).to_csv(out / "attempts.csv", index=False)
    write_json(Path(out_root) / "latest.json", {"run": relative_label(out),
               "summary_sha256": digest(out / "summary.json")})
    print(json.dumps({key: value for key, value in summary.items()
                      if key not in ("per_case",)}), flush=True)
    print("Results:", relative_label(out), flush=True)
    return summary["ok"]


def _rate(rows, key, evaluated_only=False):
    values = [row.get(key, False) for row in rows if not evaluated_only or row.get(key) is not None]
    return round(sum(bool(value) for value in values) / len(values), 4) if values else None


def _latency(values):
    if not values:
        return {"count": 0, "mean": None, "median": None, "min": None, "max": None}
    ordered = sorted(values)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return {"count": len(values), "mean": round(sum(values) / len(values), 3),
            "median": round(median, 3), "min": round(min(values), 3), "max": round(max(values), 3)}


def relative_label(path):
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["generate", "evaluate", "verify", "all"])
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("stability_config.json"))
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    spec = json.loads(args.config.read_text(encoding="utf-8"))
    if spec["repeats"] < 1 or not spec["cases"]:
        raise ValueError("Stability evaluation requires cases and positive repeats")
    started = time.perf_counter()
    ok = True
    args.out_root.mkdir(parents=True, exist_ok=True)
    write_json(args.out_root / "declared_design.json", spec)
    if args.stage in ("generate", "all"):
        ok = generate(spec, args.resume, args.out_root)
    if args.stage == "verify":
        ok = verify_saved(spec, args.out_root)
    if args.stage in ("evaluate", "verify", "all"):
        ok = evaluate(spec, args.out_root) and ok
    print("Elapsed seconds:", round(time.perf_counter() - started, 3), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
