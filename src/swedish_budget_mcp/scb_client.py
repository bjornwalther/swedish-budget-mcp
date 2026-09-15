"""SCB PxWeb API client for swedish-budget-mcp.

Provides async access to SCB's statistical database (statistikdatabasen)
for tax revenue and tax quota data. Uses the PxWeb v1 REST API with
JSON response format.

Data sources:
- SkatteIntakt: Tax revenue by type, 2000-2024, MSEK
- SkattekvotBNP: Tax quota as % of GDP, 1950-2025
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import Any

import httpx

BASE_URL = "https://api.scb.se/OV0104/v1/doris/sv/ssd/START/NR/NR0103/NR0103J"

TABLES = {
    "tax_revenue": "SkatteIntakt",
    "tax_quota": "SkattekvotBNP",
}

MAX_RETRIES = 2
RETRY_BASE_DELAY = 1.0  # seconds, doubles each retry

# Explicit allowlist of transient exceptions that warrant retry.
# All other exceptions propagate immediately.
_RETRYABLE_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.HTTPStatusError,
)

TAX_TYPE_LABELS: dict[str, str] = {
    "101": "Skatt p\u00e5 arbete",
    "102": "Direkta skatter p\u00e5 arbete",
    "103": "Kommunal skatt",
    "104": "Statlig skatt",
    "105": "Allm\u00e4n pensionsavgift",
    "106": "Skattereduktioner m.m.",
    "107": "Artistskatt m.m.",
    "120": "Indirekta skatter p\u00e5 arbete",
    "121": "Arbetsgivaravgifter",
    "122": "Egenavgifter",
    "123": "S\u00e4rskild l\u00f6neskatt",
    "124": "Neds\u00e4ttningar",
    "125": "Tj\u00e4nstegruppliv m.m.",
    "126": "Avgifter till premiepensionssystemet",
    "140": "Skatt p\u00e5 kapital",
    "141": "Skatt p\u00e5 kapital, hush\u00e5ll",
    "142": "Skatt p\u00e5 bolagsvinster",
    "143": "Avkastningsskatt",
    "144": "Fastighetsskatt",
    "145": "St\u00e4mpelskatt",
    "146": "Kupongskatt m.m.",
    "147": "Riskskatt f\u00f6r kreditinstitut",
    "160": "Skatt p\u00e5 konsumtion och insatsvaror",
    "161": "Merv\u00e4rdesskatt",
    "162": "Skatt p\u00e5 tobak",
    "163": "Skatt p\u00e5 etylalkohol",
    "164": "Skatt p\u00e5 vin m.m.",
    "165": "Skatt p\u00e5 \u00f6l",
    "166": "Energiskatt",
    "167": "Koldioxidskatt",
    "168": "\u00d6vriga skatter p\u00e5 energi och milj\u00f6",
    "169": "Skatt p\u00e5 v\u00e4gtrafik",
    "170": "Skatt p\u00e5 import",
    "171": "\u00d6vriga skatter",
    "180": "Restf\u00f6rda och \u00f6vriga skatter",
    "181": "Restf\u00f6rda skatter",
    "182": "\u00d6vriga skatter",
    "190": "Totala skatteint\u00e4kter",
    "191": "varav EU-skatter",
    "192": "varav Offentliga sektorns skatteint\u00e4kter",
}

QUOTA_TYPE_LABELS: dict[str, str] = {
    "101": "BNP till marknadspris",
    "102": "Totala skatter",
    "201": "Skatter p\u00e5 produktion och import",
    "202": "Skatter p\u00e5 produktion och import till EU",
    "203": "L\u00f6pande inkomst- och f\u00f6rm\u00f6genhetsskatter",
    "204": "Sociala avgifter, obligatoriska",
    "301": "Sociala avgifter",
    "302": "Arbetsgivares frivilliga sociala avgifter",
    "303": "Arbetsgivares avtalsenliga sociala avgifter",
    "304": "Tillr\u00e4knade sociala avgifter",
    "305": "Frivilliga kompletterande pensionsavgifter",
    "306": "Sociala f\u00f6rs\u00e4kringssystemets admin.avgifter",
    "205": "Kapitalskatter",
}


def _is_retryable(exc: Exception) -> bool:
    """Check if an exception warrants a retry.

    Only called for exceptions in _RETRYABLE_EXCEPTIONS.
    TimeoutException and ConnectError are always retryable.
    HTTPStatusError is retryable only for 5xx and 429.
    """
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return False


@dataclass
class TaxRevenueRow:
    """A single tax revenue observation."""

    tax_type_code: str
    tax_type_label: str
    year: int
    amount_msek: float | None


@dataclass
class TaxQuotaRow:
    """A single tax quota observation (amount + share of GDP)."""

    tax_type_code: str
    tax_type_label: str
    year: int
    amount_msek: float | None
    share_of_gdp: float | None


class SCBClient:
    """Async client for SCB PxWeb API (tax/revenue tables).

    Includes exponential backoff retry on timeout, connection errors,
    and server errors (5xx/429). All other exceptions propagate
    immediately without retry.

    Usage:
        async with SCBClient() as scb:
            data = await scb.get_tax_revenue(years=[2023, 2024])
    """

    def __init__(self, timeout: float = 30.0) -> None:
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> SCBClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def _post_query(self, table: str, query: dict[str, Any]) -> dict[str, Any]:
        """POST a PxWeb query with retry on transient failures.

        Retries only on explicitly allowlisted exceptions:
        TimeoutException, ConnectError, HTTPStatusError (5xx/429).
        Unknown exceptions (e.g. JSONDecodeError, 4xx client errors)
        propagate immediately.
        """
        url = f"{BASE_URL}/{table}"
        last_exc: Exception | None = None

        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = await self._client.post(url, json=query)
                resp.raise_for_status()
                return resp.json()
            except _RETRYABLE_EXCEPTIONS as exc:
                last_exc = exc
                if attempt < MAX_RETRIES and _is_retryable(exc):
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    print(
                        f"SCB API retry {attempt + 1}/{MAX_RETRIES} "
                        f"after {type(exc).__name__}, waiting {delay}s",
                        file=sys.stderr,
                    )
                    await asyncio.sleep(delay)
                else:
                    raise

        raise last_exc  # type: ignore[misc]

    def _build_query(
        self,
        tax_types: list[str],
        years: list[str],
        contents: list[str],
    ) -> dict[str, Any]:
        """Build a PxWeb JSON query payload."""
        return {
            "query": [
                {"code": "Skattetyp", "selection": {"filter": "item", "values": tax_types}},
                {"code": "ContentsCode", "selection": {"filter": "item", "values": contents}},
                {"code": "Tid", "selection": {"filter": "item", "values": years}},
            ],
            "response": {"format": "json"},
        }

    def _parse_flat_response(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Parse PxWeb flat JSON response into list of row dicts."""
        columns = data["columns"]
        rows: list[dict[str, Any]] = []
        for entry in data["data"]:
            keys = entry["key"]
            values = entry["values"]
            row: dict[str, Any] = {}
            key_idx = 0
            val_idx = 0
            for col in columns:
                if col["type"] == "c":
                    raw = values[val_idx]
                    row[col["code"]] = None if raw in ("", "..", ".") else float(raw)
                    val_idx += 1
                else:
                    row[col["code"]] = keys[key_idx]
                    key_idx += 1
            rows.append(row)
        return rows

    async def get_tax_revenue(
        self,
        years: list[int] | None = None,
        tax_types: list[str] | None = None,
    ) -> list[TaxRevenueRow]:
        """Fetch tax revenue data from SCB SkatteIntakt table.

        Args:
            years: List of years to query (default: 2000-2024).
            tax_types: List of tax type codes (default: all).

        Returns:
            List of TaxRevenueRow with amounts in MSEK.
        """
        if years is None:
            years = list(range(2000, 2025))
        if tax_types is None:
            tax_types = list(TAX_TYPE_LABELS.keys())

        year_strs = [str(y) for y in years]
        query = self._build_query(tax_types, year_strs, ["000000TE"])
        raw = await self._post_query(TABLES["tax_revenue"], query)
        parsed = self._parse_flat_response(raw)

        return [
            TaxRevenueRow(
                tax_type_code=row["Skattetyp"],
                tax_type_label=TAX_TYPE_LABELS.get(row["Skattetyp"], row["Skattetyp"]),
                year=int(row["Tid"]),
                amount_msek=row.get("000000TE"),
            )
            for row in parsed
        ]

    async def get_tax_revenue_summary(
        self,
        years: list[int] | None = None,
    ) -> list[TaxRevenueRow]:
        """Fetch top-level tax revenue categories only.

        Returns labour, capital, consumption, other, and total.
        """
        top_level = ["101", "140", "160", "180", "190"]
        return await self.get_tax_revenue(years=years, tax_types=top_level)

    async def get_tax_quota(
        self,
        years: list[int] | None = None,
        tax_types: list[str] | None = None,
    ) -> list[TaxQuotaRow]:
        """Fetch tax quota data (amount + share of GDP).

        Args:
            years: List of years to query (default: 1950-2025).
            tax_types: List of quota type codes (default: all).

        Returns:
            List of TaxQuotaRow with amounts in MSEK and GDP share in %.
        """
        if years is None:
            years = list(range(1950, 2026))
        if tax_types is None:
            tax_types = list(QUOTA_TYPE_LABELS.keys())

        year_strs = [str(y) for y in years]
        query = self._build_query(tax_types, year_strs, ["000000T8", "000000SP"])
        raw = await self._post_query(TABLES["tax_quota"], query)
        parsed = self._parse_flat_response(raw)

        grouped: dict[tuple[str, str], dict[str, float | None]] = {}
        for row in parsed:
            key = (row["Skattetyp"], row["Tid"])
            if key not in grouped:
                grouped[key] = {"amount": None, "share": None}
            if row.get("000000T8") is not None:
                grouped[key]["amount"] = row["000000T8"]
            if row.get("000000SP") is not None:
                grouped[key]["share"] = row["000000SP"]

        results: list[TaxQuotaRow] = []
        for (type_code, year_str), vals in grouped.items():
            results.append(
                TaxQuotaRow(
                    tax_type_code=type_code,
                    tax_type_label=QUOTA_TYPE_LABELS.get(type_code, type_code),
                    year=int(year_str),
                    amount_msek=vals["amount"],
                    share_of_gdp=vals["share"],
                )
            )
        return results

    async def get_laffer_data(
        self,
        from_year: int = 1950,
        to_year: int = 2025,
    ) -> list[dict[str, Any]]:
        """Get data for Laffer curve visualization.

        Returns year, GDP (MSEK), total tax (MSEK), and tax as % of GDP
        for each year in range.
        """
        years = list(range(from_year, to_year + 1))
        rows = await self.get_tax_quota(years=years, tax_types=["101", "102"])

        by_year: dict[int, dict[str, float | None]] = {}
        for row in rows:
            if row.year not in by_year:
                by_year[row.year] = {
                    "gdp_msek": None,
                    "total_tax_msek": None,
                    "tax_share_pct": None,
                }
            if row.tax_type_code == "101":
                by_year[row.year]["gdp_msek"] = row.amount_msek
            elif row.tax_type_code == "102":
                by_year[row.year]["total_tax_msek"] = row.amount_msek
                by_year[row.year]["tax_share_pct"] = row.share_of_gdp

        return [
            {"year": y, **vals}
            for y, vals in sorted(by_year.items())
            if vals["tax_share_pct"] is not None
        ]

    async def get_revenue_timeseries(
        self,
        from_year: int = 2000,
        to_year: int = 2024,
    ) -> list[dict[str, Any]]:
        """Get revenue timeseries grouped by top-level tax category.

        Returns list of dicts with year + amounts per category (labour,
        capital, consumption, other, total) in MSEK.
        """
        years = list(range(from_year, to_year + 1))
        rows = await self.get_tax_revenue_summary(years=years)

        by_year: dict[int, dict[str, float | None]] = {}
        for row in rows:
            if row.year not in by_year:
                by_year[row.year] = {}
            label_map = {
                "101": "labour",
                "140": "capital",
                "160": "consumption",
                "180": "other",
                "190": "total",
            }
            key = label_map.get(row.tax_type_code, row.tax_type_code)
            by_year[row.year][key] = row.amount_msek

        return [{"year": y, **vals} for y, vals in sorted(by_year.items())]
