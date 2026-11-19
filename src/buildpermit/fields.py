"""Interpretation of published permit tables into one canonical record.

A single permit table can publish both the construction unit and the
supervision unit, and the publisher may rename, re-order, re-case or re-space
its columns at any time.  This module is the single place that decides which
source column means which canonical field, and it records that decision
together with the raw value for every field.

Guarantees:

* header matching is by normalised *whole* name (NFKC, whitespace, case,
  punctuation and a trailing unit annotation are folded) against an explicit
  synonym table, never by substring -- so a construction-unit column can never
  silently become a supervision-unit column (or the reverse);
* a header that mixes both party concepts is reported as ambiguous and left
  unmapped instead of being guessed;
* duplicate columns for one canonical field, missing required columns, and
  wide/narrow rows are reported and their rows are rejected -- they never
  become "reliable" data;
* unknown columns are preserved, not dropped, and every canonical field
  carries ``source_column`` / ``column_index`` / ``raw_value`` provenance.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field as dc_field
from decimal import Decimal, InvalidOperation
from typing import Any

from .config import SPEC

# Fields a record is interpreted into.  The first four are the historical
# required fields used by the field-completeness kernel; the rest are optional
# business fields of a construction permit.
CANONICAL_FIELDS = ("id", "name", "value", "status", "ts",
                    "contractor", "supervisor", "owner")
REQUIRED_FIELDS = ("id", "name", "value", "status")
PARTY_FIELDS = ("contractor", "supervisor")

# Header tokens used only to *detect ambiguity*.  They are never used to
# fuzzy-match a header, because that is exactly how the two parties could be
# swapped when a publisher writes e.g. "施工单位及监理单位".
CONTRACTOR_TOKENS = ("施工", "承建", "承包", "contractor", "construction",
                     "builder")
SUPERVISOR_TOKENS = ("监理", "supervis", "monitor")


# --------------------------------------------------------------------------- #
# Header normalisation and synonym table.
# --------------------------------------------------------------------------- #
_BRACKETED_TAIL = re.compile(r"[\(\[（【][^\)\]）】]*[\)\]）】]\s*$")
_WHITESPACE = re.compile(r"\s+")
_HAS_CJK = re.compile(r"[㐀-鿿]")
_SEPARATORS = str.maketrans({"-": "_", "—": "_", "–": "_", "·": "",
                             "：": ":", "／": "/"})


def normalize_header(value) -> str:
    """Fold a source header into the lookup key used by the synonym table."""

    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = text.replace("　", " ").replace("​", "")
    text = text.replace("（", "(").replace("）", ")")
    text = text.replace("【", "[").replace("】", "]")
    text = text.translate(_SEPARATORS)
    text = text.strip().lower()
    text = _BRACKETED_TAIL.sub("", text)
    text = _WHITESPACE.sub("", text)
    # A trailing "名称"/"全称" annotation does not change the meaning.
    for suffix in ("名称", "全称", "name"):
        if text.endswith(suffix) and len(text) > len(suffix):
            stripped = text[: -len(suffix)]
            if stripped in _SYNONYMS:
                text = stripped
                break
    return text


_ALIAS_GROUPS = {
    # Permit identifier.
    "id": (
        "id", "编号", "许可证编号", "许可编号", "许可证号", "许可证书编号",
        "证书编号", "证号", "许可证号码", "许可文号", "文号", "回执编号",
        "record_id", "recordid", "permit_id", "permitid", "permit_no",
        "permitno", "permit_number", "permitnumber", "license_no",
        "licenseno", "lic_no", "licno", "licence_no", "certificate_no",
        "certificateno", "doc_id", "docid", "business_id", "businessid",
        "許可證編號", "許可編號", "證號", "編號",
    ),
    # Project / permit display name.
    "name": (
        "name", "名称", "工程名称", "项目名称", "项目名", "工程名", "工程",
        "项目", "title", "project", "project_name", "projectname",
        "project_title", "projecttitle", "item_name", "itemname",
        "permit_name", "permitname", "subject", "工程名稱", "項目名稱",
        "名稱",
    ),
    # Monetary value.
    "value": (
        "value", "金额", "合同价", "合同金额", "合同价款", "合同造价", "造价",
        "工程金额", "工程总价", "许可金额", "价款", "总价", "总金额",
        "amount", "price", "total", "total_amount", "totalamount",
        "contract_value", "contractvalue", "contract_amount",
        "contractamount", "project_value", "projectvalue", "cost",
        "金額", "合同金額", "合同價",
    ),
    # Permit status.
    "status": (
        "status", "状态", "许可状态", "当前状态", "办理状态", "审批状态",
        "证照状态", "state", "permit_status", "permitstatus", "record_state",
        "recordstate", "current_status", "currentstatus", "狀態", "許可狀態",
    ),
    # Timestamp used by the incremental watermark.
    "ts": (
        "ts", "时间", "日期", "发证日期", "签发日期", "许可日期", "办理日期",
        "公示日期", "更新日期", "更新时间", "发布日期", "受理日期",
        "timestamp", "time", "date", "issued_at", "issuedat", "issue_date",
        "issuedate", "issued_on", "issuedon", "grant_date", "grantdate",
        "updated_at", "updatedat", "update_time", "updatetime",
        "publish_date", "publishdate", "published_at", "publishedat",
        "created_at", "createdat", "時間", "日期", "發證日期",
    ),
    # Construction unit: the entity that builds.
    "contractor": (
        "contractor", "施工单位", "施工企业", "施工方", "施工公司",
        "施工承包商", "施工承包单位", "施工总承包单位", "承建单位",
        "承建企业", "承建方", "承包商", "承包单位", "承包人",
        "construction_unit", "constructionunit", "construction_company",
        "constructioncompany", "construction_contractor",
        "constructioncontractor", "builder", "build_unit", "buildunit",
        "building_unit", "buildingunit", "main_contractor",
        "maincontractor", "general_contractor", "generalcontractor",
        "施工單位", "施工企業", "承建單位", "承包商", "承包人",
    ),
    # Supervision unit: the entity that supervises.  Kept strictly separate
    # from the construction unit above.
    "supervisor": (
        "supervisor", "supervision_unit", "supervisionunit",
        "supervision_company", "supervisioncompany", "supervising_unit",
        "supervisingunit", "supervising_company", "supervisingcompany",
        "supervision", "supervisor_unit", "supervisorunit",
        "monitor_unit", "monitorunit", "监理单位", "监理企业", "监理公司",
        "监理方", "监理机构", "监理", "工程监理单位", "监理單位",
        "監理單位", "監理企業",
    ),
    # Owner / developer.  A third party that must never be mistaken for
    # either of the two parties above.
    "owner": (
        "owner", "owner_unit", "ownerunit", "construction_owner",
        "constructionowner", "developer", "proprietor", "employer",
        "建设单位", "业主", "业主单位", "发包人", "发包单位", "开发单位",
        "建设单位名称", "建設單位", "業主",
    ),
}

# Flat lookup, built once.  A synonym may never belong to two fields.
_SYNONYMS: dict[str, str] = {}
for _field, _names in _ALIAS_GROUPS.items():
    for _name in _names:
        _key = normalize_header(_name)
        if not _key:
            continue
        if _key in _SYNONYMS and _SYNONYMS[_key] != _field:
            raise RuntimeError(
                "synonym %r maps to both %r and %r" % (_key, _SYNONYMS[_key], _field)
            )
        _SYNONYMS[_key] = _field


def _contains(text: str, tokens) -> bool:
    return any(token in text for token in tokens)


@dataclass(frozen=True)
class Resolution:
    canonical: str | None
    method: str          # "alias" | "ambiguous" | "unknown" | "empty"
    normalized: str = ""
    reason: str = ""


def resolve_header(header) -> Resolution:
    """Resolve one raw header to a canonical field *without guessing*."""

    key = normalize_header(header)
    if not key:
        return Resolution(None, "empty", key, "empty header")
    if key in _SYNONYMS:
        return Resolution(_SYNONYMS[key], "alias", key)
    # A CJK header that separates words with a dash/colon is the same name.
    # Only an *exact* collapsed match is accepted, and only when the source
    # spelling contains CJK, so ``construction_unit`` is never altered.
    if _HAS_CJK.search(key):
        collapsed = key.replace("_", "").replace(":", "")
        if collapsed in _SYNONYMS:
            return Resolution(_SYNONYMS[collapsed], "alias", collapsed)
    # Not an exact synonym: it may not be mapped, but if it mixes both party
    # concepts we can at least say why.
    if _contains(key, CONTRACTOR_TOKENS) and _contains(key, SUPERVISOR_TOKENS):
        return Resolution(None, "ambiguous", key,
                          "header mixes construction-unit and supervision-unit concepts")
    return Resolution(None, "unknown", key)


# --------------------------------------------------------------------------- #
# Value coercion (identical for every source format).
# --------------------------------------------------------------------------- #
_THOUSANDS = re.compile(r"^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$")
_CURRENCY_PREFIX = re.compile(r"^[¥￥$]\s*")


def _to_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        # Keep a stable spelling for integral JSON numbers, so the same permit
        # in JSON and HTML produces the same value.
        return str(int(value))
    return str(value)


def _to_number(value) -> int | float | str:
    """Coerce a value to a number where it is unambiguous, else keep it.

    Returning the source spelling without raising keeps unusable input
    visible and distinguishable instead of inventing a value.
    """

    if value is None:
        return ""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        return int(value) if value.is_integer() else value
    text = str(value).strip()
    if not text:
        return ""
    text = _CURRENCY_PREFIX.sub("", text)
    if _THOUSANDS.match(text):
        text = text.replace(",", "")
    try:
        return int(text)
    except ValueError:
        pass
    try:
        decimal = Decimal(text)
    except (InvalidOperation, ValueError):
        return text
    if not decimal.is_finite():
        return str(value)
    if decimal == decimal.to_integral_value():
        return int(decimal)
    return float(decimal)


_WHITESPACE = re.compile(r"\s+")
# Fields whose text content is a human label rather than an exact expression.
_LABEL_FIELDS = {"name", "status", "contractor", "supervisor", "owner"}
# Fields whose value is an identifier: only surrounding space is removed.
_EXACT_FIELDS = {"id", "ts"}


def _normalize_label(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def coerce_value(canonical: str, value) -> Any:
    if canonical in SPEC.int_fields:
        return _to_number(value)
    text = _to_text(value)
    if canonical in _LABEL_FIELDS:
        # HTML collapses source whitespace differently from JSON; folding it
        # here makes the same permit read identically from either format.
        return _normalize_label(text)
    if canonical in _EXACT_FIELDS:
        return text.strip()
    return text


# --------------------------------------------------------------------------- #
# Header map.
# --------------------------------------------------------------------------- #
@dataclass
class HeaderMap:
    raw_headers: list
    mapping: dict[str, int] = dc_field(default_factory=dict)
    methods: dict[str, str] = dc_field(default_factory=dict)
    duplicates: dict[str, list[int]] = dc_field(default_factory=dict)
    unknown: list[dict] = dc_field(default_factory=list)
    ambiguous: list[dict] = dc_field(default_factory=list)
    empty: list[dict] = dc_field(default_factory=list)
    missing_required: list[str] = dc_field(default_factory=list)
    missing_expected: list[str] = dc_field(default_factory=list)
    issues: list[dict] = dc_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.duplicates and not self.missing_required

    def source_column(self, canonical: str) -> str:
        index = self.mapping[canonical]
        return "" if index >= len(self.raw_headers) else str(self.raw_headers[index])


@dataclass(frozen=True)
class HeaderMapOptions:
    """How a missing header may be recovered.

    ``positional_fallback`` is only used by the legacy tab-separated format,
    which historically had no header row at all.  HTML and JSON are always
    header-driven, so a renamed or re-ordered column can never be silently
    mapped by position there.
    """

    positional_fallback: bool = False
    fallback_columns: tuple = ()


def build_header_map(raw_headers, options: HeaderMapOptions | None = None) -> HeaderMap:
    options = options or HeaderMapOptions()
    hmap = HeaderMap(raw_headers=list(raw_headers))
    for index, header in enumerate(hmap.raw_headers):
        resolution = resolve_header(header)
        if resolution.method == "empty":
            hmap.empty.append({"index": index, "header": header})
            hmap.issues.append({"code": "empty_column_header", "index": index})
            continue
        if resolution.method == "ambiguous":
            hmap.ambiguous.append({"index": index, "header": header,
                                   "reason": resolution.reason})
            hmap.issues.append({"code": "ambiguous_column",
                                "index": index, "header": header,
                                "reason": resolution.reason})
            continue
        if resolution.canonical is None:
            hmap.unknown.append({"index": index, "header": header,
                                 "normalized": resolution.normalized})
            hmap.issues.append({"code": "unknown_column",
                                "index": index, "header": header})
            continue
        if resolution.canonical in hmap.mapping:
            first = hmap.mapping.pop(resolution.canonical)
            hmap.methods.pop(resolution.canonical, None)
            hmap.duplicates[resolution.canonical] = [first, index]
            hmap.issues.append({"code": "duplicate_column",
                                "field": resolution.canonical,
                                "indexes": hmap.duplicates[resolution.canonical]})
            continue
        if resolution.canonical in hmap.duplicates:
            # A third column for the same field: record it, never pick one.
            hmap.duplicates[resolution.canonical].append(index)
            hmap.issues.append({"code": "duplicate_column",
                                "field": resolution.canonical,
                                "indexes": hmap.duplicates[resolution.canonical]})
            continue
        hmap.mapping[resolution.canonical] = index
        hmap.methods[resolution.canonical] = "alias"

    if options.positional_fallback and len(hmap.raw_headers) == len(options.fallback_columns):
        for index, canonical in enumerate(options.fallback_columns):
            if canonical in hmap.mapping:
                continue
            if index in {entry["index"] for entry in hmap.unknown + hmap.empty}:
                hmap.mapping[canonical] = index
                hmap.methods[canonical] = "positional"

    hmap.missing_required = [f for f in REQUIRED_FIELDS if f not in hmap.mapping]
    if hmap.missing_required:
        hmap.issues.append({"code": "missing_required_column",
                            "fields": hmap.missing_required})
    hmap.missing_expected = [f for f in SPEC.columns if f not in hmap.mapping]
    return hmap


# --------------------------------------------------------------------------- #
# Row / table interpretation.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Cell:
    """One cell of a source row.

    ``present`` distinguishes "the source did not provide this column for this
    row" (e.g. a missing JSON key) from "the source provided an empty value";
    both coerce to an empty value but the provenance stays honest.
    """

    raw: Any = None
    present: bool = True


@dataclass
class Interpretation:
    records: list[dict] = dc_field(default_factory=list)
    rejected: list[dict] = dc_field(default_factory=list)
    headers: list = dc_field(default_factory=list)
    layout_issues: list[dict] = dc_field(default_factory=list)
    raw_row_count: int = 0
    format: str = ""
    ok: bool = True

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)


def _rejection(reason, issues, cells, hmap, fmt, row_index) -> dict:
    return {
        "reason": reason,
        "issues": issues,
        "row_index": row_index,
        "format": fmt,
        "headers": list(hmap.raw_headers),
        "values": [cell.raw for cell in cells],
    }


def interpret_rows(headers, rows, fmt, row_indexes=None,
                   options: HeaderMapOptions | None = None) -> Interpretation:
    """Interpret aligned source rows using ``headers``.

    ``rows`` is a sequence of cell sequences (``Cell`` or raw values).  Each
    row is interpreted independently: one damaged row is rejected without
    affecting its neighbours.
    """

    hmap = build_header_map(headers, options)
    result = Interpretation(headers=list(headers), format=fmt,
                            raw_row_count=len(rows), ok=hmap.ok,
                            layout_issues=list(hmap.issues))
    width = len(hmap.raw_headers)

    for position, row in enumerate(rows):
        row_index = row_indexes[position] if row_indexes is not None else position
        cells = [c if isinstance(c, Cell) else Cell(c) for c in row]

        if not hmap.ok:
            # The table layout itself is not trustworthy: no row of it may be
            # presented as reliable data.
            result.rejected.append(_rejection(
                "layout_invalid", list(hmap.issues), cells, hmap, fmt, row_index))
            continue

        if len(cells) != width:
            result.rejected.append(_rejection(
                "column_count_mismatch",
                [{"code": "column_count_mismatch", "expected": width,
                  "actual": len(cells)}],
                cells, hmap, fmt, row_index))
            continue

        if all(not str(cell.raw).strip() for cell in cells if cell.present):
            result.rejected.append(_rejection(
                "empty_row", [{"code": "empty_row"}], cells, hmap, fmt, row_index))
            continue

        # A repeated header row inside the data is not a permit.
        if all(cell.present and normalize_header(cell.raw) == normalize_header(
                hmap.raw_headers[i]) for i, cell in enumerate(cells)):
            result.rejected.append(_rejection(
                "repeated_header_row", [{"code": "repeated_header_row"}],
                cells, hmap, fmt, row_index))
            continue

        record: dict[str, Any] = {}
        field_sources: dict[str, dict] = {}
        for canonical in CANONICAL_FIELDS:
            if canonical not in hmap.mapping:
                continue
            index = hmap.mapping[canonical]
            cell = cells[index]
            value = coerce_value(canonical, cell.raw)
            record[canonical] = value
            field_sources[canonical] = {
                "column": hmap.source_column(canonical),
                "column_index": index,
                "normalized_column": normalize_header(hmap.raw_headers[index]),
                "raw_value": cell.raw,
                "value": value,
                "method": hmap.methods.get(canonical, "alias"),
                "present": bool(cell.present),
                "format": fmt,
            }

        unmapped: dict[str, Any] = {}
        for entry in hmap.unknown + hmap.empty:
            index = entry["index"]
            cell = cells[index]
            key = (str(hmap.raw_headers[index]).strip()
                   or "#%d" % index)
            if key in unmapped:
                key = "%s#%d" % (key, index)
            unmapped[key] = cell.raw

        record["field_sources"] = field_sources
        record["unmapped"] = unmapped
        record["row_index"] = row_index
        record["layout"] = fmt
        record["valid"] = True
        record["_source"] = {
            "format": fmt,
            "row_index": row_index,
            "headers": [str(h) for h in hmap.raw_headers],
            "values": [cell.raw for cell in cells],
        }
        result.records.append(record)

    return result
