"""Tests for Statskontoret CSV client."""

import os
import tempfile
import zipfile

import pytest

from swedish_budget_mcp.statskontoret import (
    MAX_CSV_BYTES,
    MAX_DOWNLOAD_BYTES,
    BudgetOverview,
    ExpenditureRow,
    IncomeRow,
    StatskontoretClient,
    SyncError,
    _check_required_headers,
    _classify_revision,
    _is_allowed_host,
    _parse_swedish_decimal,
    _validate_expenditure_rows,
    _validate_income_rows,
)


class TestSwedishDecimalParsing:
    def test_normal_value(self):
        assert _parse_swedish_decimal(
            "136,996",
        ) == pytest.approx(136.996)

    def test_negative_value(self):
        assert _parse_swedish_decimal(
            "-40,62704977",
        ) == pytest.approx(-40.62704977)

    def test_large_value(self):
        assert _parse_swedish_decimal(
            "1428000,12345678",
        ) == pytest.approx(1428000.12345678)

    def test_empty_string(self):
        assert _parse_swedish_decimal("") is None

    def test_double_dot(self):
        assert _parse_swedish_decimal("..") is None

    def test_dash(self):
        assert _parse_swedish_decimal("-") is None

    def test_whitespace_handling(self):
        assert _parse_swedish_decimal(
            " 1 234,56 ",
        ) == pytest.approx(1234.56)

    def test_non_breaking_space(self):
        assert _parse_swedish_decimal(
            "1\xa0234,56",
        ) == pytest.approx(1234.56)


class TestRevisionClassification:
    """_classify_revision picks best income revision."""

    def test_definitiv_highest_priority(self):
        label, prio = _classify_revision(
            "https://example.com"
            "?documentType=InkomstDefinitiv",
            "CSV (Definitiva inkomster)",
        )
        assert label == "definitiv"
        assert prio == 4

    def test_preliminar_3(self):
        label, prio = _classify_revision(
            "https://example.com",
            "CSV (Prelimin\u00e4r 3)",
        )
        assert label == "preliminar_3"
        assert prio == 3

    def test_preliminar_2(self):
        label, prio = _classify_revision(
            "https://example.com",
            "CSV (Prelimin\u00e4r 2)",
        )
        assert label == "preliminar_2"
        assert prio == 2

    def test_preliminar_1(self):
        label, prio = _classify_revision(
            "https://example.com",
            "CSV (Prelimin\u00e4r 1)",
        )
        assert label == "preliminar_1"
        assert prio == 1

    def test_unknown_fallback(self):
        label, prio = _classify_revision(
            "https://example.com",
            "CSV (data)",
        )
        assert label == "unknown"
        assert prio == 0

    def test_definitiv_beats_preliminar(self):
        _, p_def = _classify_revision(
            "", "Definitiva inkomster",
        )
        _, p_p2 = _classify_revision(
            "", "Prelimin\u00e4r 2",
        )
        _, p_p1 = _classify_revision(
            "", "Prelimin\u00e4r 1",
        )
        assert p_def > p_p2 > p_p1

    def test_documenttype_param_used(self):
        _label, prio = _classify_revision(
            "https://x.se"
            "?documentType=InkomstPreliminar2",
            "CSV",
        )
        assert prio == 2


