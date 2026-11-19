
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import hashlib
import json
from fastapi import FastAPI, Request, Response
from buildpermit.config import SPEC
from buildpermit.mockdata import build_rows, extra_row

app = FastAPI(title="Local source")
_ALL = build_rows()
_extra = []
_verified_sessions = set()
_accepted_tokens = set()
# Test hooks: pages that fail a fixed number of times, and pages whose body is
# truncated.  They make retry/recovery behaviour observable without touching
# the packaged data.
_fail_plan = {}       # page -> remaining 5xx/429 failures
_corrupt_plan = {}    # page -> remaining truncated bodies
_override = {}        # arbitrary page -> explicit response spec


def _data():
    return _ALL + _extra

def _page_items(page):
    lo, hi = (page - 1) * SPEC.page_size, page * SPEC.page_size
    return _data()[lo:hi]

def _render(data, page):
    if SPEC.fmt == "json":
        return {"items": data, "page": page}
    if SPEC.fmt == "text":
        lines = ["\t".join(str(it.get(c, "")) for c in SPEC.columns) for it in data]
        return Response(content="\n".join(lines), media_type="text/plain")
    th = "".join("<th>%s</th>" % c for c in SPEC.columns)
    trs = "".join("<tr>" + "".join("<td>%s</td>" % it.get(c, "") for c in SPEC.columns)
                  + "</tr>" for it in data)
    return Response(content="<html><table><tr>%s</tr>%s</table></html>" % (th, trs),
                    media_type="text/html")

def _headers_override():
    return SPEC.columns

@app.get("/items")
def items(page: int = 1):
    if page in _override:
        spec = _override[page]
        return Response(status_code=spec.get("status", 200),
                        content=spec.get("body", ""),
                        media_type=spec.get("media_type", "text/html"))
    if _fail_plan.get(page, 0) > 0:
        _fail_plan[page] -= 1
        return Response(status_code=503)
    data = _page_items(page)
    rendered = _render(data, page)
    if _corrupt_plan.get(page, 0) > 0:
        _corrupt_plan[page] -= 1
        body = rendered.body if isinstance(rendered, Response) else json.dumps(rendered)
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
        return Response(content=text[: max(1, len(text) // 3)],
                        media_type="text/html")
    return rendered

@app.get("/headers")
def headers():
    """Publish the current header row, for tests that rename columns."""
    return {"columns": _headers_override(), "format": SPEC.fmt}

@app.get("/refresh")
def refresh():
    _extra.append(extra_row(len(_extra) + 1))
    return {"total": len(_data())}

@app.get("/grow")
def grow():
    return refresh()

@app.get("/probe")
def probe():
    app.__dict__["_flaky_n"] = app.__dict__.get("_flaky_n", 0) + 1
    if app.__dict__["_flaky_n"] <= 2:
        return Response(status_code=429)
    return {"ok": True, "attempts": app.__dict__["_flaky_n"]}

@app.get("/flaky")
def flaky():
    return probe()

@app.get("/health")
def health():
    return {"ok": True, "rows": len(_data())}

@app.post("/_plan")
async def plan(request: Request):
    """Install test hooks: {"fail": {page: n}, "corrupt": {page: n}, "override": {...}}."""
    body = await request.json()
    for page, n in (body.get("fail") or {}).items():
        _fail_plan[int(page)] = int(n)
    for page, n in (body.get("corrupt") or {}).items():
        _corrupt_plan[int(page)] = int(n)
    for page, spec in (body.get("override") or {}).items():
        _override[int(page)] = spec
    return {"ok": True}

@app.post("/_reset_plan")
def reset_plan():
    _fail_plan.clear()
    _corrupt_plan.clear()
    _override.clear()
    return {"ok": True}

@app.post("/login")
def login(response: Response):
    sid = "SID-local"
    csrf = "CSRF-local"
    challenge = hashlib.sha256((sid + csrf).encode()).hexdigest()[:4]
    response.set_cookie("sid", sid)
    response.set_cookie("csrf", csrf)
    response.set_cookie("challenge", challenge)
    return {"role": "operator"}

@app.get("/query")
def query(request: Request, page: int = 1):
    if not request.cookies.get("sid"):
        return Response(status_code=401)
    if request.headers.get("x-csrf") != request.cookies.get("csrf"):
        return Response(status_code=403)
    items_page = _page_items(page)
    nxt = _page_items(page + 1)
    sid = request.cookies.get("sid")
    gate = (SPEC.engine == "rpa" and page == 1 and sid not in _verified_sessions
            and len(_data()) > SPEC.page_size)
    return {"items": items_page, "page": page, "has_more": bool(nxt),
            "captcha_required": bool(gate)}

@app.post("/captcha")
async def captcha(request: Request):
    body = await request.json()
    ok = body.get("code") == request.cookies.get("challenge")
    if ok:
        _verified_sessions.add(request.cookies.get("sid"))
    return {"ok": bool(ok)}

@app.post("/submit")
async def submit(request: Request):
    body = await request.json()
    token = body.get("token")
    created = token not in _accepted_tokens
    if created:
        _accepted_tokens.add(token)
    return {"created": bool(created), "token": token}
