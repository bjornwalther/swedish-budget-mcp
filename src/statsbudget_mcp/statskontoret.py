"""Statskontoret open data client for statsbudget-mcp.

Downloads and parses annual budget outcome data (arsutfall) from
Statskontoret's open data pages. Data is delivered as semicolon-separated
CSV files inside ZIP archives.

File format:
- Encoding: UTF-8
- Separator: semicolon (;)
- Decimal: comma (,)
- Amounts: millions SEK with up to 8 decimals
- First row: column headers

Data covers 2006-2025 (expenditure) and includes both budget and outcome.
"""

from __future__ import annotations

import csv
import html as html_mod
import io
import json
import re
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

BASE_URL = "https://www.statskontoret.se"
ARSUTFALL_PAGE = "/analys-och-statistik/oppna-data/arsutfall/"
ALLOWED_HOSTS = {"www.statskontoret.se", "statskontoret.se"}

MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024  # 50 MB per file
MAX_CSV_BYTES = 100 * 1024 * 1024  # 100 MB decompressed CSV

# Required semantic columns that must be found in actual CSV
# headers. If any resolve only to a default name that doesn't
# appear in fieldnames, the CSV schema has changed.
_EXP_REQUIRED_KEYS = {"year", "outcome", "area_id"}
_INC_REQUIRED_KEYS = {"year", "outcome", "income_type"}

# Post-parse validation thresholds
_MIN_OUTCOME_RATIO = 0.1  # >= 10% of rows need non-None outcome
_MIN_YEAR = 1990
_MAX_YEAR = 2100
_MIN_DISTINCT_IDS = 2  # >= 2 unique area/income type IDs

HREF_RE = re.compile(
    r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
    re.IGNORECASE,
)
TAG_RE = re.compile(r"<[^>]+>")
UPDATED_RE = re.compile(r"Senast uppdaterad\s+(\d{4}-\d{2}-\d{2})")


class SyncError(Exception):
    """Raised when a sync cannot produce a complete snapshot."""

    def __init__(
        self,
        message: str,
        has_expenditure: bool = False,
        has_income: bool = False,
    ) -> None:
        super().__init__(message)
        self.has_expenditure = has_expenditure
        self.has_income = has_income


@dataclass
class DataSourceMeta:
    source: str
    description: str
    publication_cadence: str
    last_synced_at: str | None = None
    source_last_updated: str | None = None
    files_downloaded: list[str] = field(default_factory=list)
    years_covered: list[int] = field(default_factory=list)
    income_revision: str | None = None


@dataclass
class SyncStatus:
    last_sync: str | None = None
    next_expected_update: str | None = None
    sources: list[DataSourceMeta] = field(default_factory=list)


@dataclass
class ExpenditureRow:
    expenditure_area_id: str
    expenditure_area_name: str
    appropriation_id: str
    appropriation_name: str
    year: int
    budget_msek: float | None
    amendment_budgets_msek: float | None
    outcome_msek: float | None
    opening_balance_msek: float | None
    closing_balance_msek: float | None


@dataclass
class IncomeRow:
    income_type: str
    income_type_name: str
    income_main_group: str
    income_main_group_name: str
    income_title: str
    income_title_name: str
    year: int
    budget_msek: float | None
    outcome_msek: float | None


# A 2024 comparison found a 12.370 BSEK gap between balance_msek
# and the official ESV budget balance (ClickUp task 869ey30z3).
# The gap is net lending + a cash adjustment, both published only in
# ESV's PDF reports (no structured/API source exists) or behind a
# scrape-only export on Riksgalden's site — neither is integrated,
# so we do not expose placeholder fields we cannot fill.
BALANCE_MSEK_NOTE = (
    "balance_msek is total income minus the sum of the 27 "
    "expenditure areas' outturn. It is NOT the official "
    "central-government budget balance (budgetsaldo), which also "
    "includes net lending and a cash adjustment from the Swedish "
    "National Debt Office (Riksgalden). This server does not "
    "source that data, so no official balance figure is provided."
)


@dataclass
class BudgetOverview:
    year: int
    total_expenditure_msek: float
    total_income_msek: float
    balance_msek: float
    areas: list[AreaSummary] = field(default_factory=list)


