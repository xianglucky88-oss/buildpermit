"""Header interpretation, provenance and party separation."""
import json

import pytest

from buildpermit.config import SPEC
from buildpermit.fields import (
    build_header_map,
    normalize_header,
    resolve_header,
)
from buildpermit.framework import parse_page
from buildpermit.identity import content_hash, identity_payload
from buildpermit.mockdata import build_rows

GOOD = ["P-0001", "Alpha Site", "1200", "active", "2030-01-02",
        "Alpha Construction", "Omicron Supervision"]


def html_table(headers, rows):
    head = "".join("<th>%s</th>" % h for h in headers)
    body = "".join(
        "<tr>" + "".join("<td>%s</td>" % c for c in row) + "</tr>" for row in rows)
    return "<html><body><table><tr>%s</tr>%s</table></body></html>" % (head, body)


def business(record):
    return identity_payload(record)


# --------------------------------------------------------------------------- #
# Header normalisation / synonyms.
# --------------------------------------------------------------------------- #
def test_header_normalisation_folds_case_space_and_unit_suffix():
    assert normalize_header("  ID  ") == "id"
    assert normalize_header("ＩＤ") == normalize_header("id")  # full width
    assert normalize_header("合同金额（元）") == normalize_header("合同金额")
    assert normalize_header("Contract Value (CNY)") == normalize_header("contract value")


@pytest.mark.parametrize("header", [
    "施工单位", "施工企业", "承建单位", "承包商", "construction unit",
    "Construction Company", "CONTRACTOR", "main_contractor", " builder ",
])
def test_construction_unit_headers_resolve_to_contractor(header):
    assert resolve_header(header).canonical == "contractor"


@pytest.mark.parametrize("header", [
    "监理单位", "监理企业", "监理公司", "工程监理单位", "supervision unit",
    "Supervision Company", "supervisor", "SUPERVISOR UNIT",
    "supervising_company", "监理 单位",
])
def test_supervision_unit_headers_resolve_to_supervisor(header):
    assert resolve_header(header).canonical == "supervisor"


def test_the_two_party_headers_never_cross_map():
    for header in ("施工单位", "contractor", "承建单位"):
        assert resolve_header(header).canonical != "supervisor"
    for header in ("监理单位", "supervisor", "监理公司"):
        assert resolve_header(header).canonical != "contractor"


def test_header_mixing_both_parties_is_ambiguous_not_guessed():
    resolution = resolve_header("施工单位及监理单位")
    assert resolution.canonical is None
    assert resolution.method == "ambiguous"


def test_unknown_header_is_not_fuzzy_matched():
    resolution = resolve_header("remark")
    assert resolution.canonical is None
    assert resolution.method == "unknown"
    # A substring of a known synonym must not match either.
    assert resolve_header("子施工单位").canonical is None


def test_substring_of_another_fields_synonym_does_not_match():
    # "工程" is a synonym of ``name`` and also a substring of "工程监理单位"
    # (supervision).  A fuzzy matcher would let these collide; whole-name
    # matching must keep them apart.
    assert resolve_header("工程").canonical == "name"
    assert resolve_header("工程监理单位").canonical == "supervisor"
    assert resolve_header("工程金额").canonical == "value"
    # "ts" is a substring of "permitstatus"; it must not swallow it.
    assert resolve_header("ts").canonical == "ts"
    assert resolve_header("permitstatus").canonical == "status"


def test_owner_headers_are_neither_party():
    for header in ("建设单位", "业主", "developer", "owner"):
        resolution = resolve_header(header)
        assert resolution.canonical == "owner"


# --------------------------------------------------------------------------- #
# Re-ordered / renamed / re-cased tables.
# --------------------------------------------------------------------------- #
def test_reordered_and_renamed_columns_keep_the_same_values():
    renamed = ["编号", "项目名称", "Contract Amount", "状态", "TS",
               "施工单位", "监理单位"]
    row = [GOOD[0], GOOD[1], GOOD[2], GOOD[3], GOOD[4], GOOD[5], GOOD[6]]
    result = parse_page(html_table(renamed, [row]), "html")
    assert not result.rejected
    record = result.records[0]
    assert record["id"] == "P-0001"
    assert record["name"] == "Alpha Site"
    assert record["value"] == 1200
    assert record["contractor"] == "Alpha Construction"
    assert record["supervisor"] == "Omicron Supervision"


def test_column_order_does_not_change_identity():
    forward = parse_page(html_table(SPEC.columns, [GOOD]), "html").records[0]
    reversed_headers = list(reversed(SPEC.columns))
    reversed_row = list(reversed(GOOD))
    backward = parse_page(
        html_table(reversed_headers, [reversed_row]), "html").records[0]
    assert business(forward) == business(backward)
    assert content_hash(forward) == content_hash(backward)


