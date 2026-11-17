
from types import SimpleNamespace

ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "buildpermit.db"

SPEC = SimpleNamespace(
    slug="buildpermit",
    fmt="html",
    engine="crawl",
    endpoint="items",
    columns=["id", "name", "value", "status", "ts", "contractor"],
    page_size=4,
    int_fields=["value"],
    ts_field="ts",
)
