"""Run an AST-checked generated strategy in a separate process."""
import json
import os
import sys
from pathlib import Path

os.environ["DEBUG"] = "false"
ROOT = Path(__file__).resolve().parents[2]
os.environ["NLTK_DATA"] = str(ROOT / ".cache/nltk_data")

from finrobot.functional.quant_tools import load_config, load_data
from finrobot.functional.strategy_codegen import execute_strategy

folder = Path(sys.argv[1]).resolve()
try:
    result, checks = execute_strategy((folder / "strategy.py").read_text(encoding="utf-8"), load_data(), load_config())
    output = {"ok": True, "checks": checks, "result": result}
except Exception as exc:
    output = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
filename = "verification.json" if len(sys.argv) > 2 and sys.argv[2] == "verify" else "execution.json"
(folder / filename).write_text(json.dumps(output, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
print(json.dumps({key: value for key, value in output.items() if key != "result"}, ensure_ascii=True))
raise SystemExit(0 if output["ok"] else 1)
