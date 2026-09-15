"""FastMCP server for swedish-budget-mcp.

Exposes Swedish national budget outturn data and tax revenue as MCP tools.
Entry point for `uvx swedish-budget-mcp` and Claude Desktop integration.

Startup behavior:
1. Open SQLite cache (~/.swedish-budget-cache/swedish_budget.db)
2. If cache has data and is fresh (< 1 week): load from cache (ms)
3. If cache is empty or stale: sync from Statskontoret, save to cache
4. SCB data is fetched on-demand (with retry) and cached per session
"""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

import httpx
from fastmcp import FastMCP

from .cache import BudgetCache
from .laffer import (
    TAX_REFORMS,
    build_laffer_curve,
    laffer_timeseries,
    laffer_to_chart_data,
)
from .scb_client import SCBClient
from .statskontoret import (
    BALANCE_MSEK_NOTE,
    NEXT_UPDATE_NOTE,
    TOTAL_EXPENDITURE_NOTE,
    ExpenditureRow,
    IncomeRow,
    StatskontoretClient,
    SyncError,
)

_scb: SCBClient | None = None
_sk: StatskontoretClient | None = None
_cache: BudgetCache | None = None

_SYNC_ERRORS = (
    httpx.HTTPError,
    httpx.TimeoutException,
    OSError,
    SyncError,
    ValueError,
)

_CACHE_LOAD_ERRORS = (
    KeyError,
    TypeError,
    ValueError,
    sqlite3.Error,
)


def _rows_to_dicts(rows: list) -> list[dict[str, Any]]:
    """Convert dataclass rows to dicts for cache storage."""
    return [asdict(r) for r in rows]


def _with_rank(
    rows: list[dict[str, Any]], value_key: str,
) -> list[dict[str, Any]]:
    """Attach 1-based `rank` (by value_key descending) without reordering.

    Ties broken by original position for deterministic output.
    """
    order = sorted(
        range(len(rows)),
        key=lambda i: (-(rows[i].get(value_key) or 0), i),
    )
    ranks = {idx: rank for rank, idx in enumerate(order, start=1)}
    return [
        {**row, "rank": ranks[i]}
        for i, row in enumerate(rows)
    ]


def _serialize_source(s: Any) -> dict[str, Any]:
    """Serialize a DataSourceMeta to a stable dict.

    Single source of truth for source serialization,
    used by both sync_budget_data and get_sync_status.
    """
    return {
        "source": s.source,
        "description": s.description,
        "publication_cadence": s.publication_cadence,
        "last_synced_at": s.last_synced_at,
        "source_last_updated": s.source_last_updated,
        "files_downloaded": s.files_downloaded,
        "years_covered": s.years_covered,
        "income_revision": s.income_revision,
    }


@asynccontextmanager
async def lifespan(server: FastMCP):
    """Initialize clients, load or sync data, tear down on exit."""
    global _scb, _sk, _cache
    _scb = SCBClient()
    _sk = StatskontoretClient()
    _cache = BudgetCache()

    if _cache.is_populated() and not _cache.needs_refresh():
        try:
            _load_from_cache(_sk, _cache)
            print(
                f"Loaded from cache ({_cache.db_path}), "
                f"age: {_cache.cache_age_hours():.1f}h",
                file=sys.stderr,
            )
        except _CACHE_LOAD_ERRORS as exc:
            print(
                f"Cache load failed "
                f"({type(exc).__name__}): {exc}",
                file=sys.stderr,
            )
            print(
                "Will sync fresh data instead.",
                file=sys.stderr,
            )
            _cache.invalidate()
            try:
                await _sync_and_cache(_sk, _cache)
            except _SYNC_ERRORS as sync_exc:
                print(
                    f"Sync also failed: {sync_exc}. "
                    f"Starting empty.",
                    file=sys.stderr,
                )
    else:
        print(
            "Cache empty or stale, syncing...",
            file=sys.stderr,
        )
        try:
            await _sync_and_cache(_sk, _cache)
            print(
                "Sync complete, data cached.",
                file=sys.stderr,
            )
        except _SYNC_ERRORS as exc:
            print(
                f"Sync failed ({type(exc).__name__}): "
                f"{exc}",
                file=sys.stderr,
            )
            if _cache.is_populated():
                try:
                    _load_from_cache(_sk, _cache)
                    print(
                        "Fell back to stale cache.",
                        file=sys.stderr,
                    )
                except _CACHE_LOAD_ERRORS as load_exc:
                    print(
                        f"Stale cache also unusable: "
                        f"{load_exc}",
                        file=sys.stderr,
                    )
            else:
                print(
                    "No cached data available. "
                    "Starting empty.",
                    file=sys.stderr,
                )

    try:
        yield
    finally:
        if _scb:
            await _scb.close()
        if _sk:
            await _sk.close()
        if _cache:
            _cache.close()
        _scb = None
        _sk = None
        _cache = None


