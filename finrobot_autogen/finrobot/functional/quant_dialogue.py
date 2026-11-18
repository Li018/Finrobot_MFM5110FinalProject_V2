"""Per-request experiments exposed directly to the interactive quant agent."""
import hashlib
import json
import math
import re
import subprocess
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import pandas as pd

from .quant_tools import ROOT, clean_market_data, load_config
from .quantitative import reference_signals, run_signal_backtest, save_backtest, audit_backtest
from .strategy_codegen import validate_source

DATA_ROOT = ROOT / "data/quant_dialogue"
OUT_ROOT = ROOT / "artifacts/quant_dialogue"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_experiment(experiment_id):
    if not re.fullmatch(r"[a-f0-9]{16}", experiment_id):
        raise ValueError("Invalid experiment_id; use the ID returned by prepare_quant_experiment")
    folder = DATA_ROOT / experiment_id
    metadata = json.loads((folder / "experiment.json").read_text(encoding="utf-8"))
    if sha256(folder / "prices.csv") != metadata["data_sha256"]:
        raise ValueError("Experiment data hash mismatch")
    data = pd.read_csv(folder / "prices.csv", index_col="Date", parse_dates=True)
    return data, metadata


def record_event(experiment_id, action, details):
    folder = OUT_ROOT / experiment_id
    folder.mkdir(parents=True, exist_ok=True)
    row = {"time": datetime.now(timezone.utc).isoformat(), "action": action, **details}
    with (folder / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def get_market_snapshot(symbol, start, end, slow):
    """Reuse only a matching, hash-verified snapshot with sufficient history."""
    candidates = sorted((ROOT / "data/quant_generalization").glob("*/metadata.json"))
    candidates += [ROOT / "data/quant_v2/metadata.json"]
    candidates += sorted((DATA_ROOT / "sources").glob("*/metadata.json"))
    for path in candidates:
        if not path.exists():
            continue
        meta = json.loads(path.read_text(encoding="utf-8"))
        if meta.get("symbol") != symbol or meta.get("adjust") != "qfq":
            continue
        if meta.get("requested_end", "") < end:
            continue
        price = path.parent / "prices.csv"
        if sha256(price) != meta["clean_sha256"]:
            raise ValueError("Cached market snapshot hash mismatch")
        data = pd.read_csv(price, index_col="Date", parse_dates=True)
        if len(data.loc[data.index < pd.Timestamp(start)]) >= slow and len(data.loc[start:end]) >= 2:
            return data, {"cached": True, "path": str(price.relative_to(ROOT)), "metadata": meta}

    from finrobot.data_source import CNStockUtils
    fetch_start = (pd.Timestamp(start) - pd.Timedelta(days=slow * 2 + 60)).date().isoformat()
    raw = CNStockUtils.get_stock_data(symbol, fetch_start, end, adjust="qfq")
    cfg = {**load_config(), "start_date": fetch_start, "end_date": end, "adjust": "qfq"}
    data, meta = clean_market_data(raw, cfg)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    folder = DATA_ROOT / "sources" / (symbol + "_" + stamp)
    folder.mkdir(parents=True)
    raw.to_csv(folder / "adapter_raw.csv")
    data.to_csv(folder / "prices.csv", float_format="%.12g")
    meta.update(symbol=symbol, requested_start=fetch_start, requested_end=end,
                fetched_at=datetime.now(timezone.utc).isoformat(), provider=dict(raw.attrs),
                clean_sha256=sha256(folder / "prices.csv"), raw_sha256=sha256(folder / "adapter_raw.csv"))
    write_json(folder / "metadata.json", meta)
    data = pd.read_csv(folder / "prices.csv", index_col="Date", parse_dates=True)
    return data, {"cached": False, "path": str((folder / "prices.csv").relative_to(ROOT)), "metadata": meta}


def validate_parameters(symbol, start_date, end_date, fast, slow, initial_cash, max_position):
    if not re.fullmatch(r"\d{6}", symbol):
        raise ValueError("Use a six-digit A-share code")
    start, end = pd.Timestamp(start_date), pd.Timestamp(end_date)
    if pd.isna(start) or pd.isna(end) or start >= end:
        raise ValueError("Start date must precede end date")
    if end.date() > datetime.now(ZoneInfo("Asia/Shanghai")).date():
        raise ValueError("End date cannot be in the future")
    if type(fast) is not int or type(slow) is not int or not 1 <= fast < slow <= 252:
        raise ValueError("Require integer windows 1 <= fast < slow <= 252")
    if not math.isfinite(initial_cash) or initial_cash <= 0:
        raise ValueError("Initial cash must be positive and finite")
    if not math.isfinite(max_position) or not 0 < max_position <= 1:
        raise ValueError("Entry budget fraction must be in (0, 1]")
    return start.date().isoformat(), end.date().isoformat()


def plot_comparison(experiment_id, results, config):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(12, 5))
    for name, result in results.items():
        frame = pd.DataFrame(result["equity"])
        ax.plot(pd.to_datetime(frame.Date), frame.equity / result["metrics"]["initial_cash"], label=name)
    ax.set_ylabel("Portfolio value / initial cash")
    ax.set_title(f"{config['symbol']} | {config['start_date']} to {config['end_date']} | SMA {config['fast']}/{config['slow']}")
    ax.legend()
    ax.grid(alpha=.25)
    fig.tight_layout()
    path = OUT_ROOT / experiment_id / "comparison.png"
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return str(path)


