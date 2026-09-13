"""Tests for Laffer / tax quota analysis module."""

import pytest

from statsbudget_mcp.laffer import (
    TAX_REFORMS,
    LafferPoint,
    laffer_timeseries,
    laffer_to_chart_data,
)


def _make_points() -> list[LafferPoint]:
    """Create sample LafferPoints for testing."""
    return [
        LafferPoint(
            year=1975, tax_quota_pct=44.2, gdp_msek=250000,
            total_tax_msek=110500, nominal_gdp_growth_pct=2.1,
            decade="1970s", is_reform_year=False, reform_label=None,
        ),
        LafferPoint(
            year=1976, tax_quota_pct=47.8, gdp_msek=280000,
            total_tax_msek=None, nominal_gdp_growth_pct=-1.2,
            decade="1970s", is_reform_year=True,
            reform_label="Pomperipossa (102% marginalskatt)",
        ),
        LafferPoint(
            year=1990, tax_quota_pct=52.3, gdp_msek=1450000,
            total_tax_msek=758350, nominal_gdp_growth_pct=-1.1,
            decade="1990s", is_reform_year=False, reform_label=None,
        ),
        LafferPoint(
            year=1991, tax_quota_pct=49.1, gdp_msek=1530000,
            total_tax_msek=751230, nominal_gdp_growth_pct=-1.0,
            decade="1990s", is_reform_year=True,
            reform_label="\u00c5rhundradets skattereform",
        ),
        LafferPoint(
            year=2024, tax_quota_pct=42.5, gdp_msek=6400000,
            total_tax_msek=2720000, nominal_gdp_growth_pct=1.5,
            decade="2020s", is_reform_year=False, reform_label=None,
        ),
    ]


class TestLafferPoint:
    def test_dataclass_fields(self):
        p = _make_points()[0]
        assert p.year == 1975
        assert p.tax_quota_pct == 44.2
        assert p.decade == "1970s"
        assert p.is_reform_year is False

    def test_reform_year_detection(self):
        points = _make_points()
        reforms = [p for p in points if p.is_reform_year]
        assert len(reforms) == 2
        assert reforms[0].year == 1976
        assert "Pomperipossa" in reforms[0].reform_label


class TestChartData:
    def test_timeseries_key_exists(self):
        """Chart data uses 'timeseries' (not legacy 'datasets')."""
        points = _make_points()
        chart = laffer_to_chart_data(points)
        assert "timeseries" in chart
        assert "datasets" not in chart

    def test_timeseries_has_decade_info(self):
        """Each timeseries entry carries its decade tag."""
        points = _make_points()
        chart = laffer_to_chart_data(points)
        decades = {e["decade"] for e in chart["timeseries"]}
        assert "1970s" in decades
        assert "1990s" in decades
        assert "2020s" in decades

    def test_timeseries_length(self):
        points = _make_points()
        chart = laffer_to_chart_data(points)
        assert len(chart["timeseries"]) == len(points)

    def test_annotations_contain_reforms(self):
        points = _make_points()
        chart = laffer_to_chart_data(points)
        annotations = chart["annotations"]
        assert len(annotations) == 2
        assert annotations[0]["year"] == 1976
        assert annotations[1]["year"] == 1991

    def test_summary_statistics(self):
        points = _make_points()
        chart = laffer_to_chart_data(points)
        summary = chart["summary"]
        assert summary["peak_quota_pct"] == 52.3
        assert summary["peak_year"] == 1990
        assert summary["current_year"] == 2024
        assert summary["years_covered"] == 5

    def test_axis_labels_present(self):
        points = _make_points()
        chart = laffer_to_chart_data(points)
        assert "x" in chart["axis_labels"]
        assert "y" in chart["axis_labels"]


class TestChartDataEmpty:
    """laffer_to_chart_data([]) must not crash."""

    def test_empty_returns_dict(self):
        result = laffer_to_chart_data([])
        assert isinstance(result, dict)

    def test_empty_timeseries_is_list(self):
        result = laffer_to_chart_data([])
        assert result["timeseries"] == []

    def test_empty_annotations_is_list(self):
        result = laffer_to_chart_data([])
        assert result["annotations"] == []

    def test_empty_summary_nulls(self):
        result = laffer_to_chart_data([])
        s = result["summary"]
        assert s["min_quota_pct"] is None
        assert s["max_quota_pct"] is None
        assert s["peak_year"] is None
        assert s["current_year"] is None
        assert s["years_covered"] == 0

    def test_empty_axis_labels(self):
        result = laffer_to_chart_data([])
        assert "x" in result["axis_labels"]
        assert "y" in result["axis_labels"]


class TestNonePreservation:
    """total_tax_msek=None must stay None, not become 0."""

    def test_none_in_chart_timeseries(self):
        points = _make_points()
        chart = laffer_to_chart_data(points)
        row_1976 = [t for t in chart["timeseries"] if t["year"] == 1976]
        assert len(row_1976) == 1
        assert row_1976[0]["tax_msek"] is None

    def test_none_in_flat_timeseries(self):
        points = _make_points()
        ts = laffer_timeseries(points)
        row_1976 = [t for t in ts if t["year"] == 1976]
        assert len(row_1976) == 1
        assert row_1976[0]["total_tax_msek"] is None

    def test_non_none_values_intact(self):
        points = _make_points()
        ts = laffer_timeseries(points)
        row_1975 = [t for t in ts if t["year"] == 1975]
        assert row_1975[0]["total_tax_msek"] == 110500


class TestTimeseries:
    def test_output_format(self):
        points = _make_points()
        ts = laffer_timeseries(points)
        assert len(ts) == 5
        assert ts[0]["year"] == 1975
        assert ts[0]["tax_quota_pct"] == 44.2
        assert ts[0]["reform"] is None

    def test_nominal_growth_field_name(self):
        """Field is 'nominal_gdp_growth_pct', not 'real_gdp_growth_pct'."""
        points = _make_points()
        ts = laffer_timeseries(points)
        for entry in ts:
            assert "nominal_gdp_growth_pct" in entry
            assert "real_gdp_growth_pct" not in entry

    def test_reform_annotations_in_timeseries(self):
        points = _make_points()
        ts = laffer_timeseries(points)
        reform_entries = [t for t in ts if t["reform"] is not None]
        assert len(reform_entries) == 2


class TestTaxReforms:
    def test_pomperipossa_included(self):
        years = [r["year"] for r in TAX_REFORMS]
        assert 1976 in years

    def test_1991_reform_included(self):
        years = [r["year"] for r in TAX_REFORMS]
        assert 1991 in years

    def test_all_reforms_have_required_fields(self):
        for reform in TAX_REFORMS:
            assert "year" in reform
            assert "label" in reform
            assert "description" in reform
            assert isinstance(reform["year"], int)


@pytest.mark.asyncio
class TestIntegration:
    """Integration tests hitting real SCB API."""

    pytestmark = pytest.mark.integration

    async def test_build_laffer_curve(self):
        from statsbudget_mcp.laffer import build_laffer_curve

        async with SCBClient() as scb:
            points = await build_laffer_curve(scb, from_year=2000, to_year=2005)
            assert len(points) >= 5
            assert all(isinstance(p, LafferPoint) for p in points)
            assert all(p.tax_quota_pct > 0 for p in points)
            assert all(20 < p.tax_quota_pct < 60 for p in points)


# Need import for integration test
from statsbudget_mcp.scb_client import SCBClient  # noqa: E402
