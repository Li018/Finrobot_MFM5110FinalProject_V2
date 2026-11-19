"""Repeat offline checks and write machine-readable validation evidence."""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/quant_v2"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    checks = []
    commands = [("unit_tests", ["-X", "utf8", "-m", "unittest", "discover", "-s", "tests", "-v"]),
                ("dependency_check", ["-m", "pip", "check"]),
                ("dialogue_offline", ["-X", "utf8", "scripts/validate_quant_dialogue.py", "--offline"]),
                ("offline_replay", ["-X", "utf8", "finrobot_autogen/experiments/run_quant_experiment.py", "verify"]),
                ("resume_pipeline", ["-X", "utf8", "finrobot_autogen/experiments/run_quant_experiment.py", "all", "--resume"]),
                ("generalization", ["-X", "utf8", "finrobot_autogen/experiments/run_generalization.py", "evaluate"]),
                ("stability_replay", ["-X", "utf8", "finrobot_autogen/experiments/run_stability.py", "verify"]),
                ("factor_ideation_replay", ["-X", "utf8", "finrobot_autogen/experiments/run_factor_ideation.py", "evaluate"]),
                ("methodology", ["-X", "utf8", "scripts/evaluate_agent_methodology.py"])]
    for name, command in commands:
        started = time.perf_counter()
        proc = subprocess.run([args.python, *command], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=240)
        log = proc.stdout + proc.stderr
        (OUT / (name + ".txt")).write_text(log, encoding="utf-8")
        row = {"check": name, "ok": proc.returncode == 0, "seconds": round(time.perf_counter() - started, 3)}
        if name == "unit_tests":
            match = re.search(r"Ran (\d+) tests", log)
            row["tests"] = int(match.group(1)) if match else None
        checks.append(row)
        print(json.dumps(row), flush=True)
    result = {"ok": all(row["ok"] for row in checks), "python_environment": Path(args.python).parent.parent.name,
              "api_calls_requested": 0, "checks": checks}
    (OUT / "validation_summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
