"""End-to-end robustness: damaged rows, page failures, retries, completion."""
import json

import pytest

import mockapp.main as mock_main
from buildpermit.config import SPEC
from buildpermit.framework import FetchError, run
from buildpermit.store import Store


@pytest.fixture
def con():
    connection = Store().connect()
    Store(connection).reset()
    yield connection
    Store(connection).reset()
    connection.close()


@pytest.fixture(autouse=True)
def clean_plans():
    mock_main._fail_plan.clear()
    mock_main._corrupt_plan.clear()
    mock_main._override.clear()
    mock_main._extra.clear()
    yield
    mock_main._fail_plan.clear()
    mock_main._corrupt_plan.clear()
    mock_main._override.clear()
    mock_main._extra.clear()


def _html(headers, rows):
    head = "".join("<th>%s</th>" % h for h in headers)
    body = "".join("<tr>" + "".join("<td>%s</td>" % c for c in row) + "</tr>"
                   for row in rows)
    return "<html><table><tr>%s</tr>%s</table></html>" % (head, body)


def test_mixed_good_and_damaged_rows_only_store_the_good_ones(con):
    good = ["P-1", "Alpha", "100", "active", "2030-01-01", "Build Co", "Watch Co"]
    broken = ["P-2", "Beta", "200"]           # missing cells
    good2 = ["P-3", "Gamma", "300", "active", "2030-01-02", "Build Co", "Watch Co"]
    mock_main._override[1] = {
        "status": 200,
        "body": _html(SPEC.columns, [good, broken, good2]),
        "media_type": "text/html",
    }
    result = run(con=con)
    stored = {r["id"] for r in Store(con).all_records()}
    assert stored == {"P-1", "P-3"}
    assert result["rejected"] == 1
    assert result["accepted"] == 2
    assert result["rejections"][0]["reason"] == "column_count_mismatch"
    assert result["complete"] is True


def test_damaged_table_layout_is_not_stored_at_all(con):
    headers = list(SPEC.columns) + ["施工单位"]  # duplicate construction unit
    row = ["P-1", "A", "1", "active", "2030-01-01", "B", "W", "B2"]
    mock_main._override[1] = {
        "status": 200, "body": _html(headers, [row]), "media_type": "text/html"}
    result = run(con=con)
    assert Store(con).count() == 0
    assert result["rejected"] >= 1
    assert result["ingested"] == 0


def test_transient_page_failure_is_retried_and_run_completes(con):
    mock_main._fail_plan[2] = 2  # two 503s, then success
    result = run(con=con)
    assert result["complete"] is True
    assert result["pages"] == result["pages_completed"] == 3
    assert result["count"] == 9
    assert result["http_attempts"] > result["pages"]


def test_persistent_page_failure_is_incomplete_and_pollutes_nothing(con):
    mock_main._fail_plan[2] = 99
    with pytest.raises(FetchError):
        run(con=con)
    assert Store(con).count() == 0
    assert Store(con).watermark() is None

    # Clearing the fault lets a later run complete cleanly.
    mock_main._fail_plan.clear()
    recovered = run(con=con)
    assert recovered["complete"] is True
    assert recovered["count"] == 9


def test_truncated_page_is_refetched_not_read_as_a_short_page(con):
    mock_main._corrupt_plan[1] = 1
    result = run(con=con)
    assert result["count"] == 9
    assert result["complete"] is True
    assert result["pages_completed"] == result["pages"]


def test_truncated_page_that_never_recovers_is_incomplete(con):
    mock_main._corrupt_plan[1] = 99
    with pytest.raises(FetchError):
        run(con=con)
    # A body that could not be read is not a page of "no more rows".
    assert Store(con).count() == 0
    assert Store(con).watermark() is None


def test_failed_run_does_not_advance_the_watermark(con):
    run(con=con)
    first_watermark = Store(con).watermark()
    assert first_watermark
    mock_main._fail_plan[2] = 99
    with pytest.raises(FetchError):
        run(mode="incremental", con=con)
    assert Store(con).watermark() == first_watermark


def test_rerun_is_idempotent(con):
    first = run(con=con)
    second = run(con=con)
    assert second["ingested"] == 0
    assert second["count"] == first["count"]
    assert second["complete"] is True


