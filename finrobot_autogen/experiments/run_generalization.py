"""Replay frozen model code on configurable symbols and disjoint evaluation windows."""
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
from finrobot.data_source import CNStockUtils
from finrobot.functional.quant_tools import clean_market_data, load_config
from finrobot.functional.quantitative import reference_signals, run_signal_backtest, save_backtest, audit_backtest
from finrobot.functional.strategy_codegen import execute_strategy

DATA_ROOT = ROOT / "data/quant_generalization"
OUT_ROOT = ROOT / "artifacts/quant_generalization"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_config(spec):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", spec["name"]):
        raise ValueError("Need a safe experiment name")
    if not spec["symbols"] or len(set(spec["symbols"])) != len(spec["symbols"]):
        raise ValueError("Need distinct symbols")
    if not all(re.fullmatch(r"\d{6}", symbol) for symbol in spec["symbols"]):
        raise ValueError("Symbols must contain six digits")
    if spec["adjust"] not in ("qfq", "hfq", ""):
        raise ValueError("Unsupported adjustment mode")
    names, previous_end = set(), None
    for window in sorted(spec["windows"], key=lambda row: row["start"]):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", window["name"]) or window["name"] in names:
            raise ValueError("Need unique safe window names")
        start, end = pd.Timestamp(window["start"]), pd.Timestamp(window["end"])
        if start > end or (previous_end is not None and start <= previous_end):
            raise ValueError("Evaluation windows must be ordered and disjoint")
        if start < pd.Timestamp(spec["data_start"]) or end > pd.Timestamp(spec["data_end"]):
            raise ValueError("Window outside requested data coverage")
        names.add(window["name"])
        previous_end = end
    if not names:
        raise ValueError("Need evaluation windows")


def dataset_folder(spec, symbol):
    return DATA_ROOT / f"{symbol}_{pd.Timestamp(spec['data_start']):%Y%m%d}_{pd.Timestamp(spec['data_end']):%Y%m%d}_{spec['adjust'] or 'raw'}"


def fetch_data(spec):
    for symbol in spec["symbols"]:
        folder = dataset_folder(spec, symbol)
        if (folder / "metadata.json").exists():
            load_snapshot(folder)
            print(json.dumps({"symbol": symbol, "cached": True}), flush=True)
            continue
        frame = CNStockUtils.get_stock_data(symbol, spec["data_start"], spec["data_end"], adjust=spec["adjust"])
        provider = dict(frame.attrs)
        cfg = {**load_config(), "symbol": symbol, "start_date": spec["data_start"], "end_date": spec["data_end"], "adjust": spec["adjust"]}
        clean, record = clean_market_data(frame, cfg)
        folder.mkdir(parents=True, exist_ok=True)
        frame.to_csv(folder / "adapter_raw.csv")
        clean.to_csv(folder / "prices.csv", float_format="%.12g")
        record.update(symbol=symbol, requested_start=spec["data_start"], requested_end=spec["data_end"],
            fetched_at=datetime.now(timezone.utc).isoformat(), provider=provider,
            raw_definition="CNStockUtils response after adapter normalization", raw_sha256=digest(folder / "adapter_raw.csv"),
            clean_sha256=digest(folder / "prices.csv"))
        write_json(folder / "metadata.json", record)
        print(json.dumps({"symbol": symbol, "rows": len(clean), "provider": provider}), flush=True)


def load_snapshot(folder):
    meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    if digest(folder / "prices.csv") != meta["clean_sha256"] or digest(folder / "adapter_raw.csv") != meta["raw_sha256"]:
        raise ValueError("Snapshot hash mismatch: " + folder.name)
    return pd.read_csv(folder / "prices.csv", index_col="Date", parse_dates=True), meta


def slice_window(data, window, slow):
    evaluation = data.loc[window["start"]:window["end"]]
    if len(evaluation) < 2:
        raise ValueError("Too few evaluation observations")
    history = data.loc[data.index < evaluation.index[0]].tail(slow)
    if len(history) < slow:
        raise ValueError("Insufficient historical warm-up; extend data_start")
    return pd.concat([history, evaluation]), len(history), evaluation


def frozen_sources():
    evaluation = json.loads((ROOT / "artifacts/quant_v2/evaluation.json").read_text(encoding="utf-8"))
    sources = []
    for attempt in evaluation["attempts"]:
        if not attempt["ok"]:
            raise ValueError("Original strategy not validated: " + attempt["attempt"])
        execution = ROOT / "artifacts/quant_v2" / Path(attempt["execution_source"].replace("\\", "/"))
        path = execution.parent / "strategy.py"
        sources.append({"name": attempt["attempt"], "path": path.relative_to(ROOT).as_posix(), "sha256": digest(path)})
    if not sources:
        raise ValueError("No frozen generated strategies")
    return sources