class TestRevisionFromStatusParam:
    """Classify revision from real Statskontoret URL patterns.

    The live page uses status=Preliminar+1 (URL-encoded space)
    in the query string, not documentType or anchor text.
    These tests use the actual URL shapes observed on the live
    Statskontoret arsutfall page.
    """

    # Real URL pattern (simplified, host removed):
    # /getfile?documentType=Inkomst&fileType=zip
    #   &status=Prelimin%C3%A4r+1&fileName=...
    _BASE = (
        "https://www.statskontoret.se/getfile"
        "?documentType=Inkomst&fileType=zip"
    )

    def test_status_preliminar_1(self):
        url = (
            f"{self._BASE}"
            "&status=Prelimin%C3%A4r+1"
            "&fileName=inkomster_p1.zip"
        )
        label, prio = _classify_revision(
            url, "159 kB",
        )
        assert label == "preliminar_1"
        assert prio == 1

    def test_status_preliminar_2(self):
        url = (
            f"{self._BASE}"
            "&status=Prelimin%C3%A4r+2"
            "&fileName=inkomster_p2.zip"
        )
        label, prio = _classify_revision(
            url, "162 kB",
        )
        assert label == "preliminar_2"
        assert prio == 2

    def test_status_definitiv(self):
        url = (
            f"{self._BASE}"
            "&status=Definitiv"
            "&fileName=inkomster_def.zip"
        )
        label, prio = _classify_revision(
            url, "170 kB",
        )
        assert label == "definitiv"
        assert prio == 4

    def test_filename_fallback(self):
        """Revision from fileName when status is absent."""
        url = (
            f"{self._BASE}"
            "&fileName=Prelimin%C3%A4r+3.zip"
        )
        label, prio = _classify_revision(
            url, "155 kB",
        )
        assert label == "preliminar_3"
        assert prio == 3

    def test_p2_beats_p1_via_priority(self):
        """When sorted, P2 should rank above P1."""
        url_p1 = (
            f"{self._BASE}"
            "&status=Prelimin%C3%A4r+1"
        )
        url_p2 = (
            f"{self._BASE}"
            "&status=Prelimin%C3%A4r+2"
        )
        _, prio_p1 = _classify_revision(
            url_p1, "159 kB",
        )
        _, prio_p2 = _classify_revision(
            url_p2, "162 kB",
        )
        assert prio_p2 > prio_p1


class TestHostAllowlist:
    """_is_allowed_host permits only statskontoret.se."""

    def test_www_allowed(self):
        assert _is_allowed_host(
            "https://www.statskontoret.se/foo",
        ) is True

    def test_bare_domain_allowed(self):
        assert _is_allowed_host(
            "https://statskontoret.se/bar",
        ) is True

    def test_evil_domain_blocked(self):
        assert _is_allowed_host(
            "https://evil.com/foo",
        ) is False

    def test_subdomain_blocked(self):
        assert _is_allowed_host(
            "https://api.statskontoret.se/foo",
        ) is False

    def test_empty_blocked(self):
        assert _is_allowed_host("") is False

    def test_garbage_blocked(self):
        assert _is_allowed_host("not-a-url") is False


class TestSizeLimitConstants:
    def test_download_limit_exists(self):
        assert isinstance(MAX_DOWNLOAD_BYTES, int)
        assert MAX_DOWNLOAD_BYTES > 1 * 1024 * 1024

    def test_csv_limit_exists(self):
        assert isinstance(MAX_CSV_BYTES, int)
        assert MAX_CSV_BYTES >= MAX_DOWNLOAD_BYTES


class TestZipSizeGuard:
    def _make_zip(self, csv_content, name="data.csv"):
        tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115
            suffix=".zip", delete=False,
        )
        with zipfile.ZipFile(
            tmp, "w", zipfile.ZIP_DEFLATED,
        ) as zf:
            zf.writestr(name, csv_content)
        tmp.close()
        return tmp.name

    def test_small_csv_passes(self):
        path = self._make_zip(
            "col1;col2\nval1;val2\n",
        )
        try:
            from pathlib import Path

            client = StatskontoretClient()
            result = client._extract_csv_from_zip(
                Path(path),
            )
            assert result is not None
            assert "col1" in result
        finally:
            os.unlink(path)

    def test_oversized_csv_rejected(self):
        import swedish_budget_mcp.statskontoret as sk

        original = sk.MAX_CSV_BYTES
        try:
            sk.MAX_CSV_BYTES = 10
            path = self._make_zip("a" * 100)
            try:
                from pathlib import Path

                client = StatskontoretClient()
                result = client._extract_csv_from_zip(
                    Path(path),
                )
                assert result is None
            finally:
                os.unlink(path)
        finally:
            sk.MAX_CSV_BYTES = original

    def test_bad_zip_returns_none(self):
        tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115
            suffix=".zip", delete=False,
        )
        tmp.write(b"not a zip")
        tmp.close()
        try:
            from pathlib import Path

            client = StatskontoretClient()
            result = client._extract_csv_from_zip(
                Path(tmp.name),
            )
            assert result is None
        finally:
            os.unlink(tmp.name)

    def test_zip_without_csv_returns_none(self):
        tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115
            suffix=".zip", delete=False,
        )
        with zipfile.ZipFile(tmp, "w") as zf:
            zf.writestr("readme.txt", "no csv")
        tmp.close()
        try:
            from pathlib import Path

            client = StatskontoretClient()
            result = client._extract_csv_from_zip(
                Path(tmp.name),
            )
            assert result is None
        finally:
            os.unlink(tmp.name)


