# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

MCP (Model Context Protocol) server exposing Swedish national budget data (statsbudgeten) — expenditure outturn, tax revenue, and tax quota/Laffer analysis — as tools for Claude Desktop and other MCP clients. Built on FastMCP. Python 3.12+.

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run the server
swedish-budget-mcp                      # or: python -m swedish_budget_mcp.server

# Tests (unit only — no network calls)
pytest tests/ -m "not integration"

# Single test
pytest tests/test_statskontoret.py::test_name -v

# All tests, including live SCB/Statskontoret API calls
pytest tests/

# Lint
ruff check src/ tests/
```

Tests marked `@pytest.mark.integration` (in `test_scb_client.py`, `test_laffer.py`) hit live external APIs — excluded by default via `-m "not integration"`. `asyncio_mode = "auto"` is set in `pyproject.toml`, so async test functions don't need `@pytest.mark.asyncio`.

## Architecture

Three independent data sources feed one FastMCP server (`server.py`), which holds module-level singleton clients (`_scb`, `_sk`, `_cache`) initialized in the `lifespan` context manager and accessed via `_require_*()` guards from each tool.

- **`statskontoret.py`** — `StatskontoretClient` scrapes the Statskontoret open-data page for download links, classifies them (expenditure vs. income, ZIP vs. Excel), downloads ZIPs, and parses semicolon-delimited Swedish-locale CSVs (comma decimals) into `ExpenditureRow`/`IncomeRow` dataclasses. Column mapping is header-name-based (Swedish column names, e.g. `Utgiftsområde`, `Utfall`) with a fallback default, since Statskontoret's schema isn't guaranteed stable.
- **`scb_client.py`** — `SCBClient` queries SCB's PxWeb v1 REST API (JSON POST) for tax revenue (`SkatteIntakt`) and tax quota (`SkattekvotBNP`) tables, with retry/backoff on transient errors (`_RETRYABLE_EXCEPTIONS`).
- **`laffer.py`** — builds tax-quota-vs-GDP timeseries from `SCBClient` data, annotated with a static list of Swedish tax reforms (`TAX_REFORMS`, 1971–2020). Explicitly descriptive, not a causal Laffer curve (see module docstring).
- **`cache.py`** — `BudgetCache` is a schema-versioned SQLite cache (`~/.swedish-budget-cache/swedish_budget.db`). A cache is only considered "populated" when both expenditure and income tables have rows AND `snapshot_complete` is true — partial snapshots are never trusted.
- **`formatters.py`** — ASCII visualizations (bars, flow diagrams, decision chains, Laffer timeline) for terminal/text MCP clients.
- **`server.py`** — defines all MCP tools/resources and owns startup semantics (see below).

### Sync and startup semantics (must-preserve invariants)

These are deliberate design decisions documented in the README — don't casually "simplify" them away:

- **Atomic snapshots**: `StatskontoretClient.sync()` downloads and parses expenditure + income into local staging variables first; only after both pass validation (`_validate_expenditure_rows`/`_validate_income_rows` — checks year ranges, outcome-field coverage ratio, ID diversity) does it commit to `self._expenditure_data`/`self._income_data`. Any failure raises `SyncError` and leaves in-memory data untouched.
- **Income revision selection**: multiple income revisions may be published (Preliminar 1/2/3, Definitiv). `_classify_revision()` reads `status`/`fileName`/`documentType` URL params plus anchor text to rank revisions by priority (definitiv=4 > preliminar_3=3 > ... > unknown=0); `sync()` picks the highest-priority one, not the first found. The selected revision is surfaced via `sync_budget_data`/`get_sync_status`.
- **Streaming downloads with size limits**: `download_file()` checks `Content-Length` early and enforces `MAX_DOWNLOAD_BYTES` (50 MB) during streaming — never buffers an oversized response. `MAX_CSV_BYTES` (100 MB) guards the decompressed ZIP contents. `ALLOWED_HOSTS` restricts downloads to `statskontoret.se` domains.
- **Startup fallback chain** (in `server.py`'s `lifespan`): fresh cache (<1 week old) → load from cache; stale/empty cache → sync from network; sync failure → fall back to stale cache if one exists; no cache at all → start empty and log. Cache load failures also trigger `cache.invalidate()` + a fresh sync attempt.
- **Cache schema versioning**: `BudgetCache` checks `SCHEMA_VERSION` on load; a mismatch is treated as a full cache miss (not migrated).

When touching sync, cache, or revision-selection logic, check `test_sync_e2e.py` (mocked sync-level e2e) and `test_statskontoret.py` (revision classification with real Statskontoret URL patterns) — these encode the exact failure/fallback behaviors above.

### Data semantics

All budget tools return **actual outturn** (utfall), not the originally proposed budget — every response includes `data_type` and `source` fields for this reason. Don't blur that distinction when adding new tools or fields.
