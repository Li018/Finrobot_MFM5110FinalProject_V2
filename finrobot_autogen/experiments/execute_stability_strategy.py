"""Run one stability-case strategy in a separate process."""
import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ["DEBUG"] = "false"
os.environ["NLTK_DATA"] = str(ROOT / ".cache/nltk_data")

import pandas as pd

from finrobot.functional.quantitative import audit_backtest
from finrobot.functional.strategy_codegen import execute_strategy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("case_dir", type=Path)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    case_dir = args.case_dir.resolve()
    folder = args.folder.resolve()
    data = pd.read_csv(case_dir / "data.csv", index_col="Date", parse_dates=True)
    config = json.loads((case_dir / "config.json").read_text(encoding="utf-8"))
    try:
        result, checks = execute_strategy((folder / "strategy.py").read_text(encoding="utf-8"), data, config)
        audit = audit_backtest(result, config, data)
        if not audit["ok"]:
            raise ValueError("Generated result failed independent ledger audit")
        output = {"ok": True, "checks": checks, "audit": audit, "result": result}
        if args.verify:
            saved = json.loads((folder / "execution.json").read_text(encoding="utf-8"))
            if not saved.get("ok") or saved.get("result") != result:
                raise ValueError("Replayed result differs from the saved successful execution")
            output = {"ok": True, "checks": checks, "audit": audit,
                      "saved_result_equal": True, "metrics": result["metrics"]}
    except Exception as exc:
        output = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
    (folder / ("verification.json" if args.verify else "execution.json")).write_text(
        json.dumps(output, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in output.items() if key != "result"}), flush=True)
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
