"""Tax quota analysis module for statsbudget-mcp.

Provides data for visualizing Sweden's total tax pressure over time
using SCB's SkattekvotBNP table. Annotates major tax reforms for context.

This shows the descriptive relationship between tax quota and GDP,
not a causal Laffer curve. The data is useful for understanding how
tax pressure has evolved alongside economic growth and policy changes.

Data source: SCB PxWeb API, table SkattekvotBNP (1950-2025)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .scb_client import SCBClient

TAX_REFORMS: list[dict[str, Any]] = [
    {
        "year": 1971,
        "label": "S\u00e4rskild inkomstskatt inf\u00f6rs",
        "description": "Individuell beskattning inf\u00f6rs, skattetrycket \u00f6kar kraftigt",
    },
    {
        "year": 1976,
        "label": "Pomperipossa (102% marginalskatt)",
        "description": "Astrid Lindgren publicerar Pomperipossa i Monismanien. "
        "H\u00f6gsta marginalskatten \u00f6verstiger 100% f\u00f6r h\u00f6ginkomsttagare.",
    },
    {
        "year": 1983,
        "label": "Marginalskattereform",
        "description": "Marginalskatterna s\u00e4nks fr\u00e5n 87% till 80% (h\u00f6gsta skiktet)",
    },
    {
        "year": 1991,
        "label": "\u00c5rhundradets skattereform",
        "description": "Marginalskatt max 50%, bolagsskatt 30%, breddad bas. "
        "Skattekvoten sjunker fr\u00e5n ~53% till ~47% av BNP.",
    },
    {
        "year": 2007,
        "label": "Jobbskatteavdraget inf\u00f6rs",
        "description": "F\u00f6rsta steget av jobbskatteavdrag. S\u00e4nkt skatt p\u00e5 arbete.",
    },
    {
        "year": 2020,
        "label": "V\u00e4rnskatten avskaffas",
        "description": "H\u00f6gsta marginalskattesatsen s\u00e4nks fr\u00e5n ~57% till ~52%.",
    },
]


@dataclass
class LafferPoint:
    """A single year's observation for tax quota plotting."""

    year: int
    tax_quota_pct: float
    gdp_msek: float
    total_tax_msek: float | None
    nominal_gdp_growth_pct: float | None
    decade: str
    is_reform_year: bool
    reform_label: str | None


async def build_laffer_curve(
    scb: SCBClient,
    from_year: int = 1950,
    to_year: int = 2025,
) -> list[LafferPoint]:
    """Build tax quota dataset from SCB data.

    Returns a list of LafferPoint objects, one per year,
    with tax quota, GDP, and reform annotations.
    """
    raw = await scb.get_laffer_data(from_year=from_year, to_year=to_year)

    reform_years = {r["year"]: r["label"] for r in TAX_REFORMS}

    points: list[LafferPoint] = []
    for i, row in enumerate(raw):
        year = row["year"]
        tax_pct = row["tax_share_pct"]
        gdp = row["gdp_msek"]
        tax = row["total_tax_msek"]  # may be None

        if tax_pct is None or gdp is None:
            continue

        # Nominal GDP growth (not inflation-adjusted)
        nominal_growth: float | None = None
        if i + 1 < len(raw) and raw[i + 1]["gdp_msek"] is not None:
            next_gdp = raw[i + 1]["gdp_msek"]
            nominal_growth = round((next_gdp - gdp) / gdp * 100, 2)

        decade = f"{(year // 10) * 10}s"

        points.append(
            LafferPoint(
                year=year,
                tax_quota_pct=tax_pct,
                gdp_msek=gdp,
                total_tax_msek=tax,  # preserve None, don't convert to 0
                nominal_gdp_growth_pct=nominal_growth,
                decade=decade,
                is_reform_year=year in reform_years,
                reform_label=reform_years.get(year),
            )
        )

    return points


def laffer_to_chart_data(points: list[LafferPoint]) -> dict[str, Any]:
    """Convert LafferPoints to chart-ready format.

    Returns timeseries data grouped by decade with reform annotations
    and summary statistics. Suitable for line or area charts.

    Handles empty input gracefully (returns empty datasets + null summary).
    """
    if not points:
        return {
            "timeseries": [],
            "annotations": [],
            "axis_labels": {
                "x": "\u00c5r",
                "y": "Skattekvot (% av BNP)",
            },
            "summary": {
                "min_quota_pct": None,
                "max_quota_pct": None,
                "peak_year": None,
                "peak_quota_pct": None,
                "current_year": None,
                "current_quota_pct": None,
                "years_covered": 0,
            },
        }

    # Timeseries (replaces the broken scatter datasets)
    timeseries = [
        {
            "year": p.year,
            "tax_quota_pct": p.tax_quota_pct,
            "decade": p.decade,
            "gdp_msek": p.gdp_msek,
            "tax_msek": p.total_tax_msek,
        }
        for p in points
    ]

    annotations = [
        {
            "year": p.year,
            "tax_quota_pct": p.tax_quota_pct,
            "label": p.reform_label,
        }
        for p in points
        if p.is_reform_year
    ]

    tax_quotas = [p.tax_quota_pct for p in points]
    peak = max(points, key=lambda p: p.tax_quota_pct)
    current = points[-1]

    return {
        "timeseries": timeseries,
        "annotations": annotations,
        "axis_labels": {
            "x": "\u00c5r",
            "y": "Skattekvot (% av BNP)",
        },
        "summary": {
            "min_quota_pct": min(tax_quotas),
            "max_quota_pct": max(tax_quotas),
            "peak_year": peak.year,
            "peak_quota_pct": peak.tax_quota_pct,
            "current_year": current.year,
            "current_quota_pct": current.tax_quota_pct,
            "years_covered": len(points),
        },
    }


def laffer_timeseries(points: list[LafferPoint]) -> list[dict[str, Any]]:
    """Convert to flat timeseries for line chart visualization.

    Returns year, tax quota, GDP, tax amount, nominal growth,
    and reform label (if applicable) per year.
    """
    return [
        {
            "year": p.year,
            "tax_quota_pct": p.tax_quota_pct,
            "gdp_msek": p.gdp_msek,
            "total_tax_msek": p.total_tax_msek,
            "nominal_gdp_growth_pct": p.nominal_gdp_growth_pct,
            "reform": p.reform_label,
        }
        for p in points
    ]
