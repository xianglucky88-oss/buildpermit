# buildpermit

A small offline data-processing service with a local HTTP fixture and SQLite storage. It is self-contained and does not require network access.

## Usage

```text
make run       # process the bundled source
make run_inc   # process using the incremental command-line mode
make test      # run the test suite
make mock      # start the local fixture service
```

The package exposes a Python entry point under `src/buildpermit`. The fixture service supports the source formats used by the package, and the command-line entry point writes results to the local SQLite database. Re-running commands is safe for an existing database.

## Permit interpretation

Construction permits publish the construction unit (施工单位) and the
supervision unit (监理单位) in the same table. The publisher may re-order
columns, change header case/spacing, or rename a column at any time, so
`src/buildpermit/fields.py` is the single place that decides what each source
column means.

### Header resolution

* A header is folded with NFKC (full-width → half-width), then case, internal
  whitespace, punctuation and a trailing unit annotation such as `（元）` are
  removed. It is then matched as a **whole name** against an explicit synonym
  table.
* Matching is never by substring. `施工单位` can only ever resolve to the
  construction unit and `监理单位` only to the supervision unit; the two
  synonym families are disjoint and a duplicate mapping is a startup error.
* A header that mixes both concepts (for example `施工单位及监理单位`) is
  reported as *ambiguous* and left unmapped instead of being guessed. Owner
  columns (`建设单位`, `业主`) are a separate canonical field and are never
  folded into either party.
* Unknown columns are reported **and preserved** in `record["unmapped"]`.

### Provenance

Every canonical field carries `record["field_sources"][field]` with:

| key | meaning |
| --- | --- |
| `column` / `column_index` | the raw source header and its position |
| `normalized_column` | the folded header actually matched |
| `raw_value` | the value exactly as published |
| `value` | the canonical value derived from it |
| `method` | `alias` (header synonym) or `positional` (legacy TSV only) |
| `present` | `false` when a JSON item omitted the key entirely |

`record["_source"]` additionally keeps the page's header list and the whole raw
row, so any stored value can be traced back to what the source said.
Interpretation metadata is excluded from a record's identity
(`src/buildpermit/identity.py`), so the same permit expressed with different
column names, a different column order, or as HTML rather than JSON keeps one
identity and is de-duplicated correctly.

### Damaged input is rejected, not invented

The following are reported per row (or for the whole table) and are **never**
stored as reliable data:

* `duplicate_column` — two columns for one canonical field: the whole table is
  rejected rather than silently choosing one;
* `missing_required_column` — a required field (`id`, `name`, `value`,
  `status`) has no column: the whole table is rejected;
* `ambiguous_column` — reported; the column is not mapped;
* `column_count_mismatch` — a row whose width differs from the header: only
  that row is rejected;
* `empty_row` and `repeated_header_row` — rejected.

HTML and JSON use the same header resolution and value coercion, so the same
permit yields the same canonical record from either format. Each row is
interpreted independently: a damaged row never contaminates its neighbours.

### Pages, retries and completion

`framework.fetch_all` records `pages`, `pages_completed` and `complete`. A
page is counted as completed only after its body was parsed. Server errors are
retried with backoff; a body that cannot be parsed (for example a truncated
HTML table) is re-fetched instead of being read as the final short page. If a
page still cannot be obtained, the run raises `FetchError` before writing
anything and leaves the watermark untouched, so a partial run can never look
complete.

### Compatibility

`Store.hash` is computed over business fields only. The original
implementation hashed the kernel-enriched payload; opening such a database
re-keys each row to its identity-only hash in place, keeping the payload, the
watermark and every row. Re-processing an unchanged permit does not rewrite or
duplicate it.

