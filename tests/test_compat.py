"""Store compatibility: identity-only hashing, legacy rows, idempotent writes."""
import hashlib
import json
import sqlite3

import pytest

import buildpermit.store as store_module
from buildpermit.framework import parse_page
from buildpermit.identity import content_hash
from buildpermit.store import Store


def _legacy_hash(payload):
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE records(content_hash TEXT PRIMARY KEY, "
                "id TEXT, ts TEXT, payload TEXT NOT NULL)")
    raw.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
    payload = {"id": "BUI-0001", "name": "Alpha Widget", "value": 16223699,
               "status": "new", "ts": "2030-01-02T00:00:00Z",
               "contractor": "Alpha Construction",
               "missing_fields": "", "is_complete": 1}
    raw.execute("INSERT INTO records VALUES(?,?,?,?)",
                (_legacy_hash(payload), "BUI-0001", payload["ts"],
                 json.dumps(payload)))
    raw.execute("INSERT INTO meta VALUES('watermark','2029-06-01')")
    raw.commit()
    raw.close()
    monkeypatch.setattr(store_module, "DB_PATH", path)
    return payload


def test_legacy_rows_are_rekeyed_without_loss(legacy_db):
    con = Store().connect()
    store = Store(con)
    try:
        assert store.count() == 1
        assert store.watermark() == "2029-06-01"
        stored = store.all_records()[0]
        # The payload is untouched -- only its key moved.
        assert stored["name"] == "Alpha Widget"
        (key,) = [r[0] for r in con.execute("SELECT content_hash FROM records")]
        assert key == content_hash(legacy_db)
    finally:
        con.close()


def test_reopening_a_migrated_database_is_idempotent(legacy_db):
    con = Store().connect()
    first = [r[0] for r in con.execute("SELECT content_hash FROM records")]
    Store(con)  # re-run migration
    second = [r[0] for r in con.execute("SELECT content_hash FROM records")]
    assert first == second
    assert Store(con).count() == 1
    con.close()


def test_an_unchanged_permit_is_a_duplicate_of_a_legacy_row(legacy_db):
    con = Store().connect()
    store = Store(con)
    try:
        html = ("<table><tr><th>id</th><th>name</th><th>value</th><th>status</th>"
                "<th>ts</th><th>contractor</th></tr><tr><td>BUI-0001</td>"
                "<td>Alpha Widget</td><td>16223699</td><td>new</td>"
                "<td>2030-01-02T00:00:00Z</td>"
                "<td>Alpha Construction</td></tr></table>")
        record = parse_page(html, "html").records[0]
        assert store.upsert(record, identity=record) is False
        assert store.count() == 1
    finally:
        con.close()


def test_identity_ignores_interpretation_metadata():
    first = {"id": "1", "name": "A", "value": 10, "status": "active",
             "ts": "t", "contractor": "B", "supervisor": "S"}
    second = dict(first, field_sources={"id": {"column": "编号"}},
                  unmapped={"备注": "x"}, row_index=5, layout="json",
                  missing_fields="", is_complete=1)
    assert content_hash(first) == content_hash(second)


def test_identity_reflects_a_real_business_change():
    first = {"id": "1", "name": "A", "value": 10, "status": "active",
             "ts": "t", "contractor": "B", "supervisor": "S"}
    assert content_hash(first) != content_hash(dict(first, contractor="Other"))
    assert content_hash(first) != content_hash(dict(first, supervisor="Other"))


def test_upsert_accepts_an_explicit_identity():
    con = Store().connect()
    store = Store(con)
    try:
        store.reset()
        source = {"id": "q1", "ts": "t1", "value": 5}
        enriched = dict(source, rule="permit-fields-v2", is_complete=1)
        assert store.upsert(enriched, identity=source) is True
        # Re-delivering the same source under a new derived label is a no-op.
        relabelled = dict(source, rule="permit-fields-v3", is_complete=0)
        assert store.upsert(relabelled, identity=source) is False
        assert store.count() == 1
        assert store.get(store.hash(source))["rule"] == "permit-fields-v2"
    finally:
        store.reset()
        con.close()