class QuantDialogueTools:
    @staticmethod
    def prepare_quant_experiment(symbol: str, start_date: str, end_date: str,
                                fast: int = 20, slow: int = 60,
                                initial_cash: float = 100000.0, max_position: float = 0.8) -> str:
        """Prepare an A-share experiment for the USER'S code, dates, SMA windows, cash and entry-budget fraction. Reuse verified data or fetch real data. Returns experiment_id; never silently use the fixed demo instead."""
        start, end = validate_parameters(symbol, start_date, end_date, fast, slow, initial_cash, max_position)
        initial_cash, max_position = float(initial_cash), float(max_position)
        data, source = get_market_snapshot(symbol, start, end, slow)
        evaluation = data.loc[start:end]
        history = data.loc[data.index < pd.Timestamp(start)].tail(slow)
        if len(history) < slow or len(evaluation) < 2:
            raise ValueError("Insufficient real observations or historical warm-up for the requested dates")
        frame = pd.concat([history, evaluation])
        frame.index.name = "Date"
        cfg = {**load_config(), "experiment": "interactive_quant", "symbol": symbol, "start_date": start,
               "end_date": end, "fast": fast, "slow": slow, "initial_cash": initial_cash,
               "max_position": max_position, "evaluation_start": evaluation.index[0].date().isoformat()}
        identity = json.dumps({"config": cfg, "source_sha256": source["metadata"]["clean_sha256"]}, sort_keys=True)
        experiment_id = hashlib.sha256(identity.encode()).hexdigest()[:16]
        folder = DATA_ROOT / experiment_id
        folder.mkdir(parents=True, exist_ok=True)
        if not (folder / "experiment.json").exists():
            frame.to_csv(folder / "prices.csv", float_format="%.12g")
            metadata = {"experiment_id": experiment_id, "config": cfg, "source": source,
                        "data_sha256": sha256(folder / "prices.csv"), "evaluation_rows": len(evaluation),
                        "warmup_rows": len(history), "actual_start": str(evaluation.index[0].date()),
                        "actual_end": str(evaluation.index[-1].date()), "created_at": datetime.now(timezone.utc).isoformat()}
            write_json(folder / "experiment.json", metadata)
        _, metadata = load_experiment(experiment_id)
        record_event(experiment_id, "prepare", {"config": cfg})
        return json.dumps({"ok": True, "experiment_id": experiment_id, "config": cfg,
            "evaluation_rows": metadata["evaluation_rows"], "warmup_rows": metadata["warmup_rows"],
            "actual_start": metadata["actual_start"], "actual_end": metadata["actual_end"],
            "source": metadata["source"], "risk_policy": "max_position limits ENTRY budget, not continuous holding weight; weights can drift",
            "supported_strategies": ["sma", "buy_hold"], "generated_code_interface":
            "Exactly generate_signals(data, fast, slow) and run_strategy(data, config). SMA uses Close.rolling(fast/slow).mean(), comparison >, astype(int), no imports. run_strategy returns run_signal_backtest(data, generate_signals(data, config['fast'], config['slow']), config). No shift, no files/network."}, ensure_ascii=False)

    @staticmethod
    def compare_quant_strategies(experiment_id: str) -> str:
        """Run SMA and buy-and-hold for this prepared experiment, using its exact dates, windows, cash and costs. Saves metrics, trades, equity and a plot. No LLM numbers are accepted."""
        data, meta = load_experiment(experiment_id)
        cfg = meta["config"]
        signals = {"sma": reference_signals(data, cfg["fast"], cfg["slow"]), "buy_hold": pd.Series(1, index=data.index)}
        results = {}
        for name, targets in signals.items():
            results[name] = run_signal_backtest(data, targets, cfg)
            save_backtest(results[name], OUT_ROOT / experiment_id / name)
        picture = plot_comparison(experiment_id, results, cfg)
        record_event(experiment_id, "compare", {"strategies": list(results)})
        return json.dumps({"ok": True, "experiment_id": experiment_id, "config": cfg,
            "metrics": {name: result["metrics"] for name, result in results.items()},
            "plot_path": picture, "results_directory": str(OUT_ROOT / experiment_id)}, ensure_ascii=False)

    @staticmethod
    def check_quant_risk(experiment_id: str, strategy: str = "sma") -> str:
        """Independently reconstruct the cash ledger and inspect risk for sma, buy_hold or generated_NNN. Report entry budget versus observed drifting exposure honestly."""
        data, meta = load_experiment(experiment_id)
        if strategy not in ("sma", "buy_hold") and not re.fullmatch(r"generated_\d{3}", strategy):
            raise ValueError("Choose sma, buy_hold or a returned generated_NNN name")
        folder = OUT_ROOT / experiment_id / strategy
        result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
        audit = audit_backtest(result, meta["config"], data)
        weights = [row["shares"] * row["close"] / row["equity"] for row in result["equity"]]
        output = {"ok": audit["ok"], "experiment_id": experiment_id, "strategy": strategy,
            "audit": audit, "metrics": result["metrics"], "entry_budget_limit": meta["config"]["max_position"],
            "maximum_observed_close_weight": max(weights), "continuous_position_cap_enforced": False,
            "risk_policy": "Entry-budget constraint; holding weight can drift with prices",
            "execution_assumptions": meta["config"]["assumptions"], "risk_events": result["risk_events"]}
        write_json(folder / "risk_check.json", output)
        record_event(experiment_id, "risk_check", {"strategy": strategy, "ok": audit["ok"]})
        return json.dumps(output, ensure_ascii=False)

    @staticmethod
    def execute_quant_code(experiment_id: str, code: str) -> str:
        """Save the agent-generated two-function SMA Python module, validate AST, run in a time-limited child process, and compare with the trusted rule. Return errors for model repair; never execute shell or arbitrary imports."""
        _, meta = load_experiment(experiment_id)
        parent = OUT_ROOT / experiment_id
        parent.mkdir(parents=True, exist_ok=True)
        number = 1
        while (parent / f"generated_{number:03d}").exists():
            number += 1
        name = f"generated_{number:03d}"
        folder = parent / name
        folder.mkdir()
        (folder / "strategy.py").write_text(code, encoding="utf-8")
        try:
            validate_source(code)
            worker = ROOT / "finrobot_autogen/experiments/execute_dialogue_strategy.py"
            proc = subprocess.run([sys.executable, "-I", "-X", "utf8", str(worker), experiment_id, name],
                                  cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120)
            (folder / "executor.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
            output = json.loads((folder / "execution.json").read_text(encoding="utf-8"))
            if proc.returncode and output.get("ok"):
                raise RuntimeError("Worker returned inconsistent status")
        except Exception as exc:
            output = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
            write_json(folder / "execution.json", output)
        response = {key: value for key, value in output.items() if key != "result"}
        response.update(experiment_id=experiment_id, strategy=name, code_path=str(folder / "strategy.py"))
        if output["ok"]:
            response["metrics"] = output["result"]["metrics"]
        record_event(experiment_id, "execute_code", {"strategy": name, "ok": output["ok"]})
        return json.dumps(response, ensure_ascii=False)