def _load_from_cache(
    sk: StatskontoretClient, cache: BudgetCache,
) -> None:
    """Populate the Statskontoret client from cached data."""
    exp_rows = cache.load_expenditure()
    inc_rows = cache.load_income()
    sk._expenditure_data = [
        ExpenditureRow(**{k: v for k, v in r.items()})
        for r in exp_rows
    ]
    sk._income_data = [
        IncomeRow(**{k: v for k, v in r.items()})
        for r in inc_rows
    ]


async def _sync_and_cache(
    sk: StatskontoretClient, cache: BudgetCache,
) -> None:
    """Sync from Statskontoret and persist to cache."""
    await sk.sync()
    cache.store_snapshot(
        expenditure=_rows_to_dicts(sk.expenditure_data),
        income=_rows_to_dicts(sk.income_data),
    )


mcp = FastMCP(
    "swedish-budget-mcp",
    instructions=(
        "Swedish national budget data: expenditure outturn "
        "by area, tax revenue from SCB, and tax quota "
        "analysis. Budget data shows actual outturn (utfall) "
        "from Statskontoret, not the originally proposed "
        "budget. Data from SCB and Statskontoret."
    ),
    lifespan=lifespan,
)


def _require_scb() -> SCBClient:
    if _scb is None:
        raise RuntimeError(
            "SCB client not initialized. "
            "Server not started?"
        )
    return _scb


def _require_sk() -> StatskontoretClient:
    if _sk is None:
        raise RuntimeError(
            "Statskontoret client not initialized. "
            "Server not started?"
        )
    return _sk


def _require_cache() -> BudgetCache:
    if _cache is None:
        raise RuntimeError(
            "Cache not initialized. "
            "Server not started?"
        )
    return _cache


EXPENDITURE_AREAS = [
    ("01", "Rikets styrelse"),
    ("02", "Samh\u00e4llsekonomi och finansf\u00f6rvaltning"),
    ("03", "Skatt, tull och exekution"),
    ("04", "R\u00e4ttsv\u00e4sendet"),
    ("05", "Internationell samverkan"),
    ("06", "F\u00f6rsvar och samh\u00e4llets krisberedskap"),
    ("07", "Internationellt bist\u00e5nd"),
    ("08", "Migration"),
    ("09", "H\u00e4lsov\u00e5rd, sjukv\u00e5rd och social omsorg"),
    ("10", "Ekonomisk trygghet vid sjukdom"
     " och funktionsneds\u00e4ttning"),
    ("11", "Ekonomisk trygghet vid \u00e5lderdom"),
    ("12", "Ekonomisk trygghet f\u00f6r familjer och barn"),
    ("13", "Integration och j\u00e4mst\u00e4lldhet"),
    ("14", "Arbetsmarknad och arbetsliv"),
    ("15", "Studiest\u00f6d"),
    ("16", "Utbildning och universitetsforskning"),
    ("17", "Kultur, medier, trossamfund och fritid"),
    ("18", "Samh\u00e4llsplanering, bostadsf\u00f6rs\u00f6rjning"
     " och byggande samt konsumentpolitik"),
    ("19", "Regional utveckling"),
    ("20", "Allm\u00e4n milj\u00f6- och naturv\u00e5rd"),
    ("21", "Energi"),
    ("22", "Kommunikationer"),
    ("23", "Areella n\u00e4ringar, landsbygd och livsmedel"),
    ("24", "N\u00e4ringsliv"),
    ("25", "Allm\u00e4nna bidrag till kommuner"),
    ("26", "Statsskulds\u00e4ntor m.m."),
    ("27", "Avgiften till Europeiska unionen"),
]

