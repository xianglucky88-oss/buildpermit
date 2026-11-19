"""Record identity that is independent of how a record was published.

The business fields of a permit identify it.  Interpretation metadata -- the
source column names, the raw cell values kept for traceability, the layout that
produced the record and the position of the row on the page -- is deliberately
excluded from the identity.  Consequently the *same* permit expressed with a
different column order, different header spelling, or as HTML instead of JSON
keeps one identity and is de-duplicated correctly.

Keys starting with ``_`` and the names in :data:`NON_IDENTITY_KEYS` are
metadata.  Everything else is a business field.
"""

from __future__ import annotations

import hashlib
import json

# Metadata keys that never participate in a record's identity.
NON_IDENTITY_KEYS = frozenset({
    # Interpretation / provenance written by the reader.
    "field_sources",
    "provenance",
    "unmapped",
    "row_issues",
    "row_index",
    "layout",
    "mapping",
    "valid",
    "issues",
    # Derived by the field-completeness kernel; recomputed on every run and
    # therefore not part of what the permit *is*.
    "missing_fields",
    "is_complete",
    "parties_complete",
    "parties_distinct",
    "rule",
    "error",
    "flags",
    "quality",
})


def is_metadata_key(key) -> bool:
    """Return True for keys that describe how a record was read."""

    if not isinstance(key, str):
        return False
    return key.startswith("_") or key in NON_IDENTITY_KEYS


def identity_payload(record: dict) -> dict:
    """Return only the business fields of ``record``."""

    return {k: v for k, v in record.items() if not is_metadata_key(k)}


def canonical(record: dict) -> str:
    """Canonical JSON of the business fields (stable across runs)."""

    return json.dumps(identity_payload(record), sort_keys=True, ensure_ascii=False)


def content_hash(record: dict) -> str:
    """Short content fingerprint of the business fields of ``record``."""

    return hashlib.sha256(canonical(record).encode()).hexdigest()[:24]