@dataclass
class AreaSummary:
    area_id: str
    area_name: str
    budget_msek: float
    outcome_msek: float
    delta_msek: float


PUBLICATION_SCHEDULE = {
    "expenditure_definitive": {
        "description": (
            "Definitiva utgifter f\u00f6r"
            " f\u00f6reg\u00e5ende \u00e5r"
        ),
        "typical_month": 3,
        "cadence": "Annually in March",
    },
    "income_preliminary_1": {
        "description": (
            "Prelimin\u00e4r 1: ESV:s ber\u00e4kning"
        ),
        "typical_month": 3,
        "cadence": "Annually in March",
    },
    "income_preliminary_2": {
        "description": (
            "Prelimin\u00e4r 2: Regeringens ber\u00e4kning"
        ),
        "typical_month": 6,
        "cadence": "Annually in June",
    },
    "income_preliminary_3": {
        "description": (
            "Prelimin\u00e4r 3: ESV:s uppdaterade"
            " ber\u00e4kning"
        ),
        "typical_month": 3,
        "cadence": "Annually in March (year + 1)",
    },
    "income_definitive": {
        "description": "Definitiva inkomster",
        "typical_month": 6,
        "cadence": "Annually in June (year + 2)",
    },
}


def _parse_swedish_decimal(value: str) -> float | None:
    value = value.strip()
    if not value or value in ("..", ".", "-"):
        return None
    cleaned = (
        value.replace("\xa0", "")
        .replace(" ", "")
        .replace(",", ".")
    )
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_int_safe(value: str) -> int:
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return 0


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# next_expected_update is a calendar heuristic (typical publication
# months per PUBLICATION_SCHEDULE), not a date confirmed by
# Statskontoret. The day-of-month is an arbitrary placeholder.
NEXT_UPDATE_NOTE = (
    "next_expected_update is a heuristic estimate based on "
    "Statskontoret's typical publication cadence (expenditure in "
    "March, income revisions in March and June). It is not a "
    "confirmed date from Statskontoret. See get_publication_schedule "
    "for the underlying cadence assumptions."
)


def _next_expected_update() -> str:
    now = datetime.now(UTC)
    year = now.year
    if now.month < 3:
        return f"{year}-03-15"
    elif now.month < 6:
        return f"{year}-06-15"
    else:
        return f"{year + 1}-03-15"


def _classify_link(href: str, anchor_text: str) -> str:
    parsed = urlparse(href)
    params = parse_qs(parsed.query)
    doc_type = (
        params.get("documentType")
        or params.get("DocumentType")
        or [""]
    )[0].lower()
    if "utgift" in doc_type:
        return "expenditure"
    if "inkomst" in doc_type:
        return "income"
    text = anchor_text.lower()
    if "utgift" in text or "expenditure" in text:
        return "expenditure"
    if "inkomst" in text or "income" in text:
        return "income"
    return "unknown"


def _classify_format(href: str, anchor_text: str) -> str:
    parsed = urlparse(href)
    params = parse_qs(parsed.query)
    ft = (
        params.get("fileType")
        or params.get("FileType")
        or [""]
    )[0].lower()
    if ft == "zip" or "csv" in anchor_text.lower():
        return "zip"
    if ft == "excel" or "excel" in anchor_text.lower():
        return "xlsx"
    if href.lower().endswith(".zip"):
        return "zip"
    if href.lower().endswith(".xlsx"):
        return "xlsx"
    return "unknown"


