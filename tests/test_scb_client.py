"""Tests for SCB PxWeb API client."""

import pytest

from swedish_budget_mcp.scb_client import (
    TAX_TYPE_LABELS,
    SCBClient,
    TaxQuotaRow,
    TaxRevenueRow,
)


@pytest.fixture
def scb():
    return SCBClient()


class TestQueryBuilding:
    def test_build_query_structure(self, scb: SCBClient):
        query = scb._build_query(["101", "190"], ["2023", "2024"], ["000000TE"])
        assert query["response"]["format"] == "json"
        assert len(query["query"]) == 3
        assert query["query"][0]["code"] == "Skattetyp"
        assert query["query"][0]["selection"]["values"] == ["101", "190"]
        assert query["query"][2]["selection"]["values"] == ["2023", "2024"]

    def test_build_query_empty_lists(self, scb: SCBClient):
        query = scb._build_query([], [], [])
        assert query["query"][0]["selection"]["values"] == []


class TestResponseParsing:
    def test_parse_flat_response_basic(self, scb: SCBClient):
        data = {
            "columns": [
                {"code": "Skattetyp", "type": "d"},
                {"code": "Tid", "type": "t"},
                {"code": "000000TE", "type": "c"},
            ],
            "data": [
                {"key": ["190", "2023"], "values": ["2847123"]},
                {"key": ["190", "2024"], "values": ["2901456"]},
            ],
        }
        rows = scb._parse_flat_response(data)
        assert len(rows) == 2
        assert rows[0]["Skattetyp"] == "190"
        assert rows[0]["Tid"] == "2023"
        assert rows[0]["000000TE"] == 2847123.0

    def test_parse_missing_values(self, scb: SCBClient):
        data = {
            "columns": [
                {"code": "Skattetyp", "type": "d"},
                {"code": "Tid", "type": "t"},
                {"code": "000000TE", "type": "c"},
            ],
            "data": [
                {"key": ["147", "2020"], "values": [".."]},
                {"key": ["147", "2021"], "values": ["."]},
                {"key": ["147", "2022"], "values": [""]},
            ],
        }
        rows = scb._parse_flat_response(data)
        assert all(row["000000TE"] is None for row in rows)


class TestLabels:
    def test_all_summary_types_have_labels(self):
        top_level = ["101", "140", "160", "180", "190"]
        for code in top_level:
            assert code in TAX_TYPE_LABELS

    def test_tax_type_labels_not_empty(self):
        assert len(TAX_TYPE_LABELS) > 30


@pytest.mark.asyncio
class TestIntegration:
    """Integration tests that hit the real SCB API.

    These are slow and require network access. Run with:
        pytest tests/ -m integration
    """

    pytestmark = pytest.mark.integration

    async def test_get_tax_revenue_single_year(self):
        async with SCBClient() as scb:
            rows = await scb.get_tax_revenue(years=[2023], tax_types=["190"])
            assert len(rows) == 1
            assert isinstance(rows[0], TaxRevenueRow)
            assert rows[0].year == 2023
            assert rows[0].amount_msek is not None
            assert rows[0].amount_msek > 0

    async def test_get_tax_quota_single_year(self):
        async with SCBClient() as scb:
            rows = await scb.get_tax_quota(years=[2020], tax_types=["102"])
            assert len(rows) == 1
            assert isinstance(rows[0], TaxQuotaRow)
            assert rows[0].share_of_gdp is not None
            assert 30 < rows[0].share_of_gdp < 60

    async def test_get_laffer_data(self):
        async with SCBClient() as scb:
            data = await scb.get_laffer_data(from_year=2000, to_year=2005)
            assert len(data) >= 5
            assert "year" in data[0]
            assert "tax_share_pct" in data[0]
            assert "gdp_msek" in data[0]

    async def test_get_revenue_timeseries(self):
        async with SCBClient() as scb:
            data = await scb.get_revenue_timeseries(from_year=2020, to_year=2023)
            assert len(data) == 4
            assert "labour" in data[0]
            assert "total" in data[0]
            assert data[0]["total"] > data[0]["labour"]