class TestSyncError:
    def test_is_exception(self):
        assert issubclass(SyncError, Exception)

    def test_attributes(self):
        err = SyncError(
            "missing income",
            has_expenditure=True,
            has_income=False,
        )
        assert err.has_expenditure is True
        assert err.has_income is False
        assert "missing income" in str(err)

    def test_both_missing(self):
        err = SyncError(
            "both",
            has_expenditure=False,
            has_income=False,
        )
        assert not err.has_expenditure
        assert not err.has_income


class TestHeaderValidation:
    """_check_required_headers detects missing CSV columns."""

    def test_all_present(self):
        col_map = {
            "year": "\u00c5r",
            "outcome": "Utfall",
            "area_id": "Utgiftsomr\u00e5de",
        }
        fieldnames = [
            "Utgiftsomr\u00e5de",
            "Utgiftsomr\u00e5desnamn",
            "\u00c5r",
            "Utfall",
        ]
        missing = _check_required_headers(
            col_map, fieldnames, {"year", "outcome", "area_id"},
        )
        assert missing == []

    def test_outcome_missing(self):
        col_map = {
            "year": "\u00c5r",
            "outcome": "Utfall",
            "area_id": "Utgiftsomr\u00e5de",
        }
        fieldnames = [
            "Utgiftsomr\u00e5de",
            "\u00c5r",
        ]
        missing = _check_required_headers(
            col_map, fieldnames, {"year", "outcome", "area_id"},
        )
        assert "outcome" in missing

    def test_year_missing(self):
        col_map = {
            "year": "\u00c5r",
            "outcome": "Utfall",
            "area_id": "Utgiftsomr\u00e5de",
        }
        fieldnames = [
            "Utgiftsomr\u00e5de",
            "Utfall",
        ]
        missing = _check_required_headers(
            col_map, fieldnames, {"year", "outcome", "area_id"},
        )
        assert "year" in missing


class TestDataInvariantValidation:
    """Post-parse validation catches bad data."""

    def test_good_expenditure_passes(self):
        rows = [
            ExpenditureRow(
                "01", "A", "0101", "X",
                2024, 100.0, None, 95.0, None, None,
            ),
            ExpenditureRow(
                "06", "B", "0601", "Y",
                2024, 200.0, None, 190.0, None, None,
            ),
        ]
        assert _validate_expenditure_rows(rows) == []

    def test_all_outcome_none_fails(self):
        rows = [
            ExpenditureRow(
                "01", "A", "0101", "X",
                2024, 100.0, None, None, None, None,
            ),
            ExpenditureRow(
                "06", "B", "0601", "Y",
                2024, 200.0, None, None, None, None,
            ),
        ]
        problems = _validate_expenditure_rows(rows)
        assert any("outcome" in p for p in problems)

    def test_invalid_year_fails(self):
        rows = [
            ExpenditureRow(
                "01", "A", "0101", "X",
                0, 100.0, None, 95.0, None, None,
            ),
            ExpenditureRow(
                "06", "B", "0601", "Y",
                0, 200.0, None, 190.0, None, None,
            ),
        ]
        problems = _validate_expenditure_rows(rows)
        assert any("year" in p for p in problems)

    def test_single_area_fails(self):
        rows = [
            ExpenditureRow(
                "01", "A", "0101", "X",
                2024, 100.0, None, 95.0, None, None,
            ),
            ExpenditureRow(
                "01", "A", "0102", "Y",
                2024, 200.0, None, 190.0, None, None,
            ),
        ]
        problems = _validate_expenditure_rows(rows)
        assert any("area" in p.lower() for p in problems)

    def test_good_income_passes(self):
        rows = [
            IncomeRow(
                "1000", "T", "1100", "W",
                "1111", "S", 2024, 50000.0, 48000.0,
            ),
            IncomeRow(
                "2000", "O", "2100", "M",
                "2111", "D", 2024, 40000.0, 38000.0,
            ),
        ]
        assert _validate_income_rows(rows) == []

    def test_income_all_outcome_none_fails(self):
        rows = [
            IncomeRow(
                "1000", "T", "1100", "W",
                "1111", "S", 2024, 50000.0, None,
            ),
            IncomeRow(
                "2000", "O", "2100", "M",
                "2111", "D", 2024, 40000.0, None,
            ),
        ]
        problems = _validate_income_rows(rows)
        assert any("outcome" in p for p in problems)