_REVENUE_CATEGORY_MAP = {
    "101": "labour",
    "140": "capital",
    "160": "consumption",
    "180": "other",
    "190": "total",
}


@mcp.tool()
async def get_budget_overview(
    year: int,
) -> dict[str, Any]:
    """Get the Swedish national budget outturn for a year.

    Returns actual expenditure outturn (not proposed budget),
    total income, balance, and all 27 expenditure areas in MSEK.
    Source: Statskontoret arsutfall (official statistics).

    balance_msek is income minus the sum of the 27 expenditure
    areas' outturn. It is NOT the official central-government
    budget balance (budgetsaldo), which also includes net lending
    and a cash adjustment from Riksgalden that this server does
    not source. See balance_note.

    total_expenditure_msek is the sum of the 27 areas' outturn.
    It is NOT "takbegransade utgifter" (the expenditure-ceiling
    figure most often cited in Swedish budget coverage), which
    excludes interest (area 26) but adds the old-age pension
    system, which this server does not source. See
    total_expenditure_note.

    Each area's budget_msek is the decided budget (statens
    budget, before amendments); outcome_msek is outturn. Neither
    the originally proposed budget (regeringens proposition) nor
    ESV forecasts are available from this data source. Areas can
    also be broader than narrower media-defined categories (e.g.
    "Forsvar och samhallets krisberedskap" includes civil crisis
    preparedness alongside military defense) - always check
    area_name before comparing to a headline figure.

    Args:
        year: Budget year. Expenditure available 1997-2025;
            total_income_msek/balance_msek only meaningful from
            2006 (income data starts then).
    """
    sk = _require_sk()
    overview = sk.get_budget_overview(year)
    areas = _with_rank(
        [
            {
                "area_id": a.area_id,
                "area_name": a.area_name,
                "budget_msek": a.budget_msek,
                "outcome_msek": a.outcome_msek,
                "delta_msek": a.delta_msek,
            }
            for a in overview.areas
        ],
        "outcome_msek",
    )
    return {
        "year": overview.year,
        "data_type": "outturn",
        "source": "Statskontoret",
        "as_of": sk.get_sync_status().last_sync,
        "total_expenditure_msek": (
            overview.total_expenditure_msek
        ),
        "total_expenditure_note": TOTAL_EXPENDITURE_NOTE,
        "total_income_msek": overview.total_income_msek,
        "balance_msek": overview.balance_msek,
        "balance_note": BALANCE_MSEK_NOTE,
        "areas": areas,
    }