def test_case_and_whitespace_variants_share_identity():
    variant_headers = ["  ID ", "NAME", " Value ", "status", "  TS  ",
                       "CONTRACTOR", " supervisor "]
    variant_row = list(GOOD)
    variant_row[1] = "  Alpha   Site  "
    variant = parse_page(html_table(variant_headers, [variant_row]), "html").records[0]
    baseline = parse_page(html_table(SPEC.columns, [GOOD]), "html").records[0]
    assert variant["name"] == "Alpha Site"
    assert content_hash(variant) == content_hash(baseline)


# --------------------------------------------------------------------------- #
# Provenance.
# --------------------------------------------------------------------------- #
def test_every_canonical_field_traces_back_to_its_column_and_raw_value():
    headers = ["编号", "项目名称", "合同金额（元）", "状态", "发证日期",
               "施工单位", "监理单位"]
    row = ["P-9", "Site", "1,234", "Active", "2030-05-01", "Builder Co", "Watch Co"]
    record = parse_page(html_table(headers, [row]), "html").records[0]
    sources = record["field_sources"]
    for field in ("id", "name", "value", "status", "ts", "contractor", "supervisor"):
        assert field in sources, field
        entry = sources[field]
        index = entry["column_index"]
        assert entry["column"] == headers[index]
        assert entry["raw_value"] == row[index]
        # The canonical value is derived from the raw value of that column.
        assert entry["value"] == record[field]
    assert sources["value"]["raw_value"] == "1,234"
    assert record["value"] == 1234
    assert sources["contractor"]["column"] == "施工单位"
    assert sources["supervisor"]["column"] == "监理单位"


def test_provenance_records_the_matching_method():
    record = parse_page(html_table(SPEC.columns, [GOOD]), "html").records[0]
    assert record["field_sources"]["id"]["method"] == "alias"
    assert record["field_sources"]["supervisor"]["column"] == "supervisor"


def test_unknown_columns_are_preserved_not_dropped():
    headers = list(SPEC.columns) + ["备注", "Remark"]
    row = list(GOOD) + ["keep me", "and me"]
    result = parse_page(html_table(headers, [row]), "html")
    assert not result.rejected
    record = result.records[0]
    assert record["unmapped"] == {"备注": "keep me", "Remark": "and me"}


def test_provenance_is_not_part_of_identity():
    record = parse_page(html_table(SPEC.columns, [GOOD]), "html").records[0]
    with_extra_metadata = dict(record, row_index=999)
    assert content_hash(record) == content_hash(with_extra_metadata)


# --------------------------------------------------------------------------- #
# HTML / JSON equivalence.
# --------------------------------------------------------------------------- #
def _json_items(rows):
    return json.dumps({"items": rows, "page": 1})


def test_html_and_json_of_the_same_permit_agree():
    rows = build_rows()
    html = html_table(SPEC.columns, [[r[c] for c in SPEC.columns] for r in rows])
    from_html = parse_page(html, "html")
    from_json = parse_page(_json_items(rows), "json")
    assert len(from_html.records) == len(from_json.records)
    assert not from_html.rejected and not from_json.rejected
    for left, right in zip(from_html.records, from_json.records):
        assert business(left) == business(right)
        assert content_hash(left) == content_hash(right)


def test_html_and_json_never_fabricate_from_damaged_rows():
    """Both formats refuse to invent data, each according to its own shape.

    HTML aligns by position, so a short row is unalignable and rejected.  JSON
    is keyed, so an absent key is recorded as *absent* (not empty-but-present)
    and the row is still interpretable -- but the absence stays visible.
    """
    rows = build_rows()
    good = [[r[c] for c in SPEC.columns] for r in rows]
    broken_row = good[0][:-1]
    left = parse_page(html_table(SPEC.columns, good + [broken_row]), "html")
    assert len(left.records) == len(rows)
    assert len(left.rejected) == 1
    assert left.rejected[0]["reason"] == "column_count_mismatch"

    right = parse_page(_json_items(
        [dict(r) for r in rows] +
        [{k: v for k, v in rows[0].items() if k != SPEC.columns[-1]}]), "json")
    assert len(right.records) == len(rows) + 1
    dropped = right.records[-1]
    assert dropped["field_sources"]["supervisor"]["present"] is False
    assert dropped["field_sources"]["supervisor"]["raw_value"] is None
    assert dropped["supervisor"] == ""


def test_json_item_that_is_not_an_object_is_a_page_error():
    with pytest.raises(ValueError):
        parse_page(json.dumps({"items": [["not", "an", "object"]]}), "json")


def test_html_and_json_agree_on_renamed_headers():
    headers = ["编号", "项目名称", "合同金额", "状态", "发证日期", "施工单位", "监理单位"]
    row = ["P-9", "Site", "1200", "active", "2030-05-01", "Build Co", "Watch Co"]
    html = parse_page(html_table(headers, [row]), "html").records[0]
    as_json = dict(zip(headers, row))
    js = parse_page(json.dumps({"items": [as_json]}), "json").records[0]
    assert business(html) == business(js)


