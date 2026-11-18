"""V2 fixed-data experiment: fetch, baseline, independent code generation, evaluate."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ["DEBUG"] = "false"
os.environ["NLTK_DATA"] = str(ROOT / ".cache/nltk_data")

from finrobot.functional.quant_tools import ARTIFACTS, DATA, QuantTools, load_config, load_data, prepare_data
from finrobot.functional.quantitative import calculate_metrics, save_backtest, audit_backtest


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def generate(repeats, resume):
    import autogen
    from finrobot.agents.workflow import SingleAssistant
    from finrobot.agents.prompts import quant_generation_prompt
    cfg = load_config()
    configs = None
    prompt = quant_generation_prompt.format(data_summary=QuantTools.get_data_summary())
    fingerprint = hashlib.sha256((prompt + json.dumps(cfg, sort_keys=True)).encode("utf-8")).hexdigest()
    outcomes = []
    for number in range(1, repeats + 1):
        folder = ARTIFACTS / f"generation_{number:02d}"
        if resume and (folder / "execution.json").exists():
            saved = json.loads((folder / "request.json").read_text(encoding="utf-8"))
            if saved.get("request_sha256") != fingerprint:
                raise ValueError("Resume inputs changed; preserve prior evidence and use a new experiment directory")
            print("resume: preserving completed attempt", number, flush=True)
            completed = folder / "repair_01/execution.json"
            if not completed.exists():
                completed = folder / "execution.json"
            outcomes.append(json.loads(completed.read_text(encoding="utf-8"))["ok"])
            continue
        if (folder / "execution.json").exists():
            raise FileExistsError("Attempt already exists; use --resume to preserve existing evidence")
        if configs is None:
            configs = autogen.config_list_from_json(str(ROOT / "finrobot_autogen/OAI_CONFIG_LIST"), filter_dict={"model": [cfg["model"]]})
            if not configs or "REPLACE_ME" in configs[0].get("api_key", ""):
                raise ValueError("Configure a local API key first")
        folder.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        write_json(folder / "request.json", {"model": cfg["model"], "temperature": 0, "cache_seed": None,
                   "max_tokens": 3000, "prompt": prompt, "config": cfg, "request_sha256": fingerprint})
        try:
            agent = SingleAssistant("Quant_Analyst", llm_config={"config_list": configs, "cache_seed": None,
                "temperature": 0, "max_tokens": 3000, "timeout": 90, "max_retries": 0,
                "tool_choice": "none", "response_format": {"type": "json_object"}}, code_execution_config=False)
            response = agent.assistant.generate_reply(messages=[{"role": "user", "content": prompt}])
            raw = response if isinstance(response, str) else response.get("content", "")
            (folder / "raw_response.txt").write_text(raw or "", encoding="utf-8")
            answer = json.loads(raw)
            if not isinstance(answer.get("reasoning"), str) or not isinstance(answer.get("risk_notes"), list):
                raise ValueError("Missing strategy reasoning or risk notes")
            (folder / "strategy.py").write_text(answer["code"], encoding="utf-8")
            write_json(folder / "response.json", answer)
            usage = agent.assistant.client.actual_usage_summary or {}
            # AutoGen has no built-in price for DeepSeek; retain token usage only.
            write_json(folder / "usage.json", {key: {k:v for k,v in value.items() if k != "cost"}
                       for key, value in usage.items() if isinstance(value, dict)})
            proc = subprocess.run([sys.executable, "-I", "-X", "utf8", str(Path(__file__).with_name("execute_generated_strategy.py")), str(folder)],
                                  capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=ROOT)
            (folder / "executor.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
            if not (folder / "execution.json").exists():
                raise RuntimeError("Strategy worker did not return a result")
        except Exception as exc:
            # Do not persist request headers or credentials in error evidence.
            message = str(exc)
            for entry in configs:
                message = message.replace(entry.get("api_key", "") or "<unused>", "[REDACTED]")
            write_json(folder / "execution.json", {"ok": False, "error_type": type(exc).__name__, "error": message})
        execution = json.loads((folder / "execution.json").read_text(encoding="utf-8"))
        outcomes.append(execution["ok"])
        write_json(folder / "timing.json", {"seconds": round(time.perf_counter() - started, 3)})
        print(json.dumps({"attempt": number, "ok": execution["ok"], "seconds": round(time.perf_counter() - started, 2)}), flush=True)
    return all(outcomes)


def repair_failed():
    """One model repair per failed response; original evidence is immutable."""
    import autogen
    from finrobot.agents.workflow import SingleAssistant
    cfg = load_config()
    configs = None
    outcomes = []
    for initial_path in sorted(ARTIFACTS.glob("generation_*/execution.json")):
        initial = json.loads(initial_path.read_text(encoding="utf-8"))
        if initial["ok"]:
            continue
        folder = initial_path.parent / "repair_01"
        if (folder / "execution.json").exists():
            outcomes.append(json.loads((folder / "execution.json").read_text(encoding="utf-8"))["ok"])
            continue
        folder.mkdir(parents=True, exist_ok=True)
        if configs is None:
            configs = autogen.config_list_from_json(str(ROOT / "finrobot_autogen/OAI_CONFIG_LIST"), filter_dict={"model": [cfg["model"]]})
        request = json.loads((initial_path.parent / "request.json").read_text(encoding="utf-8"))
        previous = (initial_path.parent / "raw_response.txt").read_text(encoding="utf-8")
        prompt = request["prompt"] + "\nThe previous response failed. Return a corrected valid JSON object. JSON-decode the code field to actual multiline Python, not literal backslash-n text.\nError: " + initial.get("error", "unknown") + "\nPrevious response:\n" + previous
        write_json(folder / "request.json", {"prompt": prompt, "model": cfg["model"], "cache_seed": None,
                    "temperature": 0, "max_tokens": 3000, "repair_of": initial_path.parent.name})
        started = time.perf_counter()
        try:
            agent = SingleAssistant("Quant_Analyst", llm_config={"config_list": configs, "cache_seed": None,
                "temperature": 0, "max_tokens": 3000, "timeout": 90, "max_retries": 0,
                "tool_choice": "none", "response_format": {"type": "json_object"}}, code_execution_config=False)
            response = agent.assistant.generate_reply(messages=[{"role": "user", "content": prompt}])
            raw = response if isinstance(response, str) else response.get("content", "")
            (folder / "raw_response.txt").write_text(raw or "", encoding="utf-8")
            answer = json.loads(raw)
            write_json(folder / "response.json", answer)
            (folder / "strategy.py").write_text(answer["code"], encoding="utf-8")
            proc = subprocess.run([sys.executable, "-I", "-X", "utf8", str(Path(__file__).with_name("execute_generated_strategy.py")), str(folder)],
                                  capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=ROOT)
            (folder / "executor.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
            if not (folder / "execution.json").exists():
                raise RuntimeError("Repair worker did not return a result")
        except Exception as exc:
            message = str(exc)
            for entry in configs:
                message = message.replace(entry.get("api_key", "") or "<unused>", "[REDACTED]")
            write_json(folder / "execution.json", {"ok": False, "error_type": type(exc).__name__, "error": message})
        final = json.loads((folder / "execution.json").read_text(encoding="utf-8"))
        outcomes.append(final["ok"])
        write_json(folder / "timing.json", {"seconds": round(time.perf_counter() - started, 3)})
        print(json.dumps({"repair_of": initial_path.parent.name, "ok": final["ok"]}), flush=True)
    return all(outcomes)


def audit_result(result, config, data=None):
    data = load_data() if data is None else data
    return audit_backtest(result, config, data)


def verify_saved():
    """Replay saved model code without making any API requests."""
    checks = []
    for initial in sorted(ARTIFACTS.glob("generation_*/execution.json")):
        folder = initial.parent
        if (folder / "repair_01/execution.json").exists():
            folder = folder / "repair_01"
        if not (folder / "strategy.py").exists():
            checks.append({"attempt": initial.parent.name, "ok": False, "error": "No saved code"})
            continue
        proc = subprocess.run([sys.executable, "-I", "-X", "utf8", str(Path(__file__).with_name("execute_generated_strategy.py")), str(folder), "verify"],
                              capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=ROOT)
        (folder / "verification.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
        if proc.returncode or not (folder / "verification.json").exists():
            checks.append({"attempt": initial.parent.name, "ok": False, "error": "Verification process failed"})
            continue
        verified = json.loads((folder / "verification.json").read_text(encoding="utf-8"))
        original = json.loads((folder / "execution.json").read_text(encoding="utf-8"))
        checks.append({"attempt": initial.parent.name, "ok": verified["ok"] and verified.get("result") == original.get("result")})
        print(json.dumps(checks[-1]), flush=True)
    result = {"ok": len(checks) == load_config()["repeats"] and all(row["ok"] for row in checks),
              "python": sys.version, "executable": str(Path(sys.executable).name), "api_requests": 0, "checks": checks}
    write_json(ARTIFACTS / "offline_verification.json", result)
    return result["ok"]


def evaluate():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pandas as pd
    cfg = load_config()
    reference = json.loads((ARTIFACTS / "sma/result.json").read_text(encoding="utf-8"))
    hold = json.loads((ARTIFACTS / "buy_hold/result.json").read_text(encoding="utf-8"))
    rows, audits = [], {}
    for name, result in (("manual_sma", reference), ("buy_hold", hold)):
        rows.append({"strategy": name, **result["metrics"]})
        audits[name] = audit_result(result, cfg)
    attempts = []
    for path in sorted(ARTIFACTS.glob("generation_*/execution.json")):
        result = json.loads(path.read_text(encoding="utf-8"))
        initial_path = path.parent / "execution_initial.json"
        initial = json.loads(initial_path.read_text(encoding="utf-8")) if initial_path.exists() else result
        raw_ok = (path.parent / "response.json").exists()
        first_model_ok = result["ok"]
        repair_path = path.parent / "repair_01/execution.json"
        repaired = repair_path.exists()
        if repaired:
            result = json.loads(repair_path.read_text(encoding="utf-8"))
        entry = {"attempt": path.parent.name, "ok": result["ok"], "initial_execution_ok": initial["ok"],
                 "initial_json_valid": raw_ok, "without_model_repair_ok": first_model_ok,
                 "model_repairs": int(repaired), "execution_source": str((repair_path if repaired else path).relative_to(ARTIFACTS))}
        if not initial["ok"]:
            entry["initial_error"] = initial.get("error", "unknown")
        if result["ok"]:
            generated = result["result"]
            save_backtest(generated, path.parent)
            entry.update(checks=result["checks"], identical_to_manual=generated == reference)
            rows.append({"strategy": path.parent.name, **generated["metrics"]})
            audits[path.parent.name] = audit_result(generated, cfg)
        else:
            entry["error"] = result.get("error", "unknown")
        attempts.append(entry)
    write_json(ARTIFACTS / "audit.json", audits)
    passed = sum(entry["ok"] for entry in attempts)
    complete = (len(attempts) == cfg["repeats"] and passed == len(attempts)
                and all(v["ok"] for v in audits.values()) and all(a.get("identical_to_manual", False) for a in attempts))
    write_json(ARTIFACTS / "evaluation.json", {"attempts": attempts, "requested_repeats": cfg["repeats"],
        "completed_attempts": len(attempts), "initial_execution_success_rate": sum(a["initial_execution_ok"] for a in attempts) / len(attempts) if attempts else None,
        "validated_success_rate": passed / len(attempts) if attempts else None, "complete": complete,
        "initial_json_valid_rate": sum(a["initial_json_valid"] for a in attempts) / len(attempts) if attempts else None,
        "without_model_repair_success_rate": sum(a["without_model_repair_ok"] for a in attempts) / len(attempts) if attempts else None,
        "model_repair_requests": sum(a["model_repairs"] for a in attempts),
        "successful_generations": passed, "all_audits_passed": all(v["ok"] for v in audits.values()),
        "design": "Fixed-rule implementation evaluation; not evidence of LLM investment alpha.",
        "autogen_response_cache_disabled": True, "data_sha256": hashlib.sha256((DATA / "prices.csv").read_bytes()).hexdigest()})
    pd.DataFrame(rows).to_csv(ARTIFACTS / "comparison.csv", index=False)
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True, gridspec_kw={"height_ratios": [2, 1]})
    series = [("Manual SMA", reference), ("Buy and hold", hold)]
    if passed:
        selected = next(entry for entry in attempts if entry["ok"])
        path = ARTIFACTS / selected["execution_source"]
        series.append(("LLM generated SMA", json.loads(path.read_text(encoding="utf-8"))["result"]))
    for label, result in series:
        frame = pd.DataFrame(result["equity"]).set_index("Date")
        values = frame.equity / cfg["initial_cash"]
        dates = pd.to_datetime(values.index)
        axes[0].plot(dates, values, label=label, linestyle="--" if "LLM" in label else "-")
        axes[1].plot(dates, (values / values.cummax() - 1) * 100, label=label)
    axes[0].set_ylabel("Portfolio value / initial cash")
    axes[1].set_ylabel("Drawdown (%)")
    axes[0].set_title("V2 fixed-rule experiment: 000001, 2021-2025")
    for ax in axes:
        ax.grid(alpha=0.25)
        ax.legend()
    fig.tight_layout()
    fig.savefig(ARTIFACTS / "comparison.png", dpi=160)
    plt.close(fig)
    return {"generations": len(attempts), "passed": passed, "audits_passed": all(v["ok"] for v in audits.values()), "complete": complete}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["fetch", "baseline", "generate", "repair", "verify", "evaluate", "all"])
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--repeats", type=int)
    parser.add_argument("--refresh-data", action="store_true")
    args = parser.parse_args()
    started = time.perf_counter()
    ok = True
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    if args.stage in ("fetch", "all"):
        print(json.dumps(prepare_data(args.refresh_data), ensure_ascii=True), flush=True)
    if args.stage in ("baseline", "all"):
        print(QuantTools.backtest_reference("sma"), flush=True)
        print(QuantTools.backtest_reference("buy_hold"), flush=True)
    if args.stage in ("generate", "all"):
        ok = generate(args.repeats or load_config()["repeats"], args.resume)
    if args.stage in ("repair", "all"):
        ok = repair_failed()
    if args.stage == "verify":
        ok = verify_saved()
    if args.stage in ("evaluate", "all"):
        summary = evaluate()
        print(json.dumps(summary), flush=True)
        ok = ok and summary["complete"]
    print("Elapsed seconds:", round(time.perf_counter() - started, 3), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