@mcp.tool()
async def get_expenditure_area(
    area_id: str, year: int,
) -> dict[str, Any]:
    """Drill down into a specific expenditure area.

    Returns all appropriations with budget vs outturn in MSEK.
    Source: Statskontoret. budget_msek is the decided budget,
    amendment_budgets_msek is amendments, outcome_msek is outturn.
    Neither the originally proposed budget nor ESV forecasts are
    available from this data source.

    The area total can be broader than a narrower media-defined
    category with a similar name (e.g. area 06 "Forsvar och
    samhallets krisberedskap" includes civil crisis preparedness
    alongside military defense, so it will not match a headline
    "forsvarsbudget" figure that covers military defense only) -
    check area_name and the appropriation names before comparing
    to an externally reported total.

    Args:
        area_id: Two-digit area ID (e.g. "06" for Defence).
        year: Budget year (1997-2025).
    """
    sk = _require_sk()
    rows = sk.get_expenditure_area(area_id, year)
    appropriations = _with_rank(
        [
            {
                "appropriation_id": r.appropriation_id,
                "appropriation_name": (
                    r.appropriation_name
                ),
                "budget_msek": r.budget_msek,
                "amendment_budgets_msek": (
                    r.amendment_budgets_msek
                ),
                "outcome_msek": r.outcome_msek,
                "opening_balance_msek": (
                    r.opening_balance_msek
                ),
                "closing_balance_msek": (
                    r.closing_balance_msek
                ),
            }
            for r in rows
        ],
        "outcome_msek",
    )
    return {
        "data_type": "outturn",
        "source": "Statskontoret",
        "as_of": sk.get_sync_status().last_sync,
        "area_id": area_id,
        "year": year,
        "appropriations": appropriations,
    }


@mcp.tool()
async def compare_budgets(
    year_a: int, year_b: int,
) -> dict[str, Any]:
    """Compare budget outturn between two years, per expenditure area.

    Both years are outturn data (never mixed with budget/forecast
    stages). delta_pct is null when the baseline year is zero or
    the area didn't exist that year, to avoid a misleading percentage.

    Args:
        year_a: First year (baseline).
        year_b: Second year (comparison).
    """
    sk = _require_sk()
    comparisons = sk.compare_budgets(year_a, year_b)
    return {
        "data_type": "outturn_comparison",
        "source": "Statskontoret",
        "as_of": sk.get_sync_status().last_sync,
        "year_a": year_a,
        "year_b": year_b,
        "areas": comparisons,
    }


@mcp.tool()
async def get_biggest_changes(
    year_a: int,
    year_b: int,
    area_id: str | None = None,
    top_n: int = 5,
) -> dict[str, Any]:
    """Get the biggest outturn increases and decreases between two years.

    Built on the same comparison as compare_budgets - both years are
    outturn data, never mixed with budget/forecast stages. delta_pct
    is null when the baseline is zero or the area/appropriation
    didn't exist that year. Ties broken deterministically by ID.

    Args:
        year_a: First year (baseline).
        year_b: Second year (comparison).
        area_id: If given, compare appropriations within this
            expenditure area instead of comparing the 27 areas.
        top_n: Number of increases and decreases to return each
            (default 5).
    """
    sk = _require_sk()
    result = sk.get_biggest_changes(
        year_a, year_b, area_id=area_id, top_n=top_n,
    )
    return {
        "data_type": (
            "appropriation_change"
            if area_id
            else "area_change"
        ),
        "source": "Statskontoret",
        "as_of": sk.get_sync_status().last_sync,
        "year_a": year_a,
        "year_b": year_b,
        "area_id": area_id,
        "top_n": top_n,
        "increases": result["increases"],
        "decreases": result["decreases"],
    }


@mcp.tool()
async def sync_budget_data(
    year: int | None = None,
) -> dict[str, Any]:
    """Download and parse latest budget outturn.

    Persists data atomically to SQLite cache.
    Raises SyncError if either dataset is missing.
    """
    sk = _require_sk()
    cache = _require_cache()
    status = await sk.sync(year=year)

    result = cache.store_snapshot(
        expenditure=_rows_to_dicts(sk.expenditure_data),
        income=_rows_to_dicts(sk.income_data),
    )

    return {
        "last_sync": status.last_sync,
        "next_expected_update": (
            status.next_expected_update
        ),
        "next_expected_update_note": NEXT_UPDATE_NOTE,
        "cache_stats": cache.get_stats(),
        "snapshot": result,
        "sources": [
            _serialize_source(s)
            for s in status.sources
        ],
    }


