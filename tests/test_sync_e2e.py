"""End-to-end sync tests with mocked HTTP.

Tests the full production call path:
  discover -> rank revision -> download -> parse
  -> validate -> atomic commit -> metadata

Uses httpx.MockTransport to return realistic Statskontoret
HTML and ZIP/CSV responses without network access.
"""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest

from swedish_budget_mcp.statskontoret import (
    ExpenditureRow,
    IncomeRow,
    StatskontoretClient,
    SyncError,
)

# -------------------------------------------------------
# Fixtures: realistic HTML and CSV content
# -------------------------------------------------------

_EXP_URL = (
    "https://www.statskontoret.se/getfile"
    "?documentType=Utgift"
    "&fileType=zip"
    "&status=Definitiv"
    "&fileName=utgifter_def.zip"
)

_INC_P1_URL = (
    "https://www.statskontoret.se/getfile"
    "?documentType=Inkomst"
    "&fileType=zip"
    "&status=Prelimin%C3%A4r+1"
    "&fileName=inkomster_p1.zip"
)

_INC_P2_URL = (
    "https://www.statskontoret.se/getfile"
    "?documentType=Inkomst"
    "&fileType=zip"
    "&status=Prelimin%C3%A4r+2"
    "&fileName=inkomster_p2.zip"
)

_MOCK_HTML = (
    "<html><body>"
    "<p>Senast uppdaterad 2026-03-15</p>"
    f'<a href="{_EXP_URL}">123 kB</a>'
    f'<a href="{_INC_P1_URL}">159 kB</a>'
    f'<a href="{_INC_P2_URL}">162 kB</a>'
    "</body></html>"
)

# Valid expenditure CSV (2 areas, required columns)
_EXP_CSV = (
    "Utgiftsomr\u00e5de;Utgiftsomr\u00e5desnamn;"
    "Anslag;Anslagsnamn;"
    "\u00c5r;Statens budget;Utfall\n"
    "01;Rikets styrelse;0101001;Hovet;"
    "2024;160,996;158,244\n"
    "06;F\u00f6rsvar;0601001;F\u00f6rband;"
    "2024;85000,0;84500,0\n"
)

# P1 income CSV: sentinel income_title = 1111
_INC_CSV_P1 = (
    "Inkomsttyp;Inkomsttypsnamn;"
    "Inkomsthuvudgrupp;Inkomsthuvudgruppsnamn;"
    "Inkomsttitel;Inkomsttitelsnamn;"
    "\u00c5r;Statens budget;Utfall\n"
    "1000;Skatteinkomster;1100;Arbete;"
    "1111;P1 Statlig;2024;51380,859;50805,949\n"
    "2000;Inkomster av statens verksamhet;"
    "2100;\u00d6vrigt;2111;P1 Div;"
    "2024;40000,0;38000,0\n"
)

# P2 income CSV: sentinel income_title = 2222
_INC_CSV_P2 = (
    "Inkomsttyp;Inkomsttypsnamn;"
    "Inkomsthuvudgrupp;Inkomsthuvudgruppsnamn;"
    "Inkomsttitel;Inkomsttitelsnamn;"
    "\u00c5r;Statens budget;Utfall\n"
    "1000;Skatteinkomster;1100;Arbete;"
    "2222;P2 Statlig;2024;51500,0;50900,0\n"
    "2000;Inkomster av statens verksamhet;"
    "2100;\u00d6vrigt;2222;P2 Div;"
    "2024;40500,0;38500,0\n"
)

# Two-year expenditure CSV (for get_budget_overview / get_biggest_changes)
_EXP_CSV_TWO_YEARS = (
    "Utgiftsområde;Utgiftsområdesnamn;"
    "Anslag;Anslagsnamn;"
    "År;Statens budget;Utfall\n"
    "01;Rikets styrelse;0101001;Hovet;"
    "2023;105,0;100,0\n"
    "01;Rikets styrelse;0101001;Hovet;"
    "2024;155,0;150,0\n"
    "06;Försvar;0601001;Förband;"
    "2023;85500,0;85000,0\n"
    "06;Försvar;0601001;Förband;"
    "2024;80500,0;80000,0\n"
)

