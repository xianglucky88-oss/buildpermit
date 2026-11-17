
REQUIRED = ["id", "name", "value", "status"]
def process(record):
    missing = [c for c in REQUIRED if record.get(c) in ("", None)]
    return {"missing_fields": ",".join(missing), "is_complete": int(not missing)}