def test_full_and_incremental_runs_agree(con):
    full = run(mode="full", con=con)
    incremental = run(mode="incremental", con=con)
    assert incremental["count"] == full["count"]
    assert incremental["ingested"] == 0
    assert incremental["rejected"] == 0


def test_incremental_picks_up_a_new_row(con):
    first = run(con=con)
    mock_main._extra.append(__import__(
        "buildpermit.mockdata", fromlist=["extra_row"]).extra_row(1))
    second = run(mode="incremental", con=con)
    assert second["ingested"] == 1
    assert second["count"] == first["count"] + 1


def test_rejections_are_reported_with_provenance(con):
    good = ["P-1", "Alpha", "100", "active", "2030-01-01", "B", "W"]
    broken = ["P-2", "Beta"]
    mock_main._override[1] = {
        "status": 200, "body": _html(SPEC.columns, [good, broken]),
        "media_type": "text/html"}
    result = run(con=con)
    rejection = result["rejections"][0]
    assert rejection["values"] == broken
    assert rejection["format"] == "html"
    assert rejection["issues"]
    # The batch-level result still succeeds for the readable rows.
    assert result["accepted"] == 1


def test_stored_records_carry_field_provenance(con):
    run(con=con)
    for record in Store(con).all_records():
        sources = record["field_sources"]
        for field, entry in sources.items():
            assert "column" in entry and "raw_value" in entry
            assert entry["value"] == record[field]
        assert "contractor" in sources and "supervisor" in sources
        # The two parties were read from their own columns.
        assert "施工" in sources["contractor"]["column"] or \
            sources["contractor"]["column"] == "contractor"
        assert sources["supervisor"]["column"] == "supervisor"


def test_parties_never_interchange_in_stored_records(con):
    run(con=con)
    from buildpermit.mockdata import CONTRACTORS, SUPERVISORS
    for record in Store(con).all_records():
        assert record["contractor"] in CONTRACTORS
        assert record["supervisor"] in SUPERVISORS
        assert record["contractor"] != record["supervisor"]
        assert record["parties_distinct"] == 1


SETTLED_FIELDS = ("id", "name", "value", "status", "ts", "contractor", "supervisor")
BUSINESS_METADATA = ("field_sources", "unmapped", "_source", "row_index", "layout",
                     "valid", "rule", "missing_fields", "is_complete",
                     "parties_complete", "parties_distinct")


@pytest.mark.parametrize("fmt", ["html", "json"])
def test_html_and_json_produce_the_same_stored_identities(con, fmt, monkeypatch):
    import buildpermit.mockdata as mockdata

    monkeypatch.setattr(SPEC, "fmt", fmt)
    run(con=con)
    records = Store(con).all_records()
    keys = sorted(r[0] for r in con.execute("SELECT content_hash FROM records"))
    stored = sorted(
        json.dumps({k: v for k, v in record.items() if k not in BUSINESS_METADATA},
                   sort_keys=True, ensure_ascii=False)
        for record in records)
    expected = sorted(
        json.dumps({k: row[k] for k in SETTLED_FIELDS}, sort_keys=True,
                   ensure_ascii=False)
        for row in mockdata.build_rows()[:-1])  # last row is the exact duplicate
    assert len(keys) == len(set(keys)) == 9
    assert stored == expected


def test_a_damaged_page_does_not_stop_the_remaining_pages(con):
    good = ["P-1", "Alpha", "100", "active", "2030-01-01", "B", "W"]
    good2 = ["P-3", "Gamma", "300", "active", "2030-01-02", "B", "W"]
    broken = ["P-2", "Beta"]
    # A full page (4 cells-rows) in which one row is damaged: the damaged row
    # still counts towards the page being full, so pagination continues.
    mock_main._override[1] = {
        "status": 200,
        "body": _html(SPEC.columns, [good, broken, good2, good2]),
        "media_type": "text/html"}
    result = run(con=con)
    assert result["pages_completed"] == result["pages"] == 3
    assert result["complete"] is True
    assert result["rejected"] == 1
    # Page 1 contributed two distinct good permits; every later page was read.
    stored = {r["id"] for r in Store(con).all_records()}
    assert {"P-1", "P-3"} <= stored
    assert "P-2" not in stored
    assert len(stored) == 8
