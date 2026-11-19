
"""Build deterministic source rows for the local fixture.

Both the construction unit (``contractor``) and the supervision unit
(``supervisor``) are published in the same permit table.  They are generated
from *different* name pools so a fixture can always prove the two were not
interchanged.
"""
import hashlib
from datetime import datetime, timedelta, timezone
from .config import SPEC

NAMES = ["Alpha Widget", "Beta Gadget", "Gamma Set", "Delta Unit"]
STATUS = ["new", "active", "pending", "sold", "withdrawn"]
CONTRACTORS = ["Alpha Construction", "Beta Builders", "Gamma Civil",
               "Delta Engineering"]
SUPERVISORS = ["Omicron Supervision", "Pi Consulting", "Rho Oversight",
               "Sigma Control"]
SOURCE_START = datetime(2030, 1, 1, tzinfo=timezone.utc)

def _h(salt, i):
    return int(hashlib.md5(("%s-%s-%d" % (SPEC.slug, salt, i)).encode()).hexdigest()[:6], 16)

def build_rows():
    cols = SPEC.columns
    ps = SPEC.page_size
    n = ps * 2 + 1
    rows = []
    for i in range(1, n + 1):
        d = {}
        for c in cols:
            if c == "id":
                d[c] = "%s-%04d" % (SPEC.slug[:3].upper(), i)
            elif c == "name":
                d[c] = NAMES[(i - 1) % len(NAMES)]
            elif c == "value":
                d[c] = _h("v", i)
            elif c == "status":
                d[c] = STATUS[(i - 1) % len(STATUS)]
            elif c == "ts":
                d[c] = (SOURCE_START + timedelta(days=i % 5)).isoformat().replace("+00:00", "Z")
            elif c == "contractor":
                d[c] = CONTRACTORS[(i - 1) % len(CONTRACTORS)]
            elif c == "supervisor":
                d[c] = SUPERVISORS[(i - 1) % len(SUPERVISORS)]
            else:
                d[c] = "%s_%d" % (c, _h(c, i) % 97)
        rows.append(d)
    rows.append(dict(rows[0]))
    return rows

def extra_row(k):
    d = {}
    for c in SPEC.columns:
        if c == "id":
            d[c] = "%s-X%04d" % (SPEC.slug[:3].upper(), k)
        elif c == "ts":
            d[c] = (SOURCE_START + timedelta(days=30 + k)).isoformat().replace("+00:00", "Z")
        elif c == "name":
            d[c] = NAMES[k % len(NAMES)]
        elif c == "value":
            d[c] = _h("x", k)
        elif c == "status":
            d[c] = "active"
        elif c == "contractor":
            d[c] = CONTRACTORS[k % len(CONTRACTORS)]
        elif c == "supervisor":
            d[c] = SUPERVISORS[k % len(SUPERVISORS)]
        else:
            d[c] = "%s_new_%d" % (c, k)
    return d
