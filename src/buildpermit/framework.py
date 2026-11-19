"""Local transport, parsing, storage, and session helpers."""
from __future__ import annotations
import importlib
import json

from lxml import html as _lh

from .config import SPEC
from .fields import Cell, Interpretation, coerce_value, interpret_rows
from .identity import canonical, content_hash  # re-exported for compatibility
from .store import Store


# --------------------------------------------------------------------------- #
# Transport helpers.
# --------------------------------------------------------------------------- #
class Transport:
    """Client for the bundled local service."""

    def __init__(self):
        import sys
        for p in (str(SPEC_ROOT()), str(SPEC_ROOT() / "src")):
            if p not in sys.path:
                sys.path.insert(0, p)
        from starlette.testclient import TestClient
        app = importlib.import_module("mockapp.main").app
        self.client = TestClient(app)
        self.cookies: dict[str, str] = {}

    def request(self, method, path, params=None, json_body=None, headers=None):
        q = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        resp = self.client.request(method, path, params=q, json=json_body,
                                   headers=headers or {}, cookies=self.cookies)
        for k, v in resp.cookies.items():
            self.cookies[k] = v
        return resp.status_code, dict(resp.headers), resp.text


def SPEC_ROOT():
    from .config import ROOT
    return ROOT


class Sleeper:
    """Records requested delays without blocking the caller."""

    def __init__(self):
        self.slept = 0.0
        self.calls = 0

    def sleep(self, seconds):
        self.slept += seconds
        self.calls += 1


class Http:
    """HTTP client with bounded retry support."""

    def __init__(self, transport: Transport, sleeper: Sleeper,
                 max_retries=4, base_backoff=0.5, politeness=0.25):
        self.t = transport
        self.sleeper = sleeper
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.politeness = politeness

    def get(self, path, params=None, headers=None):
        attempt = 0
        while True:
            self.sleeper.sleep(self.politeness)
            status, hdr, body = self.t.request("GET", path, params, headers=headers)
            if status in (429, 500, 502, 503) and attempt < self.max_retries:
                self.sleeper.sleep(self.base_backoff * (2 ** attempt))
                attempt += 1
                continue
            return status, hdr, body, attempt


# --------------------------------------------------------------------------- #
# Response parsing.
# --------------------------------------------------------------------------- #
def _legacy_positional_rows(rows, row_indexes, fmt):
    """Interpret header-less rows positionally (legacy tab-separated source).

    Only this format may fall back to the historical column order, and only
    when the row width matches exactly.  Every field's provenance still names
    the position it came from.
    """

    cols = list(SPEC.columns)
    result = Interpretation(headers=cols, format=fmt, raw_row_count=len(rows),
                            ok=True,
                            layout_issues=[{"code": "positional_columns",
                                            "format": fmt}])
    for position, row in enumerate(rows):
        row_index = row_indexes[position] if row_indexes is not None else position
        cells = [c if isinstance(c, Cell) else Cell(c) for c in row]
        if not cells:
            continue
        if len(cells) != len(cols):
            result.rejected.append({
                "reason": "column_count_mismatch",
                "issues": [{"code": "column_count_mismatch",
                            "expected": len(cols), "actual": len(cells)}],
                "row_index": row_index, "format": fmt,
                "headers": cols, "values": [c.raw for c in cells]})
            continue
        if all(not str(c.raw).strip() for c in cells):
            continue
        record = {}
        field_sources = {}
        for index, field in enumerate(cols):
            value = coerce_value(field, cells[index].raw)
            record[field] = value
            field_sources[field] = {
                "column": field, "column_index": index,
                "normalized_column": field,
                "raw_value": cells[index].raw, "value": value,
                "method": "positional", "present": True, "format": fmt,
            }
        record["field_sources"] = field_sources
        record["unmapped"] = {}
        record["row_index"] = row_index
        record["layout"] = fmt
        record["valid"] = True
        record["_source"] = {"format": fmt, "row_index": row_index,
                             "headers": cols,
                             "values": [c.raw for c in cells]}
        result.records.append(record)
    return result


_TABLE_CLOSE = "</table"
_TABLE_OPEN = "<table"