def run_suite(spec, resume=False):
    base, sources = load_config(), frozen_sources()
    snapshots = {symbol: load_snapshot(dataset_folder(spec, symbol)) for symbol in spec["symbols"]}
    inputs = {"spec": spec, "backtest_config": base, "frozen_sources": sources,
              "datasets": {s: m["clean_sha256"] for s, (_, m) in snapshots.items()},
              "engine_sha256": digest(ROOT / "finrobot_autogen/finrobot/functional/quantitative.py"),
              "runner_sha256": digest(Path(__file__)),
              "validator_sha256": digest(ROOT / "finrobot_autogen/finrobot/functional/strategy_codegen.py")}
    signature = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode("utf-8")).hexdigest()
    out = OUT_ROOT / (spec["name"] + "_" + signature[:12])
    write_json(out / "inputs.json", inputs)
    summaries = []
    for symbol, (data, meta) in snapshots.items():
        for window in spec["windows"]:
            folder = out / symbol / window["name"]
            cached = folder / "case_summary.json"
            if resume and cached.exists():
                summaries.append(json.loads(cached.read_text(encoding="utf-8")))
                continue
            frame, warmup, evaluated = slice_window(data, window, base["slow"])
            cfg = {**base, "symbol": symbol, "evaluation_start": evaluated.index[0].date().isoformat()}
            manual = run_signal_backtest(frame, reference_signals(frame, cfg["fast"], cfg["slow"]), cfg)
            hold = run_signal_backtest(frame, pd.Series(1, index=frame.index), cfg)
            save_backtest(manual, folder / "manual_sma")
            save_backtest(hold, folder / "buy_hold")
            manual_audit = audit_backtest(manual, cfg, frame)
            hold_audit = audit_backtest(hold, cfg, frame)
            generated = []
            for source in sources:
                try:
                    result, checks = execute_strategy((ROOT / source["path"]).read_text(encoding="utf-8"), frame, cfg)
                    audit = audit_backtest(result, cfg, frame)
                    same = result == manual
                    save_backtest(result, folder / source["name"])
                    row = {"name": source["name"], "ok": same and audit["ok"], "checks": checks,
                           "audit": audit, "identical_to_manual": same}
                except Exception as exc:
                    row = {"name": source["name"], "ok": False, "error_type": type(exc).__name__, "error": str(exc)}
                generated.append(row)
            summary = {"symbol": symbol, "window": window, "evaluation_start": str(evaluated.index[0].date()),
                "evaluation_end": str(evaluated.index[-1].date()), "observations": len(evaluated), "warmup_observations": warmup,
                "data_sha256": meta["clean_sha256"], "config": cfg,
                "manual_metrics": manual["metrics"], "buy_hold_metrics": hold["metrics"],
                "manual_audit": manual_audit, "buy_hold_audit": hold_audit, "generations": generated,
                "ok": manual_audit["ok"] and hold_audit["ok"] and all(g["ok"] for g in generated)}
            if len(manual["equity"]) != len(evaluated) or manual["equity"][0]["equity"] != cfg["initial_cash"]:
                raise ValueError("Warm-up leaked into evaluation accounting")
            write_json(cached, summary)
            summaries.append(summary)
            print(json.dumps({"symbol": symbol, "window": window["name"], "ok": summary["ok"],
                              "replays": len(generated), "observations": len(evaluated)}), flush=True)
    replays = [g for case in summaries for g in case["generations"]]
    summary = {"ok": all(c["ok"] for c in summaries), "cases": len(summaries), "symbols": spec["symbols"],
        "windows": spec["windows"], "frozen_strategies": len(sources), "replays": len(replays),
        "passed_replays": sum(g["ok"] for g in replays), "new_llm_requests": 0, "investment_alpha_tested": False,
        "scope": spec["limits"], "warmup_policy": "Past-only warm-up excluded from all metrics; first evaluation close forms the first executable signal.",
        "case_summaries": summaries}
    write_json(out / "summary.json", summary)
    plot_results(out, summaries)
    write_json(OUT_ROOT / "latest.json", {"run": out.relative_to(ROOT).as_posix(), "summary_sha256": digest(out / "summary.json")})
    print(json.dumps({key: value for key, value in summary.items() if key != "case_summaries"}), flush=True)
    print("Results:", out.relative_to(ROOT), flush=True)
    return summary["ok"]


def plot_results(out, cases):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    rows = [{"symbol": c["symbol"], "window": c["window"]["name"], "observations": c["observations"],
             "manual_total_return": c["manual_metrics"]["total_return"],
             "buy_hold_total_return": c["buy_hold_metrics"]["total_return"],
             "manual_max_drawdown": c["manual_metrics"]["max_drawdown"],
             "buy_hold_max_drawdown": c["buy_hold_metrics"]["max_drawdown"],
             "manual_orders": c["manual_metrics"]["executed_orders"],
             "passed_codes": sum(g["ok"] for g in c["generations"])} for c in cases]
    pd.DataFrame(rows).to_csv(out / "comparison.csv", index=False)
    y = np.arange(len(rows))
    fig, axes = plt.subplots(1, 2, figsize=(14, 7), sharey=True)
    labels = [r["symbol"] + " / " + r["window"] for r in rows]
    for ax, a, b, title in ((axes[0], "manual_total_return", "buy_hold_total_return", "Total return (%)"),
                             (axes[1], "manual_max_drawdown", "buy_hold_max_drawdown", "Maximum drawdown (%)")):
        ax.barh(y - .18, [r[a] * 100 for r in rows], .35, label="Manual / identical LLM SMA")
        ax.barh(y + .18, [r[b] * 100 for r in rows], .35, label="Buy and hold")
        ax.axvline(0, color="gray", linewidth=.7)
        ax.set_yticks(y, labels)
        ax.set_title(title)
        ax.grid(axis="x", alpha=.25)
        ax.legend(fontsize=8)
    axes[0].invert_yaxis()
    fig.suptitle("Frozen-code generalization across symbols and disjoint windows")
    fig.tight_layout()
    fig.savefig(out / "comparison.png", dpi=160)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["fetch", "evaluate", "all"])
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("generalization_config.json"))
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    spec = json.loads(args.config.read_text(encoding="utf-8"))
    validate_config(spec)
    started = time.perf_counter()
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    write_json(OUT_ROOT / "declared_design.json", spec)
    if args.stage in ("fetch", "all"):
        fetch_data(spec)
    ok = run_suite(spec, args.resume) if args.stage in ("evaluate", "all") else True
    print("Elapsed seconds:", round(time.perf_counter() - started, 3))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