def _classify_revision(
    href: str, anchor_text: str,
) -> tuple[str, int]:
    """Classify income data revision from URL params and text.

    Checks multiple sources in priority order:
    1. `status` query param (live Statskontoret uses this)
    2. `fileName` query param (fallback)
    3. `documentType` query param
    4. anchor text

    Returns (label, priority). Higher priority = more
    authoritative: definitiv (4) > preliminar_3 (3)
    > preliminar_2 (2) > preliminar_1 (1) > unknown (0).
    """
    parsed = urlparse(href)
    params = parse_qs(parsed.query)

    # Gather text from all available sources
    status_val = (
        params.get("status")
        or params.get("Status")
        or [""]
    )[0]
    filename_val = (
        params.get("fileName")
        or params.get("FileName")
        or params.get("filename")
        or [""]
    )[0]
    doc_type = (
        params.get("documentType")
        or params.get("DocumentType")
        or [""]
    )[0]

    # Unquote and lowercase all sources into one string
    combined = " ".join([
        unquote(status_val).lower(),
        unquote(filename_val).lower(),
        unquote(doc_type).lower(),
        anchor_text.lower(),
    ])

    if "definitiv" in combined:
        return "definitiv", 4
    for n in (3, 2, 1):
        markers = (
            f"prelimin\u00e4r {n}",
            f"preliminar{n}",
            f"preliminar {n}",
            f"prelimin\u00e4r{n}",
            f"prelimin\u00e4r+{n}",
        )
        if any(m in combined for m in markers):
            return f"preliminar_{n}", n
    if "prelimin" in combined:
        return "preliminar_1", 1
    return "unknown", 0