# Invalid expenditure CSV: missing Utfall column
_EXP_CSV_NO_UTFALL = (
    "Utgiftsomr\u00e5de;Utgiftsomr\u00e5desnamn;"
    "Anslag;Anslagsnamn;\u00c5r;Statens budget\n"
    "01;Rikets styrelse;0101001;Hovet;"
    "2024;160,996\n"
    "06;F\u00f6rsvar;0601001;F\u00f6rband;"
    "2024;85000,0\n"
)


def _make_zip(csv_content: str) -> bytes:
    """Create an in-memory ZIP containing a CSV."""
    buf = io.BytesIO()
    with zipfile.ZipFile(
        buf, "w", zipfile.ZIP_DEFLATED,
    ) as zf:
        zf.writestr("data.csv", csv_content)
    return buf.getvalue()


def _build_transport(
    exp_csv: str = _EXP_CSV,
    inc_p1_csv: str = _INC_CSV_P1,
    inc_p2_csv: str = _INC_CSV_P2,
    html: str = _MOCK_HTML,
    requested_urls: list[str] | None = None,
) -> httpx.MockTransport:
    """Build a MockTransport serving HTML + ZIP files.

    P1 and P2 income CSVs are distinct so tests can prove
    which revision was actually downloaded and parsed.
    If requested_urls is provided, every request URL is
    appended to it for assertion.
    """
    exp_zip = _make_zip(exp_csv)
    inc_p1_zip = _make_zip(inc_p1_csv)
    inc_p2_zip = _make_zip(inc_p2_csv)

    def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        url = str(request.url)
        if requested_urls is not None:
            requested_urls.append(url)
        if "arsutfall" in url:
            return httpx.Response(
                200,
                text=html,
                headers={
                    "content-type": "text/html",
                },
            )
        if "Utgift" in url:
            return httpx.Response(
                200,
                content=exp_zip,
                headers={
                    "content-type": "application/zip",
                    "content-length": str(
                        len(exp_zip),
                    ),
                },
            )
        if "Prelimin%C3%A4r+1" in url:
            return httpx.Response(
                200,
                content=inc_p1_zip,
                headers={
                    "content-type": "application/zip",
                    "content-length": str(
                        len(inc_p1_zip),
                    ),
                },
            )
        if "Prelimin%C3%A4r+2" in url:
            return httpx.Response(
                200,
                content=inc_p2_zip,
                headers={
                    "content-type": "application/zip",
                    "content-length": str(
                        len(inc_p2_zip),
                    ),
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def _make_client(
    tmp_path,
    exp_csv: str = _EXP_CSV,
    inc_p1_csv: str = _INC_CSV_P1,
    inc_p2_csv: str = _INC_CSV_P2,
    requested_urls: list[str] | None = None,
) -> StatskontoretClient:
    """Create a StatskontoretClient with mocked transport."""
    transport = _build_transport(
        exp_csv=exp_csv,
        inc_p1_csv=inc_p1_csv,
        inc_p2_csv=inc_p2_csv,
        requested_urls=requested_urls,
    )
    client = StatskontoretClient(
        data_dir=tmp_path, timeout=5.0,
    )
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        transport=transport,
    )
    return client


# -------------------------------------------------------
# Test: sync selects best revision (proven by data)
# -------------------------------------------------------


@pytest.mark.asyncio
class TestSyncSelectsBestRevision:
    """Full sync with mocked HTTP picks Preliminar 2.

    P1 and P2 have distinct sentinel values in income_title
    (1111 vs 2222) so we can prove which archive was actually
    downloaded and parsed, not just which label was reported.
    """

    async def test_selects_preliminar_2_metadata(
        self, tmp_path,
    ):
        client = await _make_client(tmp_path)
        status = await client.sync()

        assert (
            status.sources[0].income_revision
            == "preliminar_2"
        )

    async def test_parsed_data_contains_p2_sentinel(
        self, tmp_path,
    ):
        """Prove P2 CSV was downloaded, not just labeled."""
        client = await _make_client(tmp_path)
        await client.sync()

        titles = {
            r.income_title
            for r in client.income_data
        }
        # P2 sentinel is "2222", P1 would be "1111"
        assert "2222" in titles
        assert "1111" not in titles

    async def test_p1_url_not_downloaded(
        self, tmp_path,
    ):
        """P1 archive should never be requested."""
        urls: list[str] = []
        client = await _make_client(
            tmp_path, requested_urls=urls,
        )
        await client.sync()

        download_urls = [
            u for u in urls if "getfile" in u
        ]
        p1_hits = [
            u
            for u in download_urls
            if "Prelimin%C3%A4r+1" in u
        ]
        assert p1_hits == [], (
            f"P1 URL was downloaded: {p1_hits}"
        )

    async def test_parses_expenditure(
        self, tmp_path,
    ):
        client = await _make_client(tmp_path)
        await client.sync()

        assert len(client.expenditure_data) == 2
        assert isinstance(
            client.expenditure_data[0],
            ExpenditureRow,
        )
        areas = {
            r.expenditure_area_id
            for r in client.expenditure_data
        }
        assert areas == {"01", "06"}

    async def test_parses_income(self, tmp_path):
        client = await _make_client(tmp_path)
        await client.sync()

        assert len(client.income_data) == 2
        assert isinstance(
            client.income_data[0], IncomeRow,
        )
        types = {
            r.income_type
            for r in client.income_data
        }
        assert types == {"1000", "2000"}

    async def test_metadata_complete(
        self, tmp_path,
    ):
        client = await _make_client(tmp_path)
        status = await client.sync()

        assert status.last_sync is not None
        src = status.sources[0]
        assert src.source_last_updated == "2026-03-15"
        assert len(src.files_downloaded) == 2
        assert 2024 in src.years_covered


# -------------------------------------------------------
# Test: sync rejects invalid schema
# -------------------------------------------------------


@pytest.mark.asyncio
class TestSyncRejectsInvalidSchema:
    """Sync raises SyncError when CSV lacks Utfall."""

    async def test_missing_utfall_raises_sync_error(
        self, tmp_path,
    ):
        client = await _make_client(
            tmp_path, exp_csv=_EXP_CSV_NO_UTFALL,
        )
        with pytest.raises(SyncError):
            await client.sync()

    async def test_prior_data_unchanged(
        self, tmp_path,
    ):
        """Old data survives a failed sync."""
        old_exp = [
            ExpenditureRow(
                "99", "Old", "9901", "Legacy",
                2023, 100.0, None, 90.0, None, None,
            ),
        ]
        old_inc = [
            IncomeRow(
                "9000", "Old", "9100", "Legacy",
                "9111", "Old", 2023, 50000.0, 48000.0,
            ),
        ]

        client = await _make_client(
            tmp_path, exp_csv=_EXP_CSV_NO_UTFALL,
        )
        client._expenditure_data = old_exp
        client._income_data = old_inc

        with pytest.raises(SyncError):
            await client.sync()

        assert client.expenditure_data == old_exp
        assert client.income_data == old_inc

    async def test_sync_error_diagnostics(
        self, tmp_path,
    ):
        client = await _make_client(
            tmp_path, exp_csv=_EXP_CSV_NO_UTFALL,
        )
        with pytest.raises(SyncError) as exc_info:
            await client.sync()

        err = exc_info.value
        # Expenditure failed (no Utfall), income OK
        assert err.has_expenditure is False
        assert err.has_income is True


# -------------------------------------------------------
# Test: MCP handlers expose income_revision
# -------------------------------------------------------


@pytest.mark.asyncio
class TestMCPHandlerGetSyncStatus:
    """get_sync_status returns income_revision after sync."""

    async def test_has_revision(self, tmp_path):
        import swedish_budget_mcp.server as srv
        from swedish_budget_mcp.cache import BudgetCache

        client = await _make_client(tmp_path)
        await client.sync()

        orig_sk = srv._sk
        orig_cache = srv._cache
        cache = BudgetCache(
            db_path=tmp_path / "test.db",
        )
        srv._sk = client
        srv._cache = cache

        try:
            result = await srv.get_sync_status()
            sources = result["sources"]
            assert len(sources) >= 1
            assert (
                sources[0]["income_revision"]
                == "preliminar_2"
            )
        finally:
            srv._sk = orig_sk
            srv._cache = orig_cache
            cache.close()

    async def test_source_keys_complete(
        self, tmp_path,
    ):
        import swedish_budget_mcp.server as srv
        from swedish_budget_mcp.cache import BudgetCache

        client = await _make_client(tmp_path)
        await client.sync()

        orig_sk = srv._sk
        orig_cache = srv._cache
        cache = BudgetCache(
            db_path=tmp_path / "test.db",
        )
        srv._sk = client
        srv._cache = cache

        try:
            result = await srv.get_sync_status()
            src = result["sources"][0]
            expected = {
                "source",
                "description",
                "publication_cadence",
                "last_synced_at",
                "source_last_updated",
                "files_downloaded",
                "years_covered",
                "income_revision",
            }
            assert set(src.keys()) == expected
        finally:
            srv._sk = orig_sk
            srv._cache = orig_cache
            cache.close()


@pytest.mark.asyncio
class TestSyncBudgetDataHandler:
    """sync_budget_data() handler: full write path.

    Exercises handler -> sync -> cache.store_snapshot
    -> source serialization -> response.
    """

    async def test_stores_snapshot_and_returns_revision(
        self, tmp_path,
    ):
        import swedish_budget_mcp.server as srv
        from swedish_budget_mcp.cache import BudgetCache

        # Build a mocked client that hasn't synced yet
        transport = _build_transport()
        sk = StatskontoretClient(
            data_dir=tmp_path / "sk", timeout=5.0,
        )
        await sk._client.aclose()
        sk._client = httpx.AsyncClient(
            transport=transport,
        )

        cache = BudgetCache(
            db_path=tmp_path / "handler.db",
        )

        orig_sk = srv._sk
        orig_cache = srv._cache
        srv._sk = sk
        srv._cache = cache

        try:
            result = await srv.sync_budget_data()

            # Snapshot was stored
            snapshot = result["snapshot"]
            assert snapshot["expenditure"] == 2
            assert snapshot["income"] == 2
            assert snapshot["complete"] is True

            # income_revision in response
            sources = result["sources"]
            assert len(sources) >= 1
            assert (
                sources[0]["income_revision"]
                == "preliminar_2"
            )

            # Cache is now populated
            assert cache.is_populated()
        finally:
            srv._sk = orig_sk
            srv._cache = orig_cache
            cache.close()

    async def test_cache_contains_correct_data(
        self, tmp_path,
    ):
        import swedish_budget_mcp.server as srv
        from swedish_budget_mcp.cache import BudgetCache

        transport = _build_transport()
        sk = StatskontoretClient(
            data_dir=tmp_path / "sk2", timeout=5.0,
        )
        await sk._client.aclose()
        sk._client = httpx.AsyncClient(
            transport=transport,
        )

        cache = BudgetCache(
            db_path=tmp_path / "handler2.db",
        )

        orig_sk = srv._sk
        orig_cache = srv._cache
        srv._sk = sk
        srv._cache = cache

        try:
            await srv.sync_budget_data()

            # Verify cached data matches parsed data
            exp_rows = cache.load_expenditure()
            inc_rows = cache.load_income()
            assert len(exp_rows) == 2
            assert len(inc_rows) == 2

            # P2 sentinel should be in cached income
            titles = {
                r["income_title"] for r in inc_rows
            }
            assert "2222" in titles
            assert "1111" not in titles
        finally:
            srv._sk = orig_sk
            srv._cache = orig_cache
            cache.close()


# -------------------------------------------------------
# Test: get_budget_overview handler, full production path
# -------------------------------------------------------


@pytest.mark.asyncio
class TestMCPHandlerGetBudgetOverview:
    """get_budget_overview() handler: sync -> client -> response.

    Exercises the real handler function (not just the client's
    get_budget_overview method), so a regression in as_of, rank,
    or the note fields is caught by CI, not just a manual check.
    """

    async def test_response_shape_and_values(
        self, tmp_path,
    ):
        import swedish_budget_mcp.server as srv

        client = await _make_client(tmp_path)
        await client.sync()

        orig_sk = srv._sk
        srv._sk = client

        try:
            result = await srv.get_budget_overview(2024)

            assert result["year"] == 2024
            assert result["data_type"] == "outturn"
            assert result["source"] == "Statskontoret"
            assert result["as_of"] is not None

            # Fixture: area 01 outcome 158.244, area 06
            # outcome 84500.0; income (P2) 50900.0 + 38500.0
            assert result[
                "total_expenditure_msek"
            ] == pytest.approx(158.244 + 84500.0)
            assert "total_expenditure_note" in result
            assert result[
                "total_income_msek"
            ] == pytest.approx(50900.0 + 38500.0)
            assert "balance_note" in result
            assert result["balance_msek"] == pytest.approx(
                (50900.0 + 38500.0)
                - (158.244 + 84500.0),
            )
        finally:
            srv._sk = orig_sk

    async def test_areas_have_rank_by_outcome(
        self, tmp_path,
    ):
        import swedish_budget_mcp.server as srv

        client = await _make_client(tmp_path)
        await client.sync()

        orig_sk = srv._sk
        srv._sk = client

        try:
            result = await srv.get_budget_overview(2024)
            by_id = {
                a["area_id"]: a for a in result["areas"]
            }
            # 06 (84500.0) outranks 01 (158.244)
            assert by_id["06"]["rank"] == 1
            assert by_id["01"]["rank"] == 2
        finally:
            srv._sk = orig_sk


# -------------------------------------------------------
# Test: get_biggest_changes handler, full production path
# -------------------------------------------------------


@pytest.mark.asyncio
class TestMCPHandlerGetBiggestChanges:
    """get_biggest_changes() handler: sync -> client -> response."""

    async def test_area_level_increase_and_decrease(
        self, tmp_path,
    ):
        import swedish_budget_mcp.server as srv

        client = await _make_client(
            tmp_path, exp_csv=_EXP_CSV_TWO_YEARS,
        )
        await client.sync()

        orig_sk = srv._sk
        srv._sk = client

        try:
            result = await srv.get_biggest_changes(
                2023, 2024,
            )

            assert result["data_type"] == "area_change"
            assert result["area_id"] is None
            assert result["as_of"] is not None

            inc_ids = {
                c["area_id"]
                for c in result["increases"]
            }
            dec_ids = {
                c["area_id"]
                for c in result["decreases"]
            }
            # 01 grows 100 -> 150 (+50), 06 shrinks
            # 85000 -> 80000 (-5000)
            assert inc_ids == {"01"}
            assert dec_ids == {"06"}

            increase = result["increases"][0]
            assert increase["delta_msek"] == pytest.approx(
                50.0,
            )
            decrease = result["decreases"][0]
            assert decrease["delta_msek"] == pytest.approx(
                -5000.0,
            )
        finally:
            srv._sk = orig_sk

    async def test_appropriation_level_drill_down(
        self, tmp_path,
    ):
        import swedish_budget_mcp.server as srv

        client = await _make_client(
            tmp_path, exp_csv=_EXP_CSV_TWO_YEARS,
        )
        await client.sync()

        orig_sk = srv._sk
        srv._sk = client

        try:
            result = await srv.get_biggest_changes(
                2023, 2024, area_id="06",
            )

            assert (
                result["data_type"]
                == "appropriation_change"
            )
            assert result["area_id"] == "06"

            decrease = result["decreases"][0]
            assert (
                decrease["appropriation_id"]
                == "0601001"
            )
            assert decrease["delta_msek"] == pytest.approx(
                -5000.0,
            )
        finally:
            srv._sk = orig_sk

    async def test_top_n_respected(self, tmp_path):
        import swedish_budget_mcp.server as srv

        client = await _make_client(
            tmp_path, exp_csv=_EXP_CSV_TWO_YEARS,
        )
        await client.sync()

        orig_sk = srv._sk
        srv._sk = client

        try:
            result = await srv.get_biggest_changes(
                2023, 2024, top_n=1,
            )
            assert result["top_n"] == 1
            assert len(result["increases"]) <= 1
            assert len(result["decreases"]) <= 1
        finally:
            srv._sk = orig_sk
