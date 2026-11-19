
"""Real sqlite3 store: content-hash primary key, INSERT-OR-IGNORE dedup, watermark.

The content hash is computed over the record's *business* fields only
(see :mod:`buildpermit.identity`), so source-column spelling and the
interpretation metadata kept in the payload never change a record's identity.
The historical implementation hashed the whole enriched payload, so opening
such a database re-keys each row in place; the payload, the watermark and
every row are preserved and re-processing an unchanged permit still
de-duplicates.
"""
import json, sqlite3
from .config import DB_PATH
from .identity import content_hash as _identity_hash

class Store:
    def __init__(self, con=None):
        self.con = con
        if con is not None:
            self._init()

    def connect(self):
        con = sqlite3.connect(DB_PATH)
        self.__init__(con)
        return con

    def _init(self):
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS records("
            "content_hash TEXT PRIMARY KEY, id TEXT, ts TEXT, payload TEXT NOT NULL)")
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        self._migrate_legacy_keys()
        self.con.commit()

    def _migrate_legacy_keys(self):
        """Re-key historical rows onto the identity-only content hash.

        The original implementation hashed the *enriched* payload (including
        kernel-derived keys); identity now excludes that metadata.  Rows are
        re-keyed in place, which is idempotent: an already-migrated row keeps
        its key and no row is dropped or duplicated.
        """

        rows = self.con.execute(
            "SELECT content_hash, payload FROM records").fetchall()
        for old_key, payload in rows:
            try:
                record = json.loads(payload)
            except (TypeError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            new_key = self.hash(record)
            if new_key == old_key:
                continue
            self.con.execute("DELETE FROM records WHERE content_hash=?", (old_key,))
            self.con.execute(
                "INSERT OR IGNORE INTO records(content_hash,id,ts,payload) VALUES(?,?,?,?)",
                (new_key, str(record.get("id", "")),
                 str(record.get("ts", "")), payload))

    @staticmethod
    def hash(record):
        return _identity_hash(record)

    def upsert(self, record, identity=None):
        """Insert ``record`` unless an equal identity is already stored.

        ``identity`` may name the business-field subset used for the primary
        key (for example the raw source record before enrichment).  Without
        it, the record's own business fields are used.
        """

        key = self.hash(identity if identity is not None else record)
        cur = self.con.execute(
            "INSERT OR IGNORE INTO records(content_hash,id,ts,payload) VALUES(?,?,?,?)",
            (key, str(record.get("id", "")),
             str(record.get("ts", "")), json.dumps(record, ensure_ascii=False)))
        self.con.commit()
        return cur.rowcount > 0

    def count(self):
        return self.con.execute("SELECT COUNT(*) FROM records").fetchone()[0]

    def all_records(self):
        return [json.loads(p) for (_h, p) in
                self.con.execute("SELECT content_hash,payload FROM records")]

    def get(self, content_hash):
        row = self.con.execute(
            "SELECT payload FROM records WHERE content_hash=?", (content_hash,)).fetchone()
        return json.loads(row[0]) if row else None

    def watermark(self):
        row = self.con.execute("SELECT v FROM meta WHERE k='watermark'").fetchone()
        return row[0] if row else None

    def set_watermark(self, value):
        self.con.execute(
            "INSERT INTO meta(k,v) VALUES('watermark',?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(value),))
        self.con.commit()

    def reset(self):
        self.con.execute("DELETE FROM records")
        self.con.execute("DELETE FROM meta")
        self.con.commit()
