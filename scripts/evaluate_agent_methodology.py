"""Write report-ready methodology metrics from saved V2 artifacts."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "finrobot_autogen"))

from finrobot.functional.methodology import build_metrics, render_markdown


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, default=ROOT / "artifacts")
    parser.add_argument("--out", type=Path, default=ROOT / "artifacts/quant_v2/methodology_metrics")
    args = parser.parse_args()
    metrics = build_metrics(args.artifacts.resolve())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.with_suffix(".json").write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    args.out.with_suffix(".md").write_text(render_markdown(metrics), encoding="utf-8")
    summary = {
        "generations": metrics["generation"]["attempts"],
        "first_json_valid_rate": metrics["generation"]["initial_json_valid_rate"],
        "first_execution_rate": metrics["generation"]["initial_execution_success_rate"],
        "final_rate": metrics["generation"]["final_validated_success_rate"],
        "numeric_accuracy": metrics["numeric_fidelity"]["accuracy"],
        "generalization_cases": metrics["generalization"].get("cases"),
    }
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    print("Wrote:", args.out.with_suffix(".json").relative_to(ROOT), flush=True)
    print("Wrote:", args.out.with_suffix(".md").relative_to(ROOT), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
