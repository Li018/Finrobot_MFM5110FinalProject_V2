"""Immutable local research snapshots and deterministic, scope-aware comparison.

Inspired by the later FinRobot artifact store and semantic report comparison.
This AutoGen adaptation archives existing completed memory turns. It neither
opens referenced files nor asks a model to calculate or explain numeric changes.
"""

import contextlib
from datetime import date, datetime, timezone
import difflib
import json
import math
from pathlib import Path
import re
import sqlite3

from .coverage import extract_symbols, normalize_symbol


MAX_MODEL_CHARS = 12000
_CONFIG_FIELDS = {
    "symbol", "start_date", "end_date", "adjust", "fast", "slow", "initial_cash",
    "max_position", "lot_size", "commission", "min_commission", "slippage",
    "annual_periods", "risk_free_rate",
}
_SCOPE_FIELDS = {"provider", "source_tool", "unit", "formula", "price_adjustment",
                 "as_of", "data_date", "actual_start", "actual_end"}
_QUANT_TOOLS = {"compare_quant_strategies", "check_quant_risk", "execute_quant_code",
                "backtest_reference", "read_backtest_result"}
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
# Units come from the existing Backtrader result contract, not name heuristics.
_METRIC_UNITS = {
    "total_return": "fraction", "annual_return": "fraction", "max_drawdown": "fraction",
    "sharpe": "ratio", "observations": "count", "return_periods": "count",
    "executed_orders": "count", "initial_cash": "account_cash_unit",
    "final_equity": "account_cash_unit", "total_commission": "account_cash_unit",
    "min_cash": "account_cash_unit",
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def _positive(value, name, maximum=None):
    maximum = (2 ** 63 - 1) if maximum is None else maximum
    if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
        raise ValueError(f"{name} must be a positive integer" + (f" <= {maximum}" if maximum else ""))
    return value


def _complete_config(config):
    """A present but unknown/invalid cost or date is not a known scope."""
    if not _CONFIG_FIELDS <= config.keys():
        return False
    numbers = _CONFIG_FIELDS - {"symbol", "start_date", "end_date", "adjust"}
    if any(type(config[key]) not in (int, float) or not math.isfinite(config[key]) for key in numbers):
        return False
    if any(type(config[key]) is not int or config[key] < 1 for key in ("fast", "slow", "lot_size", "annual_periods")):
        return False
    if not config["fast"] < config["slow"] or config["initial_cash"] <= 0 or not 0 < config["max_position"] <= 1:
        return False
    if any(config[key] < 0 for key in ("commission", "min_commission", "slippage")) or config["adjust"] not in {"", "qfq", "hfq"}:
        return False
    try:
        normalize_symbol(config["symbol"])
        return date.fromisoformat(config["start_date"]) < date.fromisoformat(config["end_date"])
    except (TypeError, ValueError):
        return False


def _walk(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _references(evidence):
    symbols, experiments, paths = set(), set(), set()
    for key, value in _walk(evidence):
        if not isinstance(value, str):
            continue
        if key in {"symbol", "ticker"}:
            try:
                symbols.add(normalize_symbol(value))
            except ValueError:
                pass
        elif key == "experiment_id":
            experiments.add(value)
        elif key == "path" or key.endswith(("_path", "_directory")):
            paths.add(value)
    return sorted(symbols), sorted(experiments), sorted(paths)


def _summary(record):
    return {key: record[key] for key in (
        "id", "source_turn_id", "session_id", "created_at", "archived_at", "roles", "symbols", "requested_symbols",
    )} | {"question": record["question"][:240], "answer_preview": record["answer"][:400],
         "preview_truncated": len(record["question"]) > 240 or len(record["answer"]) > 400}


def _bounded(record, *, text_fields=(), list_fields=()):
    """Shorten prose and omit whole records, never fabricate partial references."""
    result = json.loads(_json(record))
    result["truncated"] = False

    def container(path):
        parts = path.split(".")
        parent = result
        for part in parts[:-1]:
            parent = parent[part]
        return parent, parts[-1]

    for path, limit in text_fields:
        parent, field = container(path)
        if len(parent.get(field, "")) > limit:
            parent[field] = parent[field][:limit] + "…"
            result["truncated"] = True
    for path in list_fields:
        parent, field = container(path)
        while len(_json(result)) > MAX_MODEL_CHARS and parent.get(field):
            parent[field].pop()
            result["truncated"] = True
    # JSON escapes can take more than one character per source character.
    for path, _ in text_fields:
        parent, field = container(path)
        while len(_json(result)) > MAX_MODEL_CHARS and parent.get(field):
            parent[field] = parent[field][:-250]
            result["truncated"] = True
    return result


def _series(evidence):
    """Use only known tool-result metric contracts; prose is never parsed."""
    output = {}
    for row in evidence:
        tool, metrics = row.get("tool"), row.get("metrics")
        if tool not in _QUANT_TOOLS or not isinstance(metrics, dict):
            continue
        experiment = row.get("experiment_id")
        related = [item for item in evidence if experiment and item.get("experiment_id") == experiment] or [row]
        config, config_conflicts = {}, []
        for item in related:
            for key, value in item.get("config", {}).items():
                if key in config and config[key] != value:
                    config_conflicts.append(key)
                else:
                    config[key] = value
        symbol = config.get("symbol", row.get("symbol", row.get("ticker")))
        try:
            symbol = normalize_symbol(symbol)
        except ValueError:
            symbol = None
        if "symbol" in config and symbol:
            config["symbol"] = symbol
        scope = {"symbol": symbol, "config": config, "experiment_id": experiment,
                 "ambiguous_config": bool(config_conflicts), "config_conflicts": sorted(set(config_conflicts))}
        scope["data_fingerprints"] = sorted({value.lower() for key, value in _walk(related)
            if key in {"data_sha256", "clean_sha256"} and isinstance(value, str) and _SHA256.fullmatch(value)})
        for field in sorted(_SCOPE_FIELDS):
            values = sorted({_json(value) for key, value in _walk(related) if key == field})
            if values:
                scope[field] = [json.loads(value) for value in values]
        groups = [(row.get("strategy", "unspecified"), metrics)] if any(key in _METRIC_UNITS for key in metrics) else metrics.items()
        for strategy, values in groups:
            if not isinstance(values, dict):
                continue
            known = {key: value for key, value in values.items() if key in _METRIC_UNITS
                     and (value is None or type(value) in (int, float) and math.isfinite(value))}
            if not known:
                continue
            identity = (tool, str(strategy), symbol or "")
            entry = {"tool": tool, "strategy": str(strategy), "symbol": symbol,
                     "scope": scope, "metrics": known}
            if identity in output and output[identity] != entry:
                # Two distinct experiments/results in one answer cannot be
                # silently reduced to whichever tool happened to run last.
                output[identity]["ambiguous_results"] = True
            else:
                output[identity] = entry
    return output


def _scope_changes(before, after, prefix=""):
    changes = []
    for key in sorted(before.keys() | after.keys()):
        old, new = before.get(key), after.get(key)
        if old == new and (key in before) == (key in after):
            continue
        path = prefix + key
        if isinstance(old, dict) and isinstance(new, dict):
            changes.extend(_scope_changes(old, new, path + "."))
        else:
            changes.append({"field": path, "old": old, "new": new})
    return changes


def _comparison(before, after):
    old_series, new_series = _series(before["evidence"]), _series(after["evidence"])
    changes, metrics = [], []
    if before["symbols"] != after["symbols"]:
        changes.append({"field": "symbols", "old": before["symbols"], "new": after["symbols"]})
    if old_series.keys() != new_series.keys():
        changes.append({"field": "series", "old": [list(key) for key in sorted(old_series)],
                        "new": [list(key) for key in sorted(new_series)]})
    for identity in sorted(old_series.keys() | new_series.keys()):
        old, new = old_series.get(identity), new_series.get(identity)
        reasons = []
        if old is None or new is None:
            reasons.append("missing_series")
        else:
            differences = _scope_changes(old["scope"], new["scope"])
            changes.extend({"series": list(identity), **item} for item in differences)
            if old.get("ambiguous_results") or new.get("ambiguous_results"):
                reasons.append("multiple_results_for_same_series")
            if not old["scope"]["symbol"] or not new["scope"]["symbol"]:
                reasons.append("unverified_symbol")
            if identity[1] == "unspecified":
                reasons.append("unknown_strategy")
            if old["scope"]["ambiguous_config"] or new["scope"]["ambiguous_config"]:
                reasons.append("conflicting_experiment_config")
            if not all(_complete_config(item["scope"]["config"]) for item in (old, new)):
                reasons.append("incomplete_backtest_scope")
            if any(item["field"] != "experiment_id" for item in differences):
                reasons.append("scope_changed")
            same_experiment = old["scope"]["experiment_id"] and old["scope"]["experiment_id"] == new["scope"]["experiment_id"]
            same_data = old["scope"]["data_fingerprints"] and old["scope"]["data_fingerprints"] == new["scope"]["data_fingerprints"]
            if not same_experiment and not same_data:
                reasons.append("unverified_data_snapshot")
        old_values, new_values = (old or {}).get("metrics", {}), (new or {}).get("metrics", {})
        for metric in sorted(old_values.keys() | new_values.keys()):
            a, b = old_values.get(metric), new_values.get(metric)
            field_reasons = reasons + (["missing_value"] if a is None or b is None else [])
            unit = _METRIC_UNITS[metric]
            comparable = not field_reasons
            metrics.append({"tool": identity[0], "strategy": identity[1], "symbol": identity[2] or None,
                "metric": metric, "unit": unit, "old": a, "new": b, "comparable": comparable,
                "delta": ((b - a) * (100 if unit == "fraction" else 1)) if comparable else None,
                "delta_unit": "percentage_points" if unit == "fraction" else unit,
                "reasons": field_reasons})
    return {"a": _summary(before), "b": _summary(after),
        "identical": all(before[key] == after[key] for key in ("question", "answer", "evidence")),
        "answer_diff": "\n".join(difflib.unified_diff(before["answer"].splitlines(), after["answer"].splitlines(),
            fromfile=f"report-{before['id']}", tofile=f"report-{after['id']}", lineterm="")),
        "scope_changes": changes, "metrics": metrics,
        "limits": ["Only persisted tool evidence supplies numbers; changed prose is not verified financial data.",
                   "Account cash units follow the saved experiment; no currency is inferred.",
                   "A numeric difference describes saved results, not its cause or a strategy improvement."]}


class ResearchArchive:
    """Archive completed turns from the same SQLite database as ResearchMemory."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS research_artifacts (id INTEGER PRIMARY KEY AUTOINCREMENT, source_turn_id INTEGER NOT NULL UNIQUE, session_id TEXT NOT NULL, created_at TEXT NOT NULL, archived_at TEXT NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS research_artifact_symbols (artifact_id INTEGER NOT NULL REFERENCES research_artifacts(id), symbol TEXT NOT NULL, PRIMARY KEY(artifact_id, symbol))")
            db.execute("CREATE INDEX IF NOT EXISTS research_symbol_history ON research_artifact_symbols(symbol, artifact_id DESC)")

    @contextlib.contextmanager
    def _db(self):
        db = sqlite3.connect(str(self.path), timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _record(row):
        if row is None:
            return None
        result = dict(row)
        result.update(json.loads(result.pop("payload")))
        return result

    def archive_turn(self, turn_id):
        """Capture one exact completed turn; repeated requests return its snapshot."""
        _positive(turn_id, "turn_id")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            saved = db.execute("SELECT * FROM research_artifacts WHERE source_turn_id = ?", (turn_id,)).fetchone()
            if saved is not None:
                return self._record(saved)
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='turns'").fetchone():
                raise ValueError("No saved research turns; use the same database as Memory")
            turn = db.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
            if turn is None:
                raise ValueError("Saved turn not found")
            if turn["status"] != "completed":
                raise ValueError("Only completed turns can be archived")
            evidence = json.loads(turn["evidence"])
            symbols, experiments, paths = _references(evidence)
            requested = sorted(extract_symbols(turn["question"]))
            payload = {"roles": json.loads(turn["roles"]), "evidence": evidence, "symbols": symbols,
                       "requested_symbols": requested, "experiment_ids": experiments, "artifact_paths": paths}
            cursor = db.execute("INSERT INTO research_artifacts (source_turn_id, session_id, created_at, archived_at, question, answer, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (turn_id, turn["session_id"], turn["created_at"], datetime.now(timezone.utc).isoformat(),
                 turn["question"], turn["answer"], _json(payload)))
            report_id = cursor.lastrowid
            db.executemany("INSERT INTO research_artifact_symbols (artifact_id, symbol) VALUES (?, ?)",
                           [(report_id, symbol) for symbol in sorted(set(symbols) | set(requested))])
            return self._record(db.execute("SELECT * FROM research_artifacts WHERE id = ?", (report_id,)).fetchone())

    def get(self, report_id, *, for_model=False):
        with self._db() as db:
            record = self._record(db.execute("SELECT * FROM research_artifacts WHERE id = ?",
                                            (_positive(report_id, "report_id"),)).fetchone())
        if record is None or not for_model:
            return record
        return _bounded(record, text_fields=(("question", 1000), ("answer", 3500)),
                        list_fields=("evidence", "artifact_paths", "experiment_ids", "roles", "symbols", "requested_symbols"))

    def find(self, symbol=None, query="", limit=10, *, for_model=False):
        """Look up explicit requested targets or separately labelled tool targets."""
        _positive(limit, "limit", maximum=100)
        if not isinstance(query, str) or len(query) > 256:
            raise ValueError("query must be text of at most 256 characters")
        conditions, arguments = [], []
        if symbol is not None:
            conditions.append("id IN (SELECT artifact_id FROM research_artifact_symbols WHERE symbol = ?)")
            arguments.append(normalize_symbol(symbol))
        if query.strip():
            conditions.append("instr(lower(question || ' ' || answer || ' ' || payload), lower(?)) > 0")
            arguments.append(query.strip())
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self._db() as db:
            rows = db.execute("SELECT * FROM research_artifacts" + where + " ORDER BY id DESC LIMIT ?",
                              (*arguments, limit + 1)).fetchall()
        result = {"reports": [_summary(self._record(row)) for row in rows[:limit]], "has_more": len(rows) > limit}
        if for_model:
            while len(_json(result)) > MAX_MODEL_CHARS:
                result["reports"].pop()
                result["has_more"] = True
        return result

    def compare(self, a_id, b_id, *, for_model=False):
        before, after = self.get(a_id), self.get(b_id)
        if before is None or after is None:
            raise ValueError("Report not found; use find() to list saved report IDs")
        result = _comparison(before, after)
        if for_model:
            result = _bounded(result, text_fields=(("answer_diff", 4000), ("a.question", 240),
                ("a.answer_preview", 400), ("b.question", 240), ("b.answer_preview", 400)),
                list_fields=("metrics", "scope_changes", "a.roles", "b.roles", "a.symbols", "b.symbols",
                             "a.requested_symbols", "b.requested_symbols"))
        return result