@mcp.tool()
async def get_revenue(year: int) -> dict[str, Any]:
    """Get tax revenue breakdown for a specific year.

    Returns total and per-category (labour, capital,
    consumption) in MSEK. Source: SCB PxWeb API.
    """
    scb = _require_scb()
    rows = await scb.get_tax_revenue_summary(
        years=[year],
    )
    result: dict[str, float | None] = {}
    for row in rows:
        key = _REVENUE_CATEGORY_MAP.get(
            row.tax_type_code, row.tax_type_code,
        )
        result[key] = row.amount_msek
    return {
        "year": year,
        "data_type": "tax_revenue",
        "source": "SCB",
        "revenue_msek": result,
    }


@mcp.tool()
async def get_revenue_timeseries(
    from_year: int = 2000, to_year: int = 2024,
) -> list[dict[str, Any]]:
    """Get tax revenue timeseries by category. Source: SCB."""
    scb = _require_scb()
    return await scb.get_revenue_timeseries(
        from_year=from_year, to_year=to_year,
    )


@mcp.tool()
async def get_revenue_detail(
    year: int, tax_types: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Get detailed tax revenue for specific types. Source: SCB."""
    scb = _require_scb()
    rows = await scb.get_tax_revenue(
        years=[year], tax_types=tax_types,
    )
    return [
        {
            "tax_type_code": r.tax_type_code,
            "tax_type_label": r.tax_type_label,
            "year": r.year,
            "amount_msek": r.amount_msek,
        }
        for r in rows
    ]


@mcp.tool()
async def get_laffer_data(
    from_year: int = 1950, to_year: int = 2025,
) -> dict[str, Any]:
    """Get tax quota data: tax pressure vs GDP. Source: SCB."""
    scb = _require_scb()
    points = await build_laffer_curve(
        scb, from_year=from_year, to_year=to_year,
    )
    return laffer_to_chart_data(points)


@mcp.tool()
async def get_laffer_timeseries(
    from_year: int = 1950, to_year: int = 2025,
) -> list[dict[str, Any]]:
    """Get tax quota timeseries with reform annotations."""
    scb = _require_scb()
    points = await build_laffer_curve(
        scb, from_year=from_year, to_year=to_year,
    )
    return laffer_timeseries(points)


@mcp.tool()
async def get_tax_reforms() -> list[dict[str, Any]]:
    """Get list of major Swedish tax reforms."""
    return TAX_REFORMS


@mcp.tool()
async def get_sync_status() -> dict[str, Any]:
    """Get data freshness information."""
    sk = _require_sk()
    cache = _require_cache()
    status = sk.get_sync_status()
    return {
        "last_sync": status.last_sync,
        "next_expected_update": (
            status.next_expected_update
        ),
        "next_expected_update_note": NEXT_UPDATE_NOTE,
        "cache": cache.get_stats(),
        "sources": [
            _serialize_source(s)
            for s in status.sources
        ],
    }


@mcp.tool()
async def get_publication_schedule() -> dict[str, Any]:
    """Get the Statskontoret data publication schedule."""
    sk = _require_sk()
    return sk.get_publication_schedule()


@mcp.tool()
async def get_available_years() -> dict[str, Any]:
    """Get list of years with loaded outturn data."""
    sk = _require_sk()
    return {"years": sk.get_available_years()}


@mcp.tool()
async def get_cache_stats() -> dict[str, Any]:
    """Get SQLite cache diagnostics."""
    cache = _require_cache()
    return cache.get_stats()


@mcp.resource("budget://areas")
async def budget_areas() -> str:
    """All 27 Swedish expenditure areas (id, name)."""
    return json.dumps(
        [
            {"area_id": aid, "area_name": name}
            for aid, name in EXPENDITURE_AREAS
        ],
        ensure_ascii=False,
        indent=2,
    )


def main():
    """Run the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
