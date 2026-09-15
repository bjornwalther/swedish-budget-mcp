"""Tests for the FastMCP server module."""

import pytest

# Tool functions are module-level, imported to verify they exist
from swedish_budget_mcp.server import (
    EXPENDITURE_AREAS,
    _require_cache,
    _require_scb,
    _require_sk,
    _serialize_source,
    compare_budgets,
    get_available_years,
    get_biggest_changes,
    get_budget_overview,
    get_cache_stats,
    get_expenditure_area,
    get_laffer_data,
    get_laffer_timeseries,
    get_publication_schedule,
    get_revenue,
    get_revenue_detail,
    get_revenue_timeseries,
    get_sync_status,
    get_tax_reforms,
    mcp,
    sync_budget_data,
)


class TestServerSetup:
    def test_mcp_name(self):
        assert mcp.name == "swedish-budget-mcp"

    def test_mcp_has_instructions(self):
        instructions = (
            getattr(mcp, "instructions", None) or ""
        )
        assert "Swedish national budget" in instructions

    def test_expenditure_areas_count(self):
        assert len(EXPENDITURE_AREAS) == 27

    def test_expenditure_areas_format(self):
        for area_id, name in EXPENDITURE_AREAS:
            assert len(area_id) == 2
            assert area_id.isdigit()
            assert len(name) > 0

    def test_area_ids_sequential(self):
        ids = [
            int(aid) for aid, _ in EXPENDITURE_AREAS
        ]
        assert ids == list(range(1, 28))


class TestClientGuards:
    def test_require_scb_raises_when_not_initialized(
        self,
    ):
        import swedish_budget_mcp.server as mod

        original = mod._scb
        mod._scb = None
        try:
            with pytest.raises(
                RuntimeError,
                match="SCB client not initialized",
            ):
                _require_scb()
        finally:
            mod._scb = original

    def test_require_sk_raises_when_not_initialized(
        self,
    ):
        import swedish_budget_mcp.server as mod

        original = mod._sk
        mod._sk = None
        try:
            with pytest.raises(
                RuntimeError,
                match="Statskontoret client not"
                " initialized",
            ):
                _require_sk()
        finally:
            mod._sk = original

    def test_require_cache_raises_when_not_initialized(
        self,
    ):
        import swedish_budget_mcp.server as mod

        original = mod._cache
        mod._cache = None
        try:
            with pytest.raises(
                RuntimeError,
                match="Cache not initialized",
            ):
                _require_cache()
        finally:
            mod._cache = original


class TestToolRegistration:
    """Verify all 15 tool functions are importable."""

    EXPECTED_TOOLS = [
        get_budget_overview,
        get_expenditure_area,
        compare_budgets,
        get_biggest_changes,
        sync_budget_data,
        get_revenue,
        get_revenue_timeseries,
        get_revenue_detail,
        get_laffer_data,
        get_laffer_timeseries,
        get_tax_reforms,
        get_sync_status,
        get_publication_schedule,
        get_available_years,
        get_cache_stats,
    ]

    def test_all_tools_callable(self):
        for fn in self.EXPECTED_TOOLS:
            assert callable(fn), (
                f"{fn.__name__} not callable"
            )

    def test_total_tool_count(self):
        assert len(self.EXPECTED_TOOLS) == 15


@pytest.mark.asyncio
class TestToolRegistrationPublicAPI:
    """Verify tools via FastMCP's public list_tools() API.

    This catches cases where functions exist but the @mcp.tool()
    decorator was removed or misconfigured.
    """

    EXPECTED_NAMES = {
        "get_budget_overview",
        "get_expenditure_area",
        "compare_budgets",
        "get_biggest_changes",
        "sync_budget_data",
        "get_revenue",
        "get_revenue_timeseries",
        "get_revenue_detail",
        "get_laffer_data",
        "get_laffer_timeseries",
        "get_tax_reforms",
        "get_sync_status",
        "get_publication_schedule",
        "get_available_years",
        "get_cache_stats",
    }

    async def test_list_tools_returns_15(self):
        tools = await mcp.list_tools()
        assert len(tools) == 15

    async def test_list_tools_contains_all_names(self):
        tools = await mcp.list_tools()
        names = {t.name for t in tools}
        assert names == self.EXPECTED_NAMES

    async def test_budget_tools_registered(self):
        tools = await mcp.list_tools()
        names = {t.name for t in tools}
        assert "get_budget_overview" in names
        assert "get_expenditure_area" in names
        assert "compare_budgets" in names
        assert "get_biggest_changes" in names
        assert "sync_budget_data" in names

    async def test_revenue_tools_registered(self):
        tools = await mcp.list_tools()
        names = {t.name for t in tools}
        assert "get_revenue" in names
        assert "get_revenue_timeseries" in names
        assert "get_revenue_detail" in names

    async def test_tools_have_descriptions(self):
        tools = await mcp.list_tools()
        for tool in tools:
            assert tool.description, (
                f"{tool.name} has no description"
            )


