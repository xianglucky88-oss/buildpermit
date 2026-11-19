
"""Permit field completeness and party-integrity rules.

The kernel is deliberately pure: it derives values from the record it is
given and never reads the source table.  The source column of every input
field is available in ``record["field_sources"]`` when a caller wants to
explain why a field is present.

Guarantees:

* the construction unit and the supervision unit are reported separately and
  are never interchanged: each is only read from its own canonical field;
* a record whose two party fields carry the same entity is flagged, because
  one of the two columns was almost certainly read into the wrong field;
* a record with no usable party information is distinguished from a record
  whose parties are simply missing.
"""
from .fields import PARTY_FIELDS, REQUIRED_FIELDS

REQUIRED = list(REQUIRED_FIELDS)
RULE_VERSION = "permit-fields-v2"


def _present(value):
    return value not in ("", None)


def process(record):
    """Return derived flags for one interpreted permit record."""

    if not isinstance(record, dict):
        record = {}
    missing = [c for c in REQUIRED if not _present(record.get(c))]
    result = {
        "missing_fields": ",".join(missing),
        "is_complete": int(not missing),
        "rule": RULE_VERSION,
    }

    contractor = record.get("contractor")
    supervisor = record.get("supervisor")
    if PARTY_FIELDS[0] in record or PARTY_FIELDS[1] in record:
        result["parties_complete"] = int(
            _present(contractor) and _present(supervisor))
        # Only compare when both are actually published: two absent parties
        # are not "the same party".
        result["parties_distinct"] = (
            None if not (_present(contractor) and _present(supervisor))
            else int(str(contractor).strip() != str(supervisor).strip()))
    return result