class TestParseRejectsMissingHeaders:
    """Parse methods return empty on missing critical columns.

    Exercises the full parse call path, not just the header
    check function, reproducing the reviewer's exact scenario.
    """

    def test_expenditure_missing_utfall(self):
        csv = (
            "Utgiftsomr\u00e5de;Utgiftsomr\u00e5desnamn;"
            "Anslag;\u00c5r\n"
            "01;Rikets styrelse;0101001;2024\n"
        )
        client = StatskontoretClient()
        rows = client._parse_expenditure_csv(csv)
        assert rows == []

    def test_expenditure_missing_year(self):
        csv = (
            "Utgiftsomr\u00e5de;Utgiftsomr\u00e5desnamn;"
            "Anslag;Utfall\n"
            "01;Rikets styrelse;0101001;158,244\n"
        )
        client = StatskontoretClient()
        rows = client._parse_expenditure_csv(csv)
        assert rows == []

    def test_income_missing_utfall(self):
        csv = (
            "Inkomsttyp;Inkomsttypsnamn;"
            "Inkomsthuvudgrupp;"
            "Inkomsthuvudgruppsnamn;"
            "Inkomsttitel;Inkomsttitelsnamn;"
            "\u00c5r\n"
            "1000;Skatter;1100;Arbete;"
            "1111;Statlig;2024\n"
        )
        client = StatskontoretClient()
        rows = client._parse_income_csv(csv)
        assert rows == []

    def test_good_csv_still_parses(self):
        csv = (
            "Utgiftsomr\u00e5de;Utgiftsomr\u00e5desnamn;"
            "Anslag;Anslagsnamn;"
            "\u00c5r;Statens budget;Utfall\n"
            "01;Rikets styrelse;0101001;Hovet;"
            "2024;160,996;158,244\n"
        )
        client = StatskontoretClient()
        rows = client._parse_expenditure_csv(csv)
        assert len(rows) == 1
        assert rows[0].outcome_msek is not None