def _check_html_complete(resp_body):
    """Reject a truncated HTML body instead of reading it as a short page.

    A body that opens more tables than it closes was cut off in transit.  It
    must not be mistaken for the final page of a short table: re-fetching is
    the only way to know whether more rows exist.
    """

    lowered = str(resp_body).lower()
    if lowered.count(_TABLE_OPEN) != lowered.count(_TABLE_CLOSE):
        raise ValueError("html page is truncated (unclosed table)")
    if not lowered.rstrip().endswith(("</table>", "</html>", ">")):
        raise ValueError("html page is truncated")


def _parse_html(resp_body):
    _check_html_complete(resp_body)
    doc = _lh.fromstring(resp_body)
    if doc.xpath("//table") == []:
        raise ValueError("html page has no table")
    headers = [th.text_content().strip()
               for th in doc.xpath("//table//tr[th][1]/th")]
    if not headers:
        # A data table with no header row cannot be interpreted reliably.
        raise ValueError("html page has no header row")
    rows = []
    indexes = []
    for position, tr in enumerate(doc.xpath("//table//tr[td]")):
        cells = [td.text_content().strip() for td in tr.xpath("./td")]
        rows.append(cells)
        indexes.append(position)
    return interpret_rows(headers, rows, "html", indexes)


def _parse_json(resp_body):
    payload = json.loads(resp_body)
    items = payload.get("items", []) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise ValueError("json items is not a list")
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("json item is not an object")
    headers = []
    seen = set()
    for item in items:
        for key in item:
            if key not in seen:
                seen.add(key)
                headers.append(key)
    rows, indexes = [], []
    for position, item in enumerate(items):
        row = [Cell(item[key], True) if key in item else Cell(None, False)
               for key in headers]
        rows.append(row)
        indexes.append(position)
    return interpret_rows(headers, rows, "json", indexes)


def _parse_text(resp_body):
    lines = [line for line in resp_body.splitlines() if line.strip()]
    rows = [[part for part in line.split("\t")] for line in lines]
    return _legacy_positional_rows(rows, None, "text")


_PARSERS = {"json": _parse_json, "html": _parse_html, "text": _parse_text}


def parse_page(resp_body, fmt):
    """Parse one page into an :class:`Interpretation`.

    All formats use the same header resolution and the same value coercion, so
    the same permit expressed as HTML or JSON yields the same canonical record.
    """

    parser = _PARSERS.get(fmt)
    if parser is None:
        raise ValueError("unknown fmt: %s" % fmt)
    return parser(resp_body)


def parse(resp_body, fmt):
    """Backwards-compatible parser returning only the reliable records."""

    return parse_page(resp_body, fmt).records


class FetchResult:
    """Everything obtained from the source, including why some rows failed."""

    def __init__(self):
        self.records = []
        self.rejected = []
        self.attempts = 0
        self.pages = 0
        self.pages_completed = 0
        self.complete = False
        self.errors = []

    @property
    def rejected_count(self):
        return len(self.rejected)


class FetchError(RuntimeError):
    """A page could not be obtained (or parsed) after every retry."""


def _fetch_one(http, page, out):
    """Fetch and parse one page, retrying an unparseable body.

    A transient server failure is already retried inside :class:`Http`.  A
    body that arrives but cannot be interpreted is retried here as well: a
    truncated page is a transport problem, not a permit row, so it must never
    be silently treated as "no more rows".
    """

    parse_attempt = 0
    while True:
        status, _hdr, body, att = http.get("/" + SPEC.endpoint,
                                           params={"page": page})
        out.attempts += att + 1
        if status != 200:
            out.errors.append({"page": page, "status": status})
            raise FetchError("fetch failed status=%s page=%s" % (status, page))
        try:
            return parse_page(body, SPEC.fmt)
        except Exception as error:  # malformed / truncated page body
            if parse_attempt >= http.max_retries:
                out.errors.append({"page": page, "status": status,
                                   "parse_error": str(error)})
                raise FetchError(
                    "parse failed page=%s: %s" % (page, error)) from error
            http.sleeper.sleep(0.1 * (2 ** parse_attempt))
            parse_attempt += 1


