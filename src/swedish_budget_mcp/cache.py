"""SQLite cache for swedish-budget-mcp.

Persists parsed budget data locally so the server starts instantly
without re-downloading and re-parsing CSV files from Statskontoret
or re-querying SCB on every startup.

Schema:
- expenditure: parsed ExpenditureRow data
- income: parsed IncomeRow data
- scb_revenue: tax revenue snapshots
- scb_quota: tax quota snapshots
- meta: sync metadata (last sync time, source dates, schema version)

The cache is stored in ~/.swedish-budget-cache/swedish_budget.db by default.
Schema version is checked on load; mismatches are treated as cache misses.
A cache is only considered valid when both expenditure AND income are present.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS expenditure (
    expenditure_area_id TEXT NOT NULL,
    expenditure_area_name TEXT NOT NULL,
    appropriation_id TEXT NOT NULL,
    appropriation_name TEXT NOT NULL,
    year INTEGER NOT NULL,
    budget_msek REAL,
    amendment_budgets_msek REAL,
    outcome_msek REAL,
    opening_balance_msek REAL,
    closing_balance_msek REAL
);

CREATE TABLE IF NOT EXISTS income (
    income_type TEXT NOT NULL,
    income_type_name TEXT NOT NULL,
    income_main_group TEXT NOT NULL,
    income_main_group_name TEXT NOT NULL,
    income_title TEXT NOT NULL,
    income_title_name TEXT NOT NULL,
    year INTEGER NOT NULL,
    budget_msek REAL,
    outcome_msek REAL
);

CREATE TABLE IF NOT EXISTS scb_revenue (
    tax_type_code TEXT NOT NULL,
    tax_type_label TEXT NOT NULL,
    year INTEGER NOT NULL,
    amount_msek REAL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scb_quota (
    tax_type_code TEXT NOT NULL,
    tax_type_label TEXT NOT NULL,
    year INTEGER NOT NULL,
    amount_msek REAL,
    share_of_gdp REAL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_expenditure_year ON expenditure(year);
CREATE INDEX IF NOT EXISTS idx_income_year ON income(year);
CREATE INDEX IF NOT EXISTS idx_scb_revenue_year ON scb_revenue(year);
CREATE INDEX IF NOT EXISTS idx_scb_quota_year ON scb_quota(year);
"""

_EXPENDITURE_REQUIRED_KEYS = {
    "expenditure_area_id",
    "expenditure_area_name",
    "appropriation_id",
    "appropriation_name",
    "year",
}
_INCOME_REQUIRED_KEYS = {
    "income_type",
    "income_type_name",
    "income_main_group",
    "income_main_group_name",
    "income_title",
    "income_title_name",
    "year",
}


def _validate_rows(
    rows: list[dict[str, Any]], required_keys: set[str],
) -> bool:
    """Validate that all rows contain required keys with correct types."""
    if not rows:
        return True
    for row in rows:
        if not isinstance(row, dict):
            return False
        if not required_keys.issubset(row.keys()):
            return False
        if not isinstance(row.get("year"), (int, float)):
            return False
    return True