# --------------------------------------------------------------------------- #
# Damaged layout: duplicate / unknown / missing / shifted.
# --------------------------------------------------------------------------- #
def test_duplicate_column_rejects_the_whole_table():
    headers = list(SPEC.columns) + ["施工单位"]
    row = list(GOOD) + ["Another Builder"]
    result = parse_page(html_table(headers, [row]), "html")
    assert result.records == []
    assert all(item["reason"] == "layout_invalid" for item in result.rejected)
    codes = {i["code"] for item in result.rejected for i in item["issues"]}
    assert "duplicate_column" in codes


def test_missing_required_column_rejects_the_whole_table():
    headers = [c for c in SPEC.columns if c != "status"]
    row = [v for c, v in zip(SPEC.columns, GOOD) if c != "status"]
    result = parse_page(html_table(headers, [row]), "html")
    assert result.records == []
    assert any(i["code"] == "missing_required_column"
               for item in result.rejected for i in item["issues"])


def test_unknown_column_alone_does_not_destroy_the_row():
    headers = list(SPEC.columns) + ["备注"]
    row = list(GOOD) + ["note"]
    result = parse_page(html_table(headers, [row]), "html")
    assert len(result.records) == 1
    assert result.records[0]["unmapped"] == {"备注": "note"}


def test_single_shifted_row_is_rejected_individually():
    rows = [list(GOOD), list(GOOD)[:3] + list(GOOD)[4:], list(GOOD)]
    result = parse_page(html_table(SPEC.columns, rows), "html")
    assert len(result.records) == 2
    assert len(result.rejected) == 1
    assert result.rejected[0]["reason"] == "column_count_mismatch"
    assert result.rejected[0]["issues"][0]["expected"] == len(SPEC.columns)


def test_a_damaged_row_does_not_contaminate_its_neighbours():
    damaged = ["P-2", "Damaged"]  # far too narrow
    rows = [list(GOOD), damaged, list(GOOD)]
    rows[0][0] = "P-1"
    rows[2][0] = "P-3"
    result = parse_page(html_table(SPEC.columns, rows), "html")
    assert [r["id"] for r in result.records] == ["P-1", "P-3"]
    assert len(result.rejected) == 1
    assert result.rejected[0]["values"] == damaged


def test_empty_row_is_rejected_not_stored():
    rows = [[""] * len(SPEC.columns), list(GOOD)]
    result = parse_page(html_table(SPEC.columns, rows), "html")
    assert len(result.records) == 1
    assert result.rejected[0]["reason"] == "empty_row"


def test_repeated_header_row_is_rejected():
    rows = [list(SPEC.columns), list(GOOD)]
    result = parse_page(html_table(SPEC.columns, rows), "html")
    assert len(result.records) == 1
    assert result.rejected[0]["reason"] == "repeated_header_row"


def test_ambiguous_column_is_reported():
    headers = list(SPEC.columns) + ["施工单位及监理单位"]
    row = list(GOOD) + ["Both"]
    result = parse_page(html_table(headers, [row]), "html")
    codes = {i["code"] for i in result.layout_issues}
    assert "ambiguous_column" in codes
    # The ambiguous column is not mapped to either party field.
    assert result.records[0]["field_sources"]["contractor"]["column"] == "contractor"
    assert result.records[0]["field_sources"]["supervisor"]["column"] == "supervisor"


def test_header_map_reports_duplicate_indexes():
    hmap = build_header_map(["id", "施工单位", "承建单位"])
    assert hmap.duplicates["contractor"] == [1, 2]
    assert "contractor" not in hmap.mapping


# --------------------------------------------------------------------------- #
# Party integrity through the kernel.
# --------------------------------------------------------------------------- #
def test_kernel_keeps_the_two_parties_separate():
    from buildpermit import kernel
    record = {"id": "1", "name": "n", "value": 1, "status": "active",
              "contractor": "Builder", "supervisor": "Watcher"}
    result = kernel.process(dict(record))
    assert result["parties_complete"] == 1
    assert result["parties_distinct"] == 1


def test_kernel_flags_identical_parties():
    from buildpermit import kernel
    record = {"id": "1", "name": "n", "value": 1, "status": "active",
              "contractor": "Same Co", "supervisor": "Same Co"}
    assert kernel.process(dict(record))["parties_distinct"] == 0


def test_kernel_does_not_claim_party_status_without_data():
    from buildpermit import kernel
    record = {"id": "1", "name": "n", "value": 1, "status": "active"}
    result = kernel.process(dict(record))
    assert "parties_complete" not in result
    assert "parties_distinct" not in result