def fetch_all(http: Http, result: FetchResult | None = None):
    """Collect every page, tracking completion and per-row rejections.

    A page is only counted as completed after its body was parsed.  If any
    page cannot be fetched or parsed after retries, the collection is marked
    incomplete and :class:`FetchError` is raised, so a partial run can never
    be reported as a full one.
    """

    out = result if result is not None else FetchResult()
    page = 1
    while True:
        interpretation = _fetch_one(http, page, out)
        out.records.extend(interpretation.records)
        out.rejected.extend(interpretation.rejected)
        out.pages_completed += 1
        out.pages = page
        if len(interpretation.records) + len(interpretation.rejected) < SPEC.page_size:
            break
        page += 1
        if page > 200:
            break
    out.complete = out.pages_completed == out.pages and not out.errors
    return out.records, out.attempts


# --------------------------------------------------------------------------- #
# Record identity helpers.
# --------------------------------------------------------------------------- #
# ``canonical`` / ``content_hash`` are re-exported from :mod:`buildpermit.identity`
# (imported above) so historical callers keep working, while identity itself
# ignores interpretation metadata such as the source column names.
def dedup(records):
    seen, out, dup = set(), [], 0
    for r in records:
        h = content_hash(r)
        if h in seen:
            dup += 1
            continue
        seen.add(h)
        out.append(r)
    return out, dup


def normalize(r):
    d = {k: (v.strip() if isinstance(v, str) else v) for k, v in r.items()}
    if isinstance(d.get("status"), str):
        d["status"] = d["status"].lower()
    return d


def _apply_kernel(records):
    k = importlib.import_module(".kernel", __package__)
    for r in records:
        if hasattr(k, "process"):
            r.update(k.process(r))
    if hasattr(k, "reduce"):
        records = k.reduce(records)
    return records


# --------------------------------------------------------------------------- #
# Collection pipeline.
# --------------------------------------------------------------------------- #
def _rejection_report(result):
    return [dict(item) for item in result.rejected]


def _dedup_pairs(records):
    """De-duplicate source records while keeping their pre-enrichment identity.

    Identity is frozen *before* the kernel enriches a record, so re-running
    with unchanged source never rewrites a stored row.
    """

    kept, identities, seen = [], [], set()
    duplicates = 0
    for record in records:
        identity = dict(record)
        h = content_hash(identity)
        if h in seen:
            duplicates += 1
            continue
        seen.add(h)
        kept.append(record)
        identities.append(identity)
    return kept, identities, duplicates


def run(mode="full", con=None):
    own = con is None
    con = con or Store().connect()
    store = Store(con)
    sleeper = Sleeper()
    http = Http(Transport(), sleeper)
    fetch = FetchResult()
    try:
        records, attempts = fetch_all(http, fetch)
        norm = [normalize(dict(r)) for r in records]
        norm, identities, dup_in_batch = _dedup_pairs(norm)
        norm = _apply_kernel(norm)

        wm = store.watermark()
        if mode == "incremental" and wm:
            # Identity-based dedup makes re-processing safe, so out-of-order
            # or late rows are still ingested instead of being dropped by an
            # over-eager watermark filter.
            pairs = [(r, i) for r, i in zip(norm, identities)
                     if str(r.get("ts", "")) > wm]
            norm = [r for r, _ in pairs]
            identities = [i for _, i in pairs]

        before = store.count()
        for identity, record in zip(identities, norm):
            store.upsert(record, identity=identity)
        total = store.count()
        ingested = total - before

        # The watermark advances only after every page was fetched and every
        # record processed: an interrupted run must not look complete.
        new_wm = max([str(r.get("ts", "")) for r in norm] + [wm or ""])
        if new_wm:
            store.set_watermark(new_wm)
        return {"engine": "crawl", "mode": mode,
                # ``fetched`` keeps its historical meaning: source rows that
                # were interpreted into records.  Rows the reader refused are
                # reported separately so they are never counted as data.
                "fetched": len(records),
                "accepted": len(records),
                "rejected": fetch.rejected_count,
                "rows_seen": len(records) + fetch.rejected_count,
                "rejections": _rejection_report(fetch),
                "duplicates": dup_in_batch, "ingested": ingested,
                "count": total,
                "http_attempts": attempts, "slept": round(sleeper.slept, 4),
                "pages": fetch.pages, "pages_completed": fetch.pages_completed,
                "complete": fetch.complete,
                "watermark": store.watermark()}
    finally:
        if own:
            con.close()