class TestExpenditureCsvParsing:
    SAMPLE_CSV = (
        "Utgiftsomr\u00e5de;Utgiftsomr\u00e5desnamn;"
        "Anslag;Anslagsnamn;"
        "Utgiftsomr\u00e5de utfalls\u00e5r;"
        "Utgiftsomr\u00e5desnamn utfalls\u00e5r;"
        "Anslag utfalls\u00e5r;"
        "Anslagsnamn utfalls\u00e5r;"
        "\u00c5r;"
        "Ing\u00e5ende \u00f6verf\u00f6ringsbelopp;"
        "Statens budget;"
        "\u00c4ndringsbudgetar;Indragningar;"
        "Utnyttjad del av medgivet"
        "\u00f6verskridande;Utfall;"
        "Anslagskredit;"
        "Utg\u00e5ende"
        " \u00f6verf\u00f6ringsbelopp\n"
        "01;Rikets styrelse;0101001;"
        "Kungliga hov- och slottsstaten;"
        "01;Rikets styrelse;0101001;"
        "Kungliga hov- och slottsstaten;"
        "2024;0,5;160,996;0;0;0;"
        "158,244;4,83;3,252\n"
        "06;F\u00f6rsvar och samh\u00e4llets"
        " krisberedskap;"
        "0601001;F\u00f6rbandsverksamhet;"
        "06;F\u00f6rsvar och samh\u00e4llets"
        " krisberedskap;"
        "0601001;F\u00f6rbandsverksamhet;"
        "2024;1200,0;85000,0;500,0;0;0;"
        "84500,0;2550,0;1200,0\n"
    )

    def test_parse_expenditure_csv(self):
        client = StatskontoretClient()
        rows = client._parse_expenditure_csv(
            self.SAMPLE_CSV,
        )
        assert len(rows) == 2
        assert isinstance(rows[0], ExpenditureRow)

    def test_first_row_values(self):
        client = StatskontoretClient()
        rows = client._parse_expenditure_csv(
            self.SAMPLE_CSV,
        )
        row = rows[0]
        assert row.expenditure_area_id == "01"
        assert row.expenditure_area_name == (
            "Rikets styrelse"
        )
        assert row.appropriation_id == "0101001"
        assert row.year == 2024
        assert row.budget_msek == pytest.approx(160.996)
        assert row.outcome_msek == pytest.approx(158.244)

    def test_defence_row(self):
        client = StatskontoretClient()
        rows = client._parse_expenditure_csv(
            self.SAMPLE_CSV,
        )
        row = rows[1]
        assert row.expenditure_area_id == "06"
        assert row.budget_msek == pytest.approx(85000.0)
        assert row.outcome_msek == pytest.approx(84500.0)


class TestIncomeCsvParsing:
    SAMPLE_CSV = (
        "Inkomsttyp;Inkomsttypsnamn;"
        "Inkomsthuvudgrupp;"
        "Inkomsthuvudgruppsnamn;"
        "Inkomsttitelgrupp;"
        "Inkomsttitelgruppsnamn;"
        "Inkomsttitel;Inkomsttitelsnamn;"
        "Inkomsttyp utfalls\u00e5r;"
        "Inkomsttypsnamn utfalls\u00e5r;"
        "Inkomsthuvudgrupp utfalls\u00e5r;"
        "Inkomsthuvudgruppsnamn utfalls\u00e5r;"
        "Inkomsttitelgrupp utfalls\u00e5r;"
        "Inkomsttitelgruppsnamn utfalls\u00e5r;"
        "Inkomsttitel utfalls\u00e5r;"
        "Inkomsttitelsnamn utfalls\u00e5r;"
        "\u00c5r;Statens budget;Utfall\n"
        "1000;Statens skatteinkomster;"
        "1100;Direkta skatter p\u00e5 arbete;"
        "1110;Inkomstskatter;"
        "1111;Statlig inkomstskatt;"
        "1000;Statens skatteinkomster;"
        "1100;Direkta skatter p\u00e5 arbete;"
        "1110;Inkomstskatter;"
        "1111;Statlig inkomstskatt;"
        "2024;51380,859539;50805,94943\n"
    )

    def test_parse_income_csv(self):
        client = StatskontoretClient()
        rows = client._parse_income_csv(
            self.SAMPLE_CSV,
        )
        assert len(rows) == 1
        assert isinstance(rows[0], IncomeRow)

    def test_income_row_values(self):
        client = StatskontoretClient()
        rows = client._parse_income_csv(
            self.SAMPLE_CSV,
        )
        row = rows[0]
        assert row.income_type == "1000"
        assert row.income_type_name == (
            "Statens skatteinkomster"
        )
        assert row.income_title == "1111"
        assert row.year == 2024
        assert row.budget_msek == pytest.approx(
            51380.859539,
        )
        assert row.outcome_msek == pytest.approx(
            50805.94943,
        )


