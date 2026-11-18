"""Bounded worker for a prepared dialogue experiment and checked generated module."""
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ["DEBUG"] = "false"
os.environ["NLTK_DATA"] = str(ROOT / ".cache/nltk_data")
from finrobot.functional.quant_dialogue import OUT_ROOT, load_experiment, write_json
from finrobot.functional.strategy_codegen import execute_strategy
from finrobot.functional.quantitative import save_backtest, audit_backtest

experiment_id, name = sys.argv[1:3]
if not re.fullmatch(r"generated_\d{3}", name):
    raise ValueError("Invalid generated strategy name")
data, meta = load_experiment(experiment_id)
folder = OUT_ROOT / experiment_id / name
try:
    result, checks = execute_strategy((folder / "strategy.py").read_text(encoding="utf-8"), data, meta["config"])
    audit = audit_backtest(result, meta["config"], data)
    if not audit["ok"]:
        raise ValueError("Generated result failed independent ledger audit")
    save_backtest(result, folder)
    output = {"ok": True, "checks": checks, "audit": audit, "result": result}
except Exception as exc:
    output = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
write_json(folder / "execution.json", output)
print(json.dumps({key: value for key, value in output.items() if key != "result"}))
raise SystemExit(0 if output["ok"] else 1)
