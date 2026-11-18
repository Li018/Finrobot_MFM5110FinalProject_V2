"""Small data and backtest tools for the V2 course experiment."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import numpy as np
import pandas as pd

from .quantitative import reference_signals, run_signal_backtest, save_backtest

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "finrobot_autogen/experiments/quant_v2_config.json"
DATA = ROOT / "data/quant_v2"
ARTIFACTS = ROOT / "artifacts/quant_v2"


def load_config():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def clean_market_data(frame, config):
    frame = frame.copy()
    frame.index = pd.to_datetime(frame.index, errors="coerce")
    frame.index.name = "Date"
    frame = frame.rename(columns={name: name.capitalize() for name in frame.columns})
    columns = ["Open", "High", "Low", "Close", "Volume"]
    if not set(columns).issubset(frame.columns):
        raise ValueError("Missing OHLCV columns")
    rows_in = len(frame)
    frame = frame.loc[~frame.index.isna()]
    frame = frame.loc[(frame.index >= config["start_date"]) & (frame.index <= config["end_date"])]
    duplicate_rows = int(frame.index.duplicated(keep="last").sum())
    frame = frame.loc[~frame.index.duplicated(keep="last")].sort_index()
    frame = frame[columns].apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan)
    missing_rows = int(frame.isna().any(axis=1).sum())
    frame = frame.dropna()
    if frame.empty:
        raise ValueError("No complete market observations")
    if (frame[["Open", "High", "Low", "Close"]] <= 0).any().any() or (frame.Volume < 0).any():
        raise ValueError("Nonpositive prices or negative volume")
    if ((frame.High < frame[["Open", "Low", "Close"]].max(axis=1)) |
        (frame.Low > frame[["Open", "High", "Close"]].min(axis=1))).any():
        raise ValueError("Inconsistent OHLC bounds")
    frame["Volume"] *= 100  # Adapter uses lots; the experiment feed uses shares.
    record = {"input_rows": rows_in, "output_rows": len(frame), "duplicate_dates_removed": duplicate_rows,
              "missing_rows_removed": missing_rows, "date_sorted": True,
              "volume_conversion": "adapter lots multiplied by 100 to shares",
              "missing_dates": "No imputation; no synthetic bars added", "adjust": config["adjust"]}
    return frame, record


def prepare_data(refresh=False):
    cfg = load_config()
    DATA.mkdir(parents=True, exist_ok=True)
    if (DATA / "prices.csv").exists() and not refresh:
        return json.loads(QuantTools.get_data_summary())
    from finrobot.data_source import CNStockUtils
    data = CNStockUtils.get_stock_data(cfg["symbol"], cfg["start_date"], cfg["end_date"], adjust=cfg["adjust"])
    attrs = dict(data.attrs)
    data.to_csv(DATA / "adapter_raw.csv")
    clean, record = clean_market_data(data, cfg)
    clean.to_csv(DATA / "prices.csv", float_format="%.12g")
    record.update(symbol=cfg["symbol"], requested_start=cfg["start_date"], requested_end=cfg["end_date"],
                  actual_start=str(clean.index.min().date()), actual_end=str(clean.index.max().date()),
                  fetched_at=datetime.now(timezone.utc).isoformat(), provider=attrs,
                  raw_definition="CNStockUtils response after adapter column/date/unit normalization",
                  raw_sha256=hashlib.sha256((DATA / "adapter_raw.csv").read_bytes()).hexdigest(),
                  clean_sha256=hashlib.sha256((DATA / "prices.csv").read_bytes()).hexdigest())
    (DATA / "metadata.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return record


def load_data():
    data = pd.read_csv(DATA / "prices.csv", index_col="Date", parse_dates=True)
    meta = json.loads((DATA / "metadata.json").read_text(encoding="utf-8"))
    if hashlib.sha256((DATA / "prices.csv").read_bytes()).hexdigest() != meta["clean_sha256"]:
        raise ValueError("Saved market snapshot hash mismatch")
    return data


class QuantTools:
    @staticmethod
    def get_data_summary() -> str:
        """Read the fixed V2 market snapshot provenance and schema; never invent missing data."""
        data = load_data()
        return json.dumps({"metadata": json.loads((DATA / "metadata.json").read_text(encoding="utf-8")),
                           "columns": list(data.columns), "rows": len(data),
                           "config": load_config()}, ensure_ascii=False)

    @staticmethod
    def backtest_reference(strategy: Annotated[str, "sma or buy_hold"] = "sma") -> str:
        """Run a fixed traditional strategy with the saved V2 data, costs and risk limits."""
        if strategy not in ("sma", "buy_hold"):
            raise ValueError("Choose sma or buy_hold")
        cfg, data = load_config(), load_data()
        signals = reference_signals(data, cfg["fast"], cfg["slow"]) if strategy == "sma" else pd.Series(1, index=data.index)
        result = run_signal_backtest(data, signals, cfg)
        folder = ARTIFACTS / strategy
        save_backtest(result, folder)
        return json.dumps({"strategy": strategy, "metrics": result["metrics"],
                           "result_path": str((folder / "result.json").relative_to(ROOT))}, ensure_ascii=False)

    @staticmethod
    def read_backtest_result(strategy: Annotated[str, "sma or buy_hold"] = "sma") -> str:
        """Read already executed V2 results; numerical claims must match these metrics exactly."""
        if strategy not in ("sma", "buy_hold"):
            raise ValueError("Choose sma or buy_hold")
        result = json.loads((ARTIFACTS / strategy / "result.json").read_text(encoding="utf-8"))
        return json.dumps({"strategy": strategy, "metrics": result["metrics"],
                           "risk_events": result["risk_events"]}, ensure_ascii=False)