class TestBudgetOverview:
    def test_overview_aggregation(self):
        client = StatskontoretClient()
        client._expenditure_data = [
            ExpenditureRow(
                "01", "Rikets styrelse", "0101001",
                "Hovet", 2024, 160.0, None, 158.0,
                None, None,
            ),
            ExpenditureRow(
                "01", "Rikets styrelse", "0101002",
                "Riksdagen", 2024, 2000.0, None,
                1950.0, None, None,
            ),
            ExpenditureRow(
                "06", "F\u00f6rsvar", "0601001",
                "F\u00f6rband", 2024, 85000.0, None,
                84500.0, None, None,
            ),
        ]
        client._income_data = [
            IncomeRow(
                "1000", "Skatter", "1100", "Arbete",
                "1111", "Statlig", 2024, 51000.0,
                50000.0,
            ),
            IncomeRow(
                "2000", "Inkomster", "2100",
                "\u00d6vrigt", "2111", "Div",
                2024, 40000.0, 38000.0,
            ),
        ]

        overview = client.get_budget_overview(2024)
        assert isinstance(overview, BudgetOverview)
        assert overview.year == 2024
        assert (
            overview.total_expenditure_msek
            == pytest.approx(
                158.0 + 1950.0 + 84500.0,
            )
        )
        assert (
            overview.total_income_msek
            == pytest.approx(50000.0 + 38000.0)
        )
        assert len(overview.areas) == 2
        assert overview.areas[0].area_id == "01"
        assert (
            overview.areas[0].outcome_msek
            == pytest.approx(158.0 + 1950.0)
        )


class TestBudgetComparison:
    def test_compare_two_years(self):
        client = StatskontoretClient()
        client._expenditure_data = [
            ExpenditureRow(
                "01", "Rikets styrelse", "0101001",
                "X", 2023, 100.0, None, 95.0,
                None, None,
            ),
            ExpenditureRow(
                "01", "Rikets styrelse", "0101001",
                "X", 2024, 110.0, None, 108.0,
                None, None,
            ),
        ]

        result = client.compare_budgets(2023, 2024)
        assert len(result) == 1
        assert result[0]["area_id"] == "01"
        assert result[0]["delta_msek"] == pytest.approx(
            13.0,
        )
        assert result[0]["delta_pct"] == pytest.approx(
            13.68, rel=0.01,
        )