def _is_allowed_host(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.hostname in ALLOWED_HOSTS
    except (ValueError, AttributeError):
        return False


def _check_required_headers(
    col_map: dict[str, str],
    fieldnames: list[str],
    required_keys: set[str],
) -> list[str]:
    """Check that required semantic columns exist in CSV headers."""
    missing = []
    fieldname_set = set(fieldnames)
    for key in sorted(required_keys):
        mapped_col = col_map.get(key)
        if (
            mapped_col is None
            or mapped_col not in fieldname_set
        ):
            missing.append(key)
    return missing


def _validate_expenditure_rows(
    rows: list[ExpenditureRow],
) -> list[str]:
    """Validate parsed expenditure data invariants."""
    if not rows:
        return ["no rows parsed"]
    problems: list[str] = []
    bad_years = [
        r for r in rows
        if r.year < _MIN_YEAR or r.year > _MAX_YEAR
    ]
    if bad_years:
        problems.append(
            f"{len(bad_years)}/{len(rows)} rows with "
            f"invalid year (e.g. {bad_years[0].year})"
        )
    with_outcome = sum(
        1 for r in rows if r.outcome_msek is not None
    )
    ratio = with_outcome / len(rows)
    if ratio < _MIN_OUTCOME_RATIO:
        problems.append(
            f"only {with_outcome}/{len(rows)} rows have "
            f"outcome_msek ({ratio:.0%}, "
            f"need {_MIN_OUTCOME_RATIO:.0%})"
        )
    area_ids = {r.expenditure_area_id for r in rows}
    if len(area_ids) < _MIN_DISTINCT_IDS:
        problems.append(
            f"only {len(area_ids)} distinct area ID(s), "
            f"need >= {_MIN_DISTINCT_IDS}"
        )
    return problems


def _validate_income_rows(
    rows: list[IncomeRow],
) -> list[str]:
    """Validate parsed income data invariants."""
    if not rows:
        return ["no rows parsed"]
    problems: list[str] = []
    bad_years = [
        r for r in rows
        if r.year < _MIN_YEAR or r.year > _MAX_YEAR
    ]
    if bad_years:
        problems.append(
            f"{len(bad_years)}/{len(rows)} rows with "
            f"invalid year (e.g. {bad_years[0].year})"
        )
    with_outcome = sum(
        1 for r in rows if r.outcome_msek is not None
    )
    ratio = with_outcome / len(rows)
    if ratio < _MIN_OUTCOME_RATIO:
        problems.append(
            f"only {with_outcome}/{len(rows)} rows have "
            f"outcome_msek ({ratio:.0%}, "
            f"need {_MIN_OUTCOME_RATIO:.0%})"
        )
    income_types = {r.income_type for r in rows}
    if len(income_types) < _MIN_DISTINCT_IDS:
        problems.append(
            f"only {len(income_types)} distinct "
            f"income_type(s), "
            f"need >= {_MIN_DISTINCT_IDS}"
        )
    return problems


class StatskontoretClient:
    """Async client for Statskontoret budget outcome data."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            timeout=timeout, follow_redirects=True,
        )
        if data_dir is not None:
            self._data_dir = Path(data_dir)
        else:
            self._data_dir = (
                Path.home() / ".statsbudget-cache"
            )
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._expenditure_data: list[ExpenditureRow] = []
        self._income_data: list[IncomeRow] = []
        self._sync_meta: SyncStatus = SyncStatus()
        self._meta_path = (
            self._data_dir / "sync_meta.json"
        )
        self._load_meta()

    def _load_meta(self) -> None:
        if self._meta_path.exists():
            try:
                raw = json.loads(
                    self._meta_path.read_text(
                        encoding="utf-8",
                    ),
                )
                self._sync_meta = SyncStatus(
                    last_sync=raw.get("last_sync"),
                    next_expected_update=raw.get(
                        "next_expected_update",
                    ),
                    sources=[
                        DataSourceMeta(**s)
                        for s in raw.get("sources", [])
                    ],
                )
            except (json.JSONDecodeError, TypeError):
                pass

    def _save_meta(self) -> None:
        data = {
            "last_sync": self._sync_meta.last_sync,
            "next_expected_update": (
                self._sync_meta.next_expected_update
            ),
            "sources": [
                {
                    "source": s.source,
                    "description": s.description,
                    "publication_cadence": (
                        s.publication_cadence
                    ),
                    "last_synced_at": s.last_synced_at,
                    "source_last_updated": (
                        s.source_last_updated
                    ),
                    "files_downloaded": (
                        s.files_downloaded
                    ),
                    "years_covered": s.years_covered,
                    "income_revision": (
                        s.income_revision
                    ),
                }
                for s in self._sync_meta.sources
            ],
        }
        self._meta_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> StatskontoretClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def discover_download_links(
        self, year: int | None = None,
    ) -> tuple[list[dict[str, str]], list[str]]:
        """Scrape the arsutfall page for download links."""
        url = f"{BASE_URL}{ARSUTFALL_PAGE}"
        if year:
            url += f"?year={year}"
        resp = await self._client.get(url)
        resp.raise_for_status()
        raw_html = resp.text
        source_dates = UPDATED_RE.findall(raw_html)
        links: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        for match in HREF_RE.finditer(raw_html):
            raw_href = html_mod.unescape(
                match.group(1).strip(),
            )
            anchor_text = TAG_RE.sub(
                "", match.group(2),
            ).strip()
            is_data_file = (
                "getfile" in raw_href.lower()
                or raw_href.lower().endswith(".zip")
                or raw_href.lower().endswith(".xlsx")
                or "filetype=" in raw_href.lower()
            )
            if not is_data_file:
                continue
            resolved = (
                raw_href
                if raw_href.startswith("http")
                else f"{BASE_URL}{raw_href}"
            )
            if not _is_allowed_host(resolved):
                print(
                    f"Skipping disallowed host: "
                    f"{resolved}",
                    file=sys.stderr,
                )
                continue
            if resolved in seen_urls:
                continue
            seen_urls.add(resolved)
            file_type = _classify_link(
                resolved, anchor_text,
            )
            file_format = _classify_format(
                resolved, anchor_text,
            )
            if (
                file_type == "unknown"
                or file_format == "unknown"
                or file_format == "xlsx"
            ):
                continue
            link_info: dict[str, Any] = {
                "url": resolved,
                "text": anchor_text,
                "type": file_type,
                "format": file_format,
            }
            if file_type == "income":
                rev_label, rev_prio = (
                    _classify_revision(
                        resolved, anchor_text,
                    )
                )
                link_info["revision"] = rev_label
                link_info["revision_priority"] = (
                    rev_prio
                )
            links.append(link_info)
        return links, source_dates

    async def download_file(
        self, url: str, filename: str,
    ) -> Path:
        """Download a file with streaming size enforcement."""
        async with self._client.stream(
            "GET", url,
        ) as resp:
            resp.raise_for_status()
            cl = resp.headers.get("content-length")
            if cl and int(cl) > MAX_DOWNLOAD_BYTES:
                raise ValueError(
                    f"Content-Length {cl} exceeds "
                    f"limit ({MAX_DOWNLOAD_BYTES}). "
                    f"URL: {url}"
                )
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ValueError(
                        f"Download exceeds limit at "
                        f"{total} bytes "
                        f"(max {MAX_DOWNLOAD_BYTES}). "
                        f"URL: {url}"
                    )
                chunks.append(chunk)
        content = b"".join(chunks)
        path = self._data_dir / filename
        path.write_bytes(content)
        return path

    async def sync(
        self, year: int | None = None,
    ) -> SyncStatus:
        """Download and parse latest data from Statskontoret.

        Three-stage process:
        1. Download and parse into local variables
        2. Validate: non-empty, required headers found, data
           invariants (valid years, outcome coverage, ID
           diversity)
        3. Commit atomically (both or neither)

        Raises SyncError on any validation failure.
        """
        links, source_dates = (
            await self.discover_download_links(year=year)
        )
        sync_time = _now_iso()
        latest_source_date = (
            max(source_dates) if source_dates else None
        )

        exp_links = [
            lnk for lnk in links
            if lnk["type"] == "expenditure"
        ]
        inc_links = sorted(
            [
                lnk for lnk in links
                if lnk["type"] == "income"
            ],
            key=lambda lnk: lnk.get(
                "revision_priority", 0,
            ),
            reverse=True,
        )

        # Stage 1: download and parse
        files_downloaded: list[str] = []
        staged_exp: list[ExpenditureRow] = []
        staged_inc: list[IncomeRow] = []
        selected_rev: str | None = None

        if exp_links:
            link = exp_links[0]
            fname = (
                f"expenditure_{year or 'latest'}.zip"
            )
            path = await self.download_file(
                link["url"], fname,
            )
            files_downloaded.append(fname)
            csv_content = self._extract_csv_from_zip(
                path,
            )
            if csv_content:
                staged_exp = (
                    self._parse_expenditure_csv(
                        csv_content,
                    )
                )

        if inc_links:
            link = inc_links[0]
            selected_rev = link.get("revision")
            fname = f"income_{year or 'latest'}.zip"
            path = await self.download_file(
                link["url"], fname,
            )
            files_downloaded.append(fname)
            csv_content = self._extract_csv_from_zip(
                path,
            )
            if csv_content:
                staged_inc = self._parse_income_csv(
                    csv_content,
                )
            print(
                f"Selected income revision: "
                f"{selected_rev} "
                f"(from {len(inc_links)} available)",
                file=sys.stderr,
            )

        # Stage 2: validate
        missing: list[str] = []
        if not staged_exp:
            missing.append("expenditure")
        if not staged_inc:
            missing.append("income")
        if missing:
            raise SyncError(
                f"Incomplete sync: missing "
                f"{', '.join(missing)}. "
                f"Downloaded {len(files_downloaded)} "
                f"file(s): {files_downloaded}. "
                f"In-memory data NOT updated.",
                has_expenditure=bool(staged_exp),
                has_income=bool(staged_inc),
            )

        exp_problems = _validate_expenditure_rows(
            staged_exp,
        )
        inc_problems = _validate_income_rows(
            staged_inc,
        )
        if exp_problems or inc_problems:
            details = []
            if exp_problems:
                details.append(
                    "expenditure: "
                    + "; ".join(exp_problems)
                )
            if inc_problems:
                details.append(
                    "income: "
                    + "; ".join(inc_problems)
                )
            raise SyncError(
                f"Data validation failed: "
                f"{'. '.join(details)}. "
                f"In-memory data NOT updated.",
                has_expenditure=not bool(exp_problems),
                has_income=not bool(inc_problems),
            )

        # Stage 3: commit atomically
        self._expenditure_data = staged_exp
        self._income_data = staged_inc

        source_meta = DataSourceMeta(
            source="Statskontoret \u00d6ppna Data",
            description=(
                "Annual budget outturn for central "
                "government"
            ),
            publication_cadence=(
                "Expenditure: March. "
                "Income: March + June."
            ),
            last_synced_at=sync_time,
            source_last_updated=latest_source_date,
            files_downloaded=files_downloaded,
            years_covered=self.get_available_years(),
            income_revision=selected_rev,
        )
        self._sync_meta = SyncStatus(
            last_sync=sync_time,
            next_expected_update=(
                _next_expected_update()
            ),
            sources=[source_meta],
        )
        self._save_meta()
        return self._sync_meta

    def get_sync_status(self) -> SyncStatus:
        return self._sync_meta

    def get_publication_schedule(
        self,
    ) -> dict[str, Any]:
        return {
            "schedule": PUBLICATION_SCHEDULE,
            "summary": (
                "Statskontoret publicerar ny budgetdata"
                " i mars och juni."
            ),
            "next_expected_update": (
                _next_expected_update()
            ),
            "next_expected_update_note": NEXT_UPDATE_NOTE,
            "sync_recommendation": (
                "Sync in March and June each year."
            ),
        }

    def load_from_csv(
        self,
        expenditure_path=None,
        income_path=None,
    ) -> None:
        if expenditure_path:
            content = Path(
                expenditure_path,
            ).read_text(encoding="utf-8")
            self._expenditure_data = (
                self._parse_expenditure_csv(content)
            )
        if income_path:
            content = Path(
                income_path,
            ).read_text(encoding="utf-8")
            self._income_data = (
                self._parse_income_csv(content)
            )

    def _extract_csv_from_zip(
        self, zip_path: Path,
    ) -> str | None:
        """Extract first CSV from ZIP with size guard."""
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                csv_files = [
                    n
                    for n in zf.namelist()
                    if n.lower().endswith(".csv")
                ]
                if not csv_files:
                    return None
                info = zf.getinfo(csv_files[0])
                if info.file_size > MAX_CSV_BYTES:
                    print(
                        f"CSV too large: "
                        f"{info.file_size} bytes "
                        f"(max {MAX_CSV_BYTES}). "
                        f"Skipping {csv_files[0]}",
                        file=sys.stderr,
                    )
                    return None
                with zf.open(csv_files[0]) as f:
                    return f.read().decode("utf-8")
        except (
            zipfile.BadZipFile,
            KeyError,
            UnicodeDecodeError,
        ):
            return None

    def _parse_expenditure_csv(
        self, content: str,
    ) -> list[ExpenditureRow]:
        """Parse expenditure CSV with header validation."""
        reader = csv.DictReader(
            io.StringIO(content), delimiter=";",
        )
        if not reader.fieldnames:
            return []
        col_map = self._map_expenditure_columns(
            reader.fieldnames,
        )
        missing = _check_required_headers(
            col_map,
            list(reader.fieldnames),
            _EXP_REQUIRED_KEYS,
        )
        if missing:
            print(
                f"Expenditure CSV missing required "
                f"columns: {missing}",
                file=sys.stderr,
            )
            return []
        rows: list[ExpenditureRow] = []
        for record in reader:
            area_id = record.get(
                col_map["area_id"], "",
            ).strip()
            if (
                not area_id
                or not area_id[0].isdigit()
            ):
                continue
            rows.append(ExpenditureRow(
                expenditure_area_id=area_id,
                expenditure_area_name=record.get(
                    col_map["area_name"], "",
                ).strip(),
                appropriation_id=record.get(
                    col_map["approp_id"], "",
                ).strip(),
                appropriation_name=record.get(
                    col_map["approp_name"], "",
                ).strip(),
                year=_parse_int_safe(
                    record.get(col_map["year"], "0"),
                ),
                budget_msek=_parse_swedish_decimal(
                    record.get(
                        col_map["budget"], "",
                    ),
                ),
                amendment_budgets_msek=(
                    _parse_swedish_decimal(
                        record.get(
                            col_map["amendments"],
                            "",
                        ),
                    )
                ),
                outcome_msek=_parse_swedish_decimal(
                    record.get(
                        col_map["outcome"], "",
                    ),
                ),
                opening_balance_msek=(
                    _parse_swedish_decimal(
                        record.get(
                            col_map["opening"], "",
                        ),
                    )
                ),
                closing_balance_msek=(
                    _parse_swedish_decimal(
                        record.get(
                            col_map["closing"], "",
                        ),
                    )
                ),
            ))
        return rows

    def _parse_income_csv(
        self, content: str,
    ) -> list[IncomeRow]:
        """Parse income CSV with header validation."""
        reader = csv.DictReader(
            io.StringIO(content), delimiter=";",
        )
        if not reader.fieldnames:
            return []
        col_map = self._map_income_columns(
            reader.fieldnames,
        )
        missing = _check_required_headers(
            col_map,
            list(reader.fieldnames),
            _INC_REQUIRED_KEYS,
        )
        if missing:
            print(
                f"Income CSV missing required "
                f"columns: {missing}",
                file=sys.stderr,
            )
            return []
        rows: list[IncomeRow] = []
        for record in reader:
            income_type = record.get(
                col_map["income_type"], "",
            ).strip()
            if (
                not income_type
                or not income_type[0].isdigit()
            ):
                continue
            rows.append(IncomeRow(
                income_type=income_type,
                income_type_name=record.get(
                    col_map["income_type_name"], "",
                ).strip(),
                income_main_group=record.get(
                    col_map["main_group"], "",
                ).strip(),
                income_main_group_name=record.get(
                    col_map["main_group_name"], "",
                ).strip(),
                income_title=record.get(
                    col_map["title"], "",
                ).strip(),
                income_title_name=record.get(
                    col_map["title_name"], "",
                ).strip(),
                year=_parse_int_safe(
                    record.get(col_map["year"], "0"),
                ),
                budget_msek=_parse_swedish_decimal(
                    record.get(
                        col_map["budget"], "",
                    ),
                ),
                outcome_msek=_parse_swedish_decimal(
                    record.get(
                        col_map["outcome"], "",
                    ),
                ),
            ))
        return rows

    @staticmethod
    def _map_expenditure_columns(fieldnames):
        mapping = {}
        for name in fieldnames:
            lower = name.lower().strip()
            if (
                lower.startswith("utgiftsomr\u00e5de")
                and "namn" not in lower
                and "utfalls" not in lower
            ):
                mapping.setdefault("area_id", name)
            elif (
                "utgiftsomr\u00e5desnamn" in lower
                and "utfalls" not in lower
            ):
                mapping.setdefault("area_name", name)
            elif lower == "anslag":
                mapping.setdefault("approp_id", name)
            elif (
                "anslagsnamn" in lower
                and "utfalls" not in lower
            ):
                mapping.setdefault(
                    "approp_name", name,
                )
            elif lower in ("\u00e5r", "ar", "year"):
                mapping.setdefault("year", name)
            elif "statens budget" in lower:
                mapping.setdefault("budget", name)
            elif (
                "\u00e4ndringsbudget" in lower
                or "andringsbudget" in lower
            ):
                mapping.setdefault(
                    "amendments", name,
                )
            elif lower == "utfall":
                mapping.setdefault("outcome", name)
            elif (
                "ing\u00e5ende" in lower
                or "ingaende" in lower
            ):
                mapping.setdefault("opening", name)
            elif (
                "utg\u00e5ende" in lower
                or "utgaende" in lower
            ):
                mapping.setdefault("closing", name)

        defaults = {
            "area_id": "Utgiftsomr\u00e5de",
            "area_name": "Utgiftsomr\u00e5desnamn",
            "approp_id": "Anslag",
            "approp_name": "Anslagsnamn",
            "year": "\u00c5r",
            "budget": "Statens budget",
            "amendments": "\u00c4ndringsbudgetar",
            "outcome": "Utfall",
            "opening": (
                "Ing\u00e5ende"
                " \u00f6verf\u00f6ringsbelopp"
            ),
            "closing": (
                "Utg\u00e5ende"
                " \u00f6verf\u00f6ringsbelopp"
            ),
        }
        for k, v in defaults.items():
            mapping.setdefault(k, v)
        return mapping

    @staticmethod
    def _map_income_columns(fieldnames):
        mapping = {}
        for name in fieldnames:
            lower = name.lower().strip()
            if lower == "inkomsttyp" or (
                lower.startswith("inkomsttyp")
                and "namn" not in lower
                and "utfalls" not in lower
            ):
                mapping.setdefault(
                    "income_type", name,
                )
            elif (
                "inkomsttypsnamn" in lower
                and "utfalls" not in lower
            ):
                mapping.setdefault(
                    "income_type_name", name,
                )
            elif (
                "inkomsthuvudgrupp" in lower
                and "namn" not in lower
                and "utfalls" not in lower
            ):
                mapping.setdefault(
                    "main_group", name,
                )
            elif (
                "inkomsthuvudgruppsnamn" in lower
                and "utfalls" not in lower
            ):
                mapping.setdefault(
                    "main_group_name", name,
                )
            elif lower == "inkomsttitel" or (
                lower.startswith("inkomsttitel")
                and "namn" not in lower
                and "grupp" not in lower
                and "utfalls" not in lower
            ):
                mapping.setdefault("title", name)
            elif (
                "inkomsttitelsnamn" in lower
                and "utfalls" not in lower
            ):
                mapping.setdefault(
                    "title_name", name,
                )
            elif lower in ("\u00e5r", "ar", "year"):
                mapping.setdefault("year", name)
            elif "statens budget" in lower:
                mapping.setdefault("budget", name)
            elif lower == "utfall":
                mapping.setdefault("outcome", name)

        defaults = {
            "income_type": "Inkomsttyp",
            "income_type_name": "Inkomsttypsnamn",
            "main_group": "Inkomsthuvudgrupp",
            "main_group_name": (
                "Inkomsthuvudgruppsnamn"
            ),
            "title": "Inkomsttitel",
            "title_name": "Inkomsttitelsnamn",
            "year": "\u00c5r",
            "budget": "Statens budget",
            "outcome": "Utfall",
        }
        for k, v in defaults.items():
            mapping.setdefault(k, v)
        return mapping

    def get_budget_overview(self, year):
        year_exp = [
            r
            for r in self._expenditure_data
            if r.year == year
        ]
        year_inc = [
            r
            for r in self._income_data
            if r.year == year
        ]
        area_map: dict[str, AreaSummary] = {}
        for row in year_exp:
            aid = row.expenditure_area_id
            if aid not in area_map:
                area_map[aid] = AreaSummary(
                    area_id=aid,
                    area_name=(
                        row.expenditure_area_name
                    ),
                    budget_msek=0.0,
                    outcome_msek=0.0,
                    delta_msek=0.0,
                )
            if row.budget_msek is not None:
                area_map[aid].budget_msek += (
                    row.budget_msek
                )
            if row.outcome_msek is not None:
                area_map[aid].outcome_msek += (
                    row.outcome_msek
                )
        for area in area_map.values():
            area.delta_msek = (
                area.outcome_msek - area.budget_msek
            )
        total_exp = sum(
            a.outcome_msek for a in area_map.values()
        )
        total_inc = sum(
            r.outcome_msek or 0.0 for r in year_inc
        )
        return BudgetOverview(
            year=year,
            total_expenditure_msek=total_exp,
            total_income_msek=total_inc,
            balance_msek=total_inc - total_exp,
            areas=sorted(
                area_map.values(),
                key=lambda a: a.area_id,
            ),
        )

    def get_expenditure_area(self, area_id, year):
        return [
            r
            for r in self._expenditure_data
            if r.expenditure_area_id == area_id
            and r.year == year
        ]

    def compare_budgets(self, year_a, year_b):
        oa = self.get_budget_overview(year_a)
        ob = self.get_budget_overview(year_b)
        aa = {a.area_id: a for a in oa.areas}
        ab = {a.area_id: a for a in ob.areas}
        result = []
        for aid in sorted(set(aa) | set(ab)):
            a, b = aa.get(aid), ab.get(aid)
            va = a.outcome_msek if a else 0.0
            vb = b.outcome_msek if b else 0.0
            d = vb - va
            p = (
                round(d / va * 100, 2)
                if va != 0
                else None
            )
            area_name = (
                (b or a).area_name
                if (b or a)
                else aid
            )
            result.append({
                "area_id": aid,
                "area_name": area_name,
                f"outcome_{year_a}_msek": va,
                f"outcome_{year_b}_msek": vb,
                "delta_msek": d,
                "delta_pct": p,
            })
        return result

    def get_available_years(self):
        years: set[int] = set()
        for r in self._expenditure_data:
            years.add(r.year)
        for r in self._income_data:
            years.add(r.year)
        return sorted(years)

    @property
    def expenditure_data(self):
        return self._expenditure_data

    @property
    def income_data(self):
        return self._income_data