class BudgetCache:
    """SQLite-backed cache for budget data.

    Schema-versioned: data is only loaded if the stored schema version
    matches SCHEMA_VERSION exactly. Mismatches are treated as cache
    misses (data is discarded, not migrated).

    A cache is only considered populated when BOTH expenditure and
    income datasets have rows and the snapshot is marked complete.

    Usage:
        cache = BudgetCache()
        cache.store_snapshot(expenditure_rows, income_rows)  # atomic
        rows = cache.load_expenditure(year=2024)
        cache.close()
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            cache_dir = Path.home() / ".swedish-budget-cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            db_path = cache_dir / "swedish_budget.db"
        self._db_path = Path(db_path)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _schema_valid(self) -> bool:
        """Check if cached data matches current schema version."""
        stored = self.get_meta("schema_version")
        return stored == SCHEMA_VERSION

    def _snapshot_complete(self) -> bool:
        """Check if the last snapshot wrote both datasets."""
        return self.get_meta("snapshot_complete") == "true"

    def is_populated(self) -> bool:
        """Check if cache has valid, complete data.

        Requires: correct schema version, snapshot marked complete,
        and both expenditure and income have rows.
        """
        if not self._schema_valid():
            return False
        if not self._snapshot_complete():
            return False
        exp_count = self._conn.execute(
            "SELECT COUNT(*) FROM expenditure",
        ).fetchone()[0]
        inc_count = self._conn.execute(
            "SELECT COUNT(*) FROM income",
        ).fetchone()[0]
        return exp_count > 0 and inc_count > 0

    def get_meta(self, key: str) -> str | None:
        cur = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,),
        )
        row = cur.fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        self._conn.commit()

    def cache_age_hours(self) -> float | None:
        """Hours since last successful sync, or None if never synced."""
        last = self.get_meta("last_sync_utc")
        if not last:
            return None
        try:
            synced = datetime.fromisoformat(last)
            delta = datetime.now(UTC) - synced
            return delta.total_seconds() / 3600
        except (ValueError, TypeError):
            return None

    def needs_refresh(self, max_age_hours: float = 24 * 7) -> bool:
        """Check if cache is stale (default: older than 1 week)."""
        if not self._schema_valid():
            return True
        if not self._snapshot_complete():
            return True
        age = self.cache_age_hours()
        if age is None:
            return True
        return age > max_age_hours

    def invalidate(self) -> None:
        """Clear all cached data (schema mismatch recovery)."""
        tables = (
            "expenditure", "income", "scb_revenue",
            "scb_quota", "meta",
        )
        for table in tables:
            self._conn.execute(f"DELETE FROM {table}")
        self._conn.commit()

    # -- Atomic snapshot --

    def store_snapshot(
        self,
        expenditure: list[dict[str, Any]],
        income: list[dict[str, Any]],
        sync_utc: str | None = None,
    ) -> dict[str, int]:
        """Atomically store both expenditure and income datasets.

        Writes both tables + metadata in a single transaction.
        If either dataset is empty, the snapshot is still stored
        but snapshot_complete is set to false.
        """
        if sync_utc is None:
            sync_utc = datetime.now(UTC).isoformat(
                timespec="seconds",
            )

        self._conn.execute("DELETE FROM expenditure")
        self._conn.execute("DELETE FROM income")

        exp_count = 0
        if expenditure:
            self._conn.executemany(
                "INSERT INTO expenditure VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        r["expenditure_area_id"],
                        r["expenditure_area_name"],
                        r["appropriation_id"],
                        r["appropriation_name"],
                        r["year"],
                        r.get("budget_msek"),
                        r.get("amendment_budgets_msek"),
                        r.get("outcome_msek"),
                        r.get("opening_balance_msek"),
                        r.get("closing_balance_msek"),
                    )
                    for r in expenditure
                ],
            )
            exp_count = len(expenditure)

        inc_count = 0
        if income:
            self._conn.executemany(
                "INSERT INTO income VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        r["income_type"],
                        r["income_type_name"],
                        r["income_main_group"],
                        r["income_main_group_name"],
                        r["income_title"],
                        r["income_title_name"],
                        r["year"],
                        r.get("budget_msek"),
                        r.get("outcome_msek"),
                    )
                    for r in income
                ],
            )
            inc_count = len(income)

        complete = exp_count > 0 and inc_count > 0
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("schema_version", SCHEMA_VERSION),
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("snapshot_complete", "true" if complete else "false"),
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("last_sync_utc", sync_utc),
        )
        self._conn.commit()

        return {
            "expenditure": exp_count,
            "income": inc_count,
            "complete": complete,
        }

    # -- Expenditure --

    def store_expenditure(self, rows: list[dict[str, Any]]) -> int:
        """Store expenditure rows, replacing existing data."""
        self._conn.execute("DELETE FROM expenditure")
        self._conn.executemany(
            "INSERT INTO expenditure VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    r["expenditure_area_id"],
                    r["expenditure_area_name"],
                    r["appropriation_id"],
                    r["appropriation_name"],
                    r["year"],
                    r.get("budget_msek"),
                    r.get("amendment_budgets_msek"),
                    r.get("outcome_msek"),
                    r.get("opening_balance_msek"),
                    r.get("closing_balance_msek"),
                )
                for r in rows
            ],
        )
        self.set_meta("schema_version", SCHEMA_VERSION)
        self._conn.commit()
        return len(rows)

    def load_expenditure(
        self, year: int | None = None,
    ) -> list[dict[str, Any]]:
        if not self._schema_valid():
            print(
                "Cache schema mismatch, treating as empty.",
                file=sys.stderr,
            )
            return []
        if year is not None:
            cur = self._conn.execute(
                "SELECT * FROM expenditure WHERE year = ?", (year,),
            )
        else:
            cur = self._conn.execute("SELECT * FROM expenditure")
        rows = [dict(row) for row in cur.fetchall()]
        if not _validate_rows(rows, _EXPENDITURE_REQUIRED_KEYS):
            print(
                "Cache expenditure data malformed, treating as empty.",
                file=sys.stderr,
            )
            return []
        return rows

    # -- Income --

    def store_income(self, rows: list[dict[str, Any]]) -> int:
        self._conn.execute("DELETE FROM income")
        self._conn.executemany(
            "INSERT INTO income VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    r["income_type"],
                    r["income_type_name"],
                    r["income_main_group"],
                    r["income_main_group_name"],
                    r["income_title"],
                    r["income_title_name"],
                    r["year"],
                    r.get("budget_msek"),
                    r.get("outcome_msek"),
                )
                for r in rows
            ],
        )
        self.set_meta("schema_version", SCHEMA_VERSION)
        self._conn.commit()
        return len(rows)

    def load_income(
        self, year: int | None = None,
    ) -> list[dict[str, Any]]:
        if not self._schema_valid():
            print(
                "Cache schema mismatch, treating as empty.",
                file=sys.stderr,
            )
            return []
        if year is not None:
            cur = self._conn.execute(
                "SELECT * FROM income WHERE year = ?", (year,),
            )
        else:
            cur = self._conn.execute("SELECT * FROM income")
        rows = [dict(row) for row in cur.fetchall()]
        if not _validate_rows(rows, _INCOME_REQUIRED_KEYS):
            print(
                "Cache income data malformed, treating as empty.",
                file=sys.stderr,
            )
            return []
        return rows

    # -- SCB Revenue --

    def store_scb_revenue(self, rows: list[dict[str, Any]]) -> int:
        self._conn.execute("DELETE FROM scb_revenue")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        self._conn.executemany(
            "INSERT INTO scb_revenue VALUES (?,?,?,?,?)",
            [
                (
                    r["tax_type_code"],
                    r["tax_type_label"],
                    r["year"],
                    r.get("amount_msek"),
                    now,
                )
                for r in rows
            ],
        )
        self.set_meta("schema_version", SCHEMA_VERSION)
        self._conn.commit()
        return len(rows)

    def load_scb_revenue(
        self, year: int | None = None,
    ) -> list[dict[str, Any]]:
        if not self._schema_valid():
            return []
        if year is not None:
            cur = self._conn.execute(
                "SELECT * FROM scb_revenue WHERE year = ?", (year,),
            )
        else:
            cur = self._conn.execute("SELECT * FROM scb_revenue")
        return [dict(row) for row in cur.fetchall()]

    # -- SCB Quota --

    def store_scb_quota(self, rows: list[dict[str, Any]]) -> int:
        self._conn.execute("DELETE FROM scb_quota")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        self._conn.executemany(
            "INSERT INTO scb_quota VALUES (?,?,?,?,?,?)",
            [
                (
                    r["tax_type_code"],
                    r["tax_type_label"],
                    r["year"],
                    r.get("amount_msek"),
                    r.get("share_of_gdp"),
                    now,
                )
                for r in rows
            ],
        )
        self.set_meta("schema_version", SCHEMA_VERSION)
        self._conn.commit()
        return len(rows)

    def load_scb_quota(
        self, year: int | None = None,
    ) -> list[dict[str, Any]]:
        if not self._schema_valid():
            return []
        if year is not None:
            cur = self._conn.execute(
                "SELECT * FROM scb_quota WHERE year = ?", (year,),
            )
        else:
            cur = self._conn.execute("SELECT * FROM scb_quota")
        return [dict(row) for row in cur.fetchall()]

    # -- Summary --

    def get_stats(self) -> dict[str, Any]:
        """Cache statistics for diagnostics."""
        counts = {}
        tables = (
            "expenditure", "income", "scb_revenue", "scb_quota",
        )
        for table in tables:
            cur = self._conn.execute(
                f"SELECT COUNT(*) FROM {table}",
            )
            counts[table] = cur.fetchone()[0]

        years: set[int] = set()
        for table in ("expenditure", "income"):
            cur = self._conn.execute(
                f"SELECT DISTINCT year FROM {table}",
            )
            years.update(row[0] for row in cur.fetchall())

        return {
            "db_path": str(self._db_path),
            "schema_version": SCHEMA_VERSION,
            "schema_valid": self._schema_valid(),
            "snapshot_complete": self._snapshot_complete(),
            "row_counts": counts,
            "total_rows": sum(counts.values()),
            "years_covered": sorted(years),
            "last_sync": self.get_meta("last_sync_utc"),
            "cache_age_hours": self.cache_age_hours(),
            "needs_refresh": self.needs_refresh(),
        }