class TestBiggestChanges:
    def _client_with_areas(self):
        client = StatskontoretClient()
        client._expenditure_data = [
            # "01" grows by 200 (biggest increase)
            ExpenditureRow(
                "01", "Rikets styrelse", "0101001",
                "X", 2023, 1000.0, None, 1000.0,
                None, None,
            ),
            ExpenditureRow(
                "01", "Rikets styrelse", "0101001",
                "X", 2024, 1200.0, None, 1200.0,
                None, None,
            ),
            # "02" grows by 50 (smaller increase)
            ExpenditureRow(
                "02", "Ekonomi", "0201001",
                "Y", 2023, 500.0, None, 500.0,
                None, None,
            ),
            ExpenditureRow(
                "02", "Ekonomi", "0201001",
                "Y", 2024, 550.0, None, 550.0,
                None, None,
            ),
            # "03" shrinks by 300 (biggest decrease)
            ExpenditureRow(
                "03", "Skatt", "0301001",
                "Z", 2023, 900.0, None, 900.0,
                None, None,
            ),
            ExpenditureRow(
                "03", "Skatt", "0301001",
                "Z", 2024, 600.0, None, 600.0,
                None, None,
            ),
            # "04" is new in 2024: baseline 0, so delta_pct must be
            # null rather than a misleading percentage.
            ExpenditureRow(
                "04", "Rattsvasendet", "0401001",
                "W", 2024, 80.0, None, 80.0,
                None, None,
            ),
        ]
        return client

    def test_increases_and_decreases_ranked(self):
        client = self._client_with_areas()
        result = client.get_biggest_changes(2023, 2024)

        inc_ids = [c["area_id"] for c in result["increases"]]
        assert inc_ids == ["01", "04", "02"]

        dec_ids = [c["area_id"] for c in result["decreases"]]
        assert dec_ids == ["03"]

    def test_top_n_bounds_each_list(self):
        client = self._client_with_areas()
        result = client.get_biggest_changes(
            2023, 2024, top_n=1,
        )
        assert len(result["increases"]) == 1
        assert result["increases"][0]["area_id"] == "01"
        assert len(result["decreases"]) == 1
        assert result["decreases"][0]["area_id"] == "03"

    def test_zero_baseline_gives_null_pct_not_misleading_value(
        self,
    ):
        client = self._client_with_areas()
        result = client.get_biggest_changes(2023, 2024)
        new_area = next(
            c
            for c in result["increases"]
            if c["area_id"] == "04"
        )
        assert new_area["delta_pct"] is None

    def test_ties_broken_deterministically_by_id(self):
        client = StatskontoretClient()
        client._expenditure_data = [
            ExpenditureRow(
                "02", "B", "X", "X", 2023, 100.0, None,
                100.0, None, None,
            ),
            ExpenditureRow(
                "02", "B", "X", "X", 2024, 150.0, None,
                150.0, None, None,
            ),
            ExpenditureRow(
                "01", "A", "Y", "Y", 2023, 100.0, None,
                100.0, None, None,
            ),
            ExpenditureRow(
                "01", "A", "Y", "Y", 2024, 150.0, None,
                150.0, None, None,
            ),
        ]
        result = client.get_biggest_changes(2023, 2024)
        assert [
            c["area_id"] for c in result["increases"]
        ] == ["01", "02"]

    def test_appropriation_level_within_area(self):
        client = StatskontoretClient()
        client._expenditure_data = [
            # continuing appropriation, grows
            ExpenditureRow(
                "06", "Forsvar", "0601001", "Forband",
                2023, 1000.0, None, 1000.0, None, None,
            ),
            ExpenditureRow(
                "06", "Forsvar", "0601001", "Forband",
                2024, 1300.0, None, 1300.0, None, None,
            ),
            # removed in 2024 (baseline 200 -> 0)
            ExpenditureRow(
                "06", "Forsvar", "0601002", "Gammal post",
                2023, 200.0, None, 200.0, None, None,
            ),
            # new in 2024 (baseline 0)
            ExpenditureRow(
                "06", "Forsvar", "0601003", "Ny post",
                2024, 150.0, None, 150.0, None, None,
            ),
            # a different area's row must never leak in
            ExpenditureRow(
                "01", "Rikets styrelse", "0101001", "Other",
                2024, 999.0, None, 999.0, None, None,
            ),
        ]
        result = client.get_biggest_changes(
            2023, 2024, area_id="06",
        )
        inc_ids = {
            c["appropriation_id"] for c in result["increases"]
        }
        dec_ids = {
            c["appropriation_id"] for c in result["decreases"]
        }
        assert inc_ids == {"0601001", "0601003"}
        assert dec_ids == {"0601002"}

        removed = next(
            c
            for c in result["decreases"]
            if c["appropriation_id"] == "0601002"
        )
        assert removed[f"outcome_{2024}_msek"] == 0.0
        assert removed["delta_msek"] == pytest.approx(-200.0)


class TestAvailableYears:
    def test_returns_sorted_years(self):
        client = StatskontoretClient()
        client._expenditure_data = [
            ExpenditureRow(
                "01", "X", "Y", "Z", 2022,
                None, None, None, None, None,
            ),
            ExpenditureRow(
                "01", "X", "Y", "Z", 2024,
                None, None, None, None, None,
            ),
        ]
        client._income_data = [
            IncomeRow(
                "1000", "X", "1100", "Y",
                "1111", "Z", 2023, None, None,
            ),
        ]
        assert client.get_available_years() == [
            2022, 2023, 2024,
        ]
