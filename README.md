# swedish-budget-mcp

> **Status: Alpha (v0.1.0)**
> This project is under active development. APIs, schemas, and output formats may change without notice. Not recommended for production use yet. Contributions and feedback welcome.

MCP server for the Swedish national budget (statsbudgeten). Provides structured, queryable access to budget outturn (utfall), tax revenue, tax quota analysis, and fiscal data.

Built for Claude Desktop, Glama, and any MCP-compatible client.

## Important: Data Semantics

Budget tools return **actual outturn** (utfall) from Statskontoret, not the originally proposed budget. This means the numbers show what was actually spent and collected, not what was planned. Every response includes `data_type` and `source` fields so consuming applications can communicate this clearly.

## Budget Balance Semantics

`get_budget_overview` returns `balance_msek`, which is **total income minus the sum of the 27 expenditure areas' outturn**. This is *not* the official central-government budget balance (budgetsaldo) published by ESV/Riksgalden, which also includes net lending and a cash adjustment. A 2024 comparison found a 12.370 BSEK gap between the two (-91.902 BSEK vs. the official -104.272 BSEK).

To avoid silently misleading consumers, the response also includes `balance_note`, which explains the above in-band. No `official_balance_msek` field is exposed: net lending and cash adjustment data exist only in ESV's PDF reports or behind a scrape-only export on Riksgalden's site, not as a structured/API source, so we don't promise a field we have no reliable way to fill.

## Total Expenditure Semantics

`get_budget_overview` returns `total_expenditure_msek`, which is **the sum of the 27 expenditure areas' outturn only**. This is *not* "takbegransade utgifter" (the expenditure-ceiling figure most often cited in Swedish budget coverage), which excludes area 26 (state debt interest) but adds the old-age pension system (alderspensionssystemet), which sits outside statens budget entirely. A 2024 check found our sum was 1,364,651 MSEK against a commonly-reported ceiling figure of 1,686,000 MSEK - the gap reconciles almost exactly to area 26 plus the pension system.

The response includes `total_expenditure_note` explaining this in-band, the same pattern as `balance_note`. Individual area totals can also be broader than a narrower media-defined category with a similar name - e.g. area 06 "Forsvar och samhallets krisberedskap" includes civil crisis preparedness alongside military defense, so it will not match a headline "forsvarsbudget" figure covering military defense only.

## Sync Guarantees

- **Atomic snapshots**: both expenditure and income must parse successfully before any in-memory data or cache is updated. If either dataset fails, the server raises `SyncError` and falls back to the previous valid cache.
- **Income revision selection**: when multiple income revisions are available (Preliminar 1, 2, 3, Definitiv), the sync picks the highest-priority revision (definitiv > preliminar 3 > 2 > 1), not the first one in DOM order. The selected revision is exposed in sync metadata.
- **Streaming downloads**: files are downloaded with a streaming byte counter and `Content-Length` early check. The full response is never buffered before enforcing the 50 MB limit.
- **Startup fallback**: if sync fails at startup, the server falls back to a stale but complete cache. If no cache exists, it starts empty and logs the error.

## Data Sources

| Source | What | Format | Coverage |
|--------|------|--------|----------|
| SCB PxWeb API | Tax revenue by type, tax quota/GDP | JSON (POST) | 1950-2025 |
| Statskontoret Oppna Data | Budget outturn: expenditure | CSV in ZIP | 1997-2025 |
| Statskontoret Oppna Data | Budget outturn: income | CSV in ZIP | 2006-2025 |

## MCP Tools (15)

**Budget Outturn (5)**
- `get_budget_overview(year)` : actual expenditure outturn, total income, balance, all 27 areas (MSEK). Expenditure available from 1997; total_income_msek/balance_msek only meaningful from 2006 (income coverage starts then)
- `get_expenditure_area(area_id, year)` : drill-down into appropriations with budget vs outturn (1997-2025)
- `compare_budgets(year_a, year_b)` : year-over-year outturn delta per area
- `get_biggest_changes(year_a, year_b, area_id?, top_n?)` : top increases/decreases between two years, area-level or (with area_id) appropriation-level within one area
- `sync_budget_data(year?)` : download and cache latest outturn from Statskontoret

**Tax Revenue (3)**
- `get_revenue(year)` : tax revenue by category (labour, capital, consumption) in MSEK
- `get_revenue_timeseries(from_year, to_year)` : revenue over time by category
- `get_revenue_detail(year, tax_types?)` : full 40-category breakdown

**Tax Quota Analysis (3)**
- `get_laffer_data(from_year?, to_year?)` : tax quota (% of GDP) as timeseries with decade tags and reform annotations
- `get_laffer_timeseries(from_year?, to_year?)` : flat timeseries with nominal GDP growth and reform markers
- `get_tax_reforms()` : annotated Swedish tax reforms (1971-2020)

**Meta (4)**
- `get_sync_status()` : data freshness, selected income revision, and cache diagnostics
- `get_publication_schedule()` : when Statskontoret publishes new data
- `get_available_years()` : years with loaded outturn data
- `get_cache_stats()` : SQLite cache diagnostics

**Resources**
- `budget://areas` : all 27 expenditure areas (id, name)

## Installation

```bash
# From PyPI
pip install swedish-budget-mcp
swedish-budget-mcp

# Or, without installing:
uvx swedish-budget-mcp
```

```bash
# Development
git clone https://github.com/bjornwalther/swedish-budget-mcp.git
cd swedish-budget-mcp
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run the server
swedish-budget-mcp
```

### Claude Desktop

Add to `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "swedish-budget-mcp": {
      "command": "uvx",
      "args": ["swedish-budget-mcp"]
    }
  }
}
```

## Development

```bash
# Run tests (unit only)
pytest tests/ -m "not integration"

# Run all tests including SCB API integration
pytest tests/

# Lint
ruff check src/ tests/
```

## Architecture

```
src/swedish_budget_mcp/
|-- __init__.py        # Package version
|-- server.py          # FastMCP server, 15 tools, lifespan with auto-cache
|-- scb_client.py      # SCB PxWeb API client (async, retry with backoff)
|-- statskontoret.py   # Statskontoret client (scrape, revision select, streaming download)
|-- laffer.py          # Tax quota analysis with reform annotations
|-- cache.py           # SQLite cache (schema-versioned, atomic snapshots, dual-dataset validation)
|-- formatters.py      # ASCII visualization (bars, flow, decision chain, Laffer)
```

## Roadmap

- [x] SCB tax revenue and quota client
- [x] Statskontoret budget outturn client
- [x] Tax quota analysis module with reform annotations
- [x] SQLite cache with schema versioning and atomic snapshots
- [x] FastMCP server with 15 tools
- [x] ASCII formatters (bars, flow, decision, comparison, Laffer timeline)
- [x] Retry logic with exponential backoff
- [x] Download/ZIP size limits and host allowlist
- [x] Atomic staged sync with SyncError fallback
- [x] Income revision selection (definitiv > preliminar)
- [x] Streaming download with size enforcement
- [x] Publish to PyPI (`uvx swedish-budget-mcp`)
- [ ] Riksdagen voting client (propositions, votes per party)
- [ ] Taxpayer breakdown by income source (5-level drill-down with legislative history)
- [ ] Laffer #2: corporate tax (statutory rate vs revenue/GDP)
- [ ] Laffer #3: marginal income tax (Pomperipossa analysis)

## License

MIT
