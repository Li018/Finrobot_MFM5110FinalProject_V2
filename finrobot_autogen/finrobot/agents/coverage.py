"""Explicit watchlists and research theses, without market or model requests.

Inspired by the later FinRobot Coverage Desk's persisted ticker annotations.
The caller chooses the database; this store owns only ``coverage_items``.
Timestamps record user edits, not the as-of time of prices or financial data.
"""

import contextlib
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3


_CHINA_SYMBOL = re.compile(r"(?:(?:SH|SZ|BJ)([0-9]{6})|([0-9]{6})(?:\.(?:SH|SZ|BJ))?)\Z")
_US_SYMBOL = re.compile(r"[A-Z][A-Z0-9]{0,9}(?:[.-][A-Z0-9]{1,4})?\Z")
_MENTIONS = re.compile(
    r"(?<![A-Za-z0-9])(?P<cn>(?:(?:SH|SZ|BJ))?[0-9]{6}(?:\.(?:SH|SZ|BJ))?)(?![A-Za-z0-9])"
    r"|(?:\$|\b(?:NASDAQ|NYSE|AMEX)\s*:|\b(?:ticker|symbol)\s*[:=]\s*)"
    r"(?P<us>[A-Za-z][A-Za-z0-9]{0,9}(?:[.-][A-Za-z0-9]{1,4})?)(?![A-Za-z0-9.-])",
    re.I,
)
_STATUSES = {"watching", "researching", "archived"}
_STATUS_ALIASES = {"关注": "watching", "研究": "researching", "归档": "archived"}
_TEXT_FIELDS = {"note", "thesis", "catalysts", "invalidation"}
_EDITABLE_FIELDS = _TEXT_FIELDS | {"status", "priority"}


def normalize_symbol(symbol):
    """Canonicalize A-share aliases or a US ticker; do not verify a listing."""
    if not isinstance(symbol, str):
        raise ValueError("symbol must be text")
    symbol = symbol.strip().upper()
    chinese = _CHINA_SYMBOL.fullmatch(symbol)
    if chinese:
        return chinese.group(1) or chinese.group(2)
    if len(symbol) <= 15 and _US_SYMBOL.fullmatch(symbol):
        return symbol
    raise ValueError("Use a six-digit A-share code or a US ticker such as AAPL or BRK.B")


def extract_symbols(text):
    """Find codes and explicit US mentions ($AAPL, NASDAQ:AAPL, ticker=AAPL).

    Generic capitalized words such as PDF, JSON and SMA are not ticker hints.
    A bare six-digit code is only a syntactic candidate, not a verified listing.
    """
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    symbols = []
    for match in _MENTIONS.finditer(text):
        symbol = normalize_symbol(match.group("cn") or match.group("us"))
        if symbol not in symbols:
            symbols.append(symbol)
    return symbols


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _status(value):
    if not isinstance(value, str):
        raise ValueError("status must be watching, researching or archived")
    value = value.strip().lower()
    value = _STATUS_ALIASES.get(value, value)
    if value not in _STATUSES:
        raise ValueError("status must be watching, researching or archived")
    return value


def _changes(values):
    unknown = values.keys() - _EDITABLE_FIELDS
    if unknown:
        raise ValueError("Unknown coverage field: " + ", ".join(sorted(unknown)))
    output = dict(values)
    for field in _TEXT_FIELDS & values.keys():
        if not isinstance(values[field], str):
            raise ValueError(field + " must be text; use an empty string to clear it")
    if "status" in values:
        output["status"] = _status(values["status"])
    if "priority" in values and (type(values["priority"]) is not int or not 0 <= values["priority"] <= 5):
        raise ValueError("priority must be an integer between 0 and 5")
    return output


class ResearchCoverage:
    """User-maintained coverage; omitted update fields are never overwritten."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS coverage_items (
                symbol TEXT PRIMARY KEY,
                note TEXT NOT NULL DEFAULT '',
                thesis TEXT NOT NULL DEFAULT '',
                catalysts TEXT NOT NULL DEFAULT '',
                invalidation TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'watching'
                    CHECK(status IN ('watching', 'researching', 'archived')),
                priority INTEGER NOT NULL DEFAULT 0 CHECK(priority BETWEEN 0 AND 5),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")

    @contextlib.contextmanager
    def _db(self):
        db = sqlite3.connect(str(self.path), timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA synchronous=NORMAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, symbol, *, note="", thesis="", catalysts="", invalidation="",
            status="watching", priority=0):
        symbol = normalize_symbol(symbol)
        values = _changes({"note": note, "thesis": thesis, "catalysts": catalysts,
                           "invalidation": invalidation, "status": status, "priority": priority})
        now = _now()
        with self._db() as db:
            cursor = db.execute("""INSERT INTO coverage_items
                (symbol, note, thesis, catalysts, invalidation, status, priority, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(symbol) DO NOTHING""",
                (symbol, values["note"], values["thesis"], values["catalysts"],
                 values["invalidation"], values["status"], values["priority"], now, now))
            if cursor.rowcount == 0:
                raise ValueError(symbol + " is already tracked; use update to change its fields")
            return dict(db.execute("SELECT * FROM coverage_items WHERE symbol = ?", (symbol,)).fetchone())

    def update(self, symbol, /, **changes):
        symbol = normalize_symbol(symbol)
        values = _changes(changes)
        with self._db() as db:
            if values:
                # All field names come from the fixed allowlist above. User data
                # remains parameterized, including apostrophes and JSON text.
                assignment = ", ".join(field + " = ?" for field in values)
                cursor = db.execute("UPDATE coverage_items SET " + assignment + ", updated_at = ? WHERE symbol = ?",
                                    (*values.values(), _now(), symbol))
                if cursor.rowcount == 0:
                    raise ValueError(symbol + " is not tracked; add it first")
            row = db.execute("SELECT * FROM coverage_items WHERE symbol = ?", (symbol,)).fetchone()
            if row is None:
                raise ValueError(symbol + " is not tracked; add it first")
            return dict(row)

    def get(self, symbol):
        symbol = normalize_symbol(symbol)
        with self._db() as db:
            row = db.execute("SELECT * FROM coverage_items WHERE symbol = ?", (symbol,)).fetchone()
        return dict(row) if row is not None else None

    def list(self, *, status=None, query="", limit=50):
        """List stored annotations, ordered by priority and most recent edit."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        if not isinstance(query, str):
            raise ValueError("query must be text")
        filters, parameters = [], []
        if status is not None:
            filters.append("status = ?")
            parameters.append(_status(status))
        query = query.strip()
        if query:
            try:
                query = normalize_symbol(query)
            except ValueError:
                pass
            filters.append("instr(lower(symbol || ' ' || note || ' ' || thesis || ' ' || catalysts || ' ' || invalidation), lower(?)) > 0")
            parameters.append(query)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        with self._db() as db:
            rows = db.execute("SELECT * FROM coverage_items" + where +
                              " ORDER BY priority DESC, updated_at DESC, symbol ASC LIMIT ?", (*parameters, limit)).fetchall()
        return [dict(row) for row in rows]

    def remove(self, symbol):
        symbol = normalize_symbol(symbol)
        with self._db() as db:
            return db.execute("DELETE FROM coverage_items WHERE symbol = ?", (symbol,)).rowcount > 0