class TestSourceSerialization:
    """_serialize_source includes all fields."""

    def test_includes_income_revision(self):
        from swedish_budget_mcp.statskontoret import (
            DataSourceMeta,
        )

        meta = DataSourceMeta(
            source="test",
            description="test desc",
            publication_cadence="monthly",
            income_revision="preliminar_2",
        )
        result = _serialize_source(meta)
        assert result["income_revision"] == "preliminar_2"

    def test_income_revision_none_when_unset(self):
        from swedish_budget_mcp.statskontoret import (
            DataSourceMeta,
        )

        meta = DataSourceMeta(
            source="test",
            description="test desc",
            publication_cadence="monthly",
        )
        result = _serialize_source(meta)
        assert result["income_revision"] is None

    def test_all_expected_keys_present(self):
        from swedish_budget_mcp.statskontoret import (
            DataSourceMeta,
        )

        meta = DataSourceMeta(
            source="s",
            description="d",
            publication_cadence="c",
        )
        result = _serialize_source(meta)
        expected_keys = {
            "source",
            "description",
            "publication_cadence",
            "last_synced_at",
            "source_last_updated",
            "files_downloaded",
            "years_covered",
            "income_revision",
        }
        assert set(result.keys()) == expected_keys


class TestWithRank:
    """_with_rank attaches 1-based rank without reordering."""

    def test_ranks_by_value_descending(self):
        from swedish_budget_mcp.server import _with_rank

        rows = [
            {"id": "a", "outcome_msek": 10},
            {"id": "b", "outcome_msek": 30},
            {"id": "c", "outcome_msek": 20},
        ]
        ranked = _with_rank(rows, "outcome_msek")
        by_id = {r["id"]: r["rank"] for r in ranked}
        assert by_id == {"a": 3, "b": 1, "c": 2}

    def test_preserves_original_order(self):
        from swedish_budget_mcp.server import _with_rank

        rows = [
            {"id": "a", "outcome_msek": 10},
            {"id": "b", "outcome_msek": 30},
        ]
        ranked = _with_rank(rows, "outcome_msek")
        assert [r["id"] for r in ranked] == ["a", "b"]

    def test_none_value_treated_as_zero(self):
        from swedish_budget_mcp.server import _with_rank

        rows = [
            {"id": "a", "outcome_msek": None},
            {"id": "b", "outcome_msek": 5},
        ]
        ranked = _with_rank(rows, "outcome_msek")
        by_id = {r["id"]: r["rank"] for r in ranked}
        assert by_id == {"a": 2, "b": 1}

    def test_ties_broken_by_original_position(self):
        from swedish_budget_mcp.server import _with_rank

        rows = [
            {"id": "a", "outcome_msek": 10},
            {"id": "b", "outcome_msek": 10},
        ]
        ranked = _with_rank(rows, "outcome_msek")
        by_id = {r["id"]: r["rank"] for r in ranked}
        assert by_id == {"a": 1, "b": 2}


class TestSyncErrorHandling:
    """Server imports and catches SyncError."""

    def test_sync_error_in_sync_errors_tuple(self):
        from swedish_budget_mcp.server import _SYNC_ERRORS
        from swedish_budget_mcp.statskontoret import (
            SyncError,
        )

        assert SyncError in _SYNC_ERRORS

    def test_value_error_in_sync_errors_tuple(self):
        from swedish_budget_mcp.server import _SYNC_ERRORS

        assert ValueError in _SYNC_ERRORS


class TestEntryPoint:
    def test_main_function_exists(self):
        from swedish_budget_mcp.server import main

        assert callable(main)