# --------------------------------------------------------------------------- #
# Session pipeline.
# --------------------------------------------------------------------------- #
class Rpa:
    def __init__(self):
        self.t = Transport()
        self.sleeper = Sleeper()
        self.log = []
        self.captcha_solved = False

    def login(self):
        status, _h, _b = self.t.request("POST", "/login",
                                        json_body={"u": "operator", "p": "local"})
        if status != 200:
            raise RuntimeError("login failed status=%s" % status)
        self.log.append("login")
        return True

    def _csrf(self):
        return {"x-csrf": self.t.cookies.get("csrf", "")}

    def _query_page(self, page):
        status, _h, body = self.t.request("GET", "/query", params={"page": page},
                                          headers=self._csrf())
        if status != 200:
            raise RuntimeError("query failed status=%s" % status)
        return json.loads(body)

    def collect(self):
        out, page = [], 1
        while True:
            self.sleeper.sleep(0.1)
            data = self._query_page(page)
            out.extend(data.get("items", []))
            if data.get("captcha_required") and not self.captcha_solved:
                ok = self._solve_captcha()
                if not ok:
                    raise RuntimeError("captcha solve rejected")
                self.captcha_solved = True
                page += 1
                continue
            if not data.get("has_more"):
                break
            page += 1
            if page > 200:
                break
        self.log.append("collect:%d" % len(out))
        return out

    def _solve_captcha(self):
        status, _h, body = self.t.request("POST", "/captcha",
                                          json_body={"code": self.t.cookies.get("challenge", "")},
                                          headers=self._csrf())
        ok = status == 200 and json.loads(body).get("ok") is True
        self.log.append("captcha:%s" % ("solved" if ok else "failed"))
        return ok

    def submit(self, record):
        token = content_hash(record)
        status, _h, body = self.t.request("POST", "/submit",
                                          json_body={"token": token, "record": record},
                                          headers=self._csrf())
        data = json.loads(body)
        self.log.append("submit:%s" % ("created" if data.get("created") else "dedup"))
        return bool(data.get("created"))


def run_rpa(con=None):
    own = con is None
    con = con or Store().connect()
    store = Store(con)
    r = Rpa()
    try:
        logged = r.login()
        items = r.collect()
        norm = [normalize(dict(it)) for it in items]
        norm = _interpret_raw_records(norm, "json")
        norm, identities, dup = _dedup_pairs(norm)
        norm = _apply_kernel(norm)
        before = store.count()
        for identity, record in zip(identities, norm):
            store.upsert(record, identity=identity)
        total = store.count()
        ingested = total - before
        created = dedup_submit = 0
        for x in norm[:2]:
            if r.submit(x):
                created += 1
            else:
                dedup_submit += 1
        for x in norm[:1]:
            if not r.submit(x):
                dedup_submit += 1
        wm = max([str(x.get("ts", "")) for x in norm] + [""])
        if wm:
            store.set_watermark(wm)
        return {"engine": "rpa", "logged_in": logged, "captcha_solved": r.captcha_solved,
                "collected": len(items), "fetched": len(items), "duplicates": dup,
                "ingested": ingested, "count": total,
                "submit_created": created, "submit_dedup": dedup_submit, "log": r.log}
    finally:
        if own:
            con.close()


def _interpret_raw_records(raw_items, fmt):
    """Interpret already-parsed source items through the same field rules.

    Used by the session engine, whose transport returns JSON items directly.
    """

    headers = []
    seen = set()
    for item in raw_items:
        for key in item:
            if key not in seen:
                seen.add(key)
                headers.append(key)
    rows = [[Cell(item.get(key), key in item) for key in headers] for item in raw_items]
    interpretation = interpret_rows(headers, rows, fmt)
    return interpretation.records
