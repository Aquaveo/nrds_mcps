# Changelog

All notable changes to NRDS MCP Server container images will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Image tags follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.1] - 2026-05-18

### Added

- New MCP tool `query_output_files_from_output_selector` runs a single
  read-only DuckDB query across **all parquet output files** for a
  selector (model/date/forecast/cycle/vpu, plus `ensemble` for
  `medium_range`) as a unified dataset. Each result row carries two
  provenance columns: `filename` (basename only — e.g.
  `troute_output_202605180100.parquet`) and `source_path` (full S3 URL).
  The basename split lets LLM SQL extract time portions or labels via
  `substr` / `regexp_extract` without tripping over the `s3://...`
  prefix. Use this for cross-file aggregations and ranking when the
  singular `query_output_file_from_output_selector` would otherwise
  require N separate calls.

  - Parquet only — NetCDF outputs in the same directory are intentionally
    ignored. Selectors that resolve to only `.nc` files return a
    `not_found` envelope with `count` reflecting the unfiltered total so
    the LLM can redirect to the singular tool.
  - Default query: `SELECT filename, * FROM output LIMIT 10`. The
    description recommends WHERE-clause filtering (by feature_id, time
    range, etc.) for data extraction and reserves LIMIT for schema
    exploration / sampling only. Earlier iteration of this tool used
    `SELECT filename, * FROM output LIMIT 10` as the default; the
    `LIMIT 10` was anchoring downstream LLM calls — they copied it
    into every retry even for time-series extraction where dropping
    rows breaks the series. New default:
    `SELECT filename, COUNT(*) AS rows_per_file FROM output GROUP BY filename ORDER BY filename`
    — gives the LLM a useful first look at row counts without biasing
    toward truncation.

### Changed

- `EXPECTED_MIN_TOOLS` in `release.yml` bumped 10 → 11.
- `REQUIRED` smoke list in `release.yml` adds
  `query_output_files_from_output_selector` so a regression that drops
  the registration fails the deploy gate.
- **Server log readability**: the
  `query_output_file_from_output_selector` and
  `query_output_files_from_output_selector` tool handlers no longer
  dump the entire result envelope (including the full `data` array) to
  INFO logs after completion. They now log a one-line summary with
  aggregates *and a single sample row* — operators can spot-check that
  the data looks right without the per-row dump. Success line shape:

      ok=True file_count=10 rows=240 columns=[time,flow]
      sample_row={'time': '2026-05-18T02:00:00.000000Z', 'flow': 18.58}

  Error line shape:

      ok=False code=not_found message=No output files matched. fix_hint_preview=...

  The previous behavior produced ~19 KB single-line log entries on a
  10-file × 240-row query; the new format is capped at ~500 chars
  regardless of payload size. Wide column lists are truncated with a
  `...+N` tail. New helper: `nextgen_mcp.utils._summarize_tool_result`.
- **Validation envelope quality**: when a tool input fails a
  Pydantic constraint (`string_pattern_mismatch`, numeric bounds,
  length, enum), the envelope now surfaces the field's
  `Field(description=...)` text alongside the constraint. For example,
  `date='ngen.20250929'` previously produced
  `fix_hint: "date must match pattern '^(?:\\d{4}-\\d{2}-\\d{2}|...)$'"`
  which the LLM had to mentally parse the regex to recover from. The
  new envelope reads
  `fix_hint: "date (YYYY-MM-DD or YYYY/MM/DD) must match pattern '...'"`
  and the `details[].description` field carries the same text for
  structured-data consumers. Shortens recovery to one round trip when
  the LLM mis-formats a regex-constrained kwarg.

## [0.4.0] - 2026-05-16

### Removed (BREAKING - tool surface)

- **`query_hydrofabric_parquet_file` is removed.** Its hydrofabric-id
  search overlapped almost entirely with `lookup_hydrofabric_feature`
  (same parquet, same `id`/`divide_id` match-ranking SQL); the only
  meaningful differences were the output envelope shape and the row
  limit. The merged tool keeps `lookup_hydrofabric_feature`'s
  data-only envelope (`{rows, pmtiles_layer, bbox}`) and grows a
  `limit` argument (default `1`, max `200`) for callers that need
  more than one match.

### Changed

- `lookup_hydrofabric_feature` accepts an optional `limit: int`
  argument (default `1`, range `1..200`). Previous behavior is
  preserved when callers omit `limit`.
- `prompts/list` drops the `query_hydrofabric` slash-command template
  (it drove the removed tool); use `lookup_feature` instead.
- `EXPECTED_MIN_TOOLS` in `release.yml` bumped 11 → 10.

### Changed

- **Date regex deduplicated.** `utils.DATE_PATTERN` and
  `validation.DATE_RE` both defined the same regex (`^(?:\d{4}-\d{2}-
  \d{2}|\d{4}/\d{2}/\d{2})$`). The pattern string now lives in
  `validation.py` only; `DATE_RE` is `re.compile(DATE_PATTERN)`, so
  the compiled object and the string-for-pydantic-Field are
  guaranteed to stay in lockstep. `tools.py` imports `DATE_PATTERN`
  from `.validation` instead of `.utils`.

### Removed

- **`_helpers.py` merged into `utils.py`.** Both files held tool-body
  helpers and the split was arbitrary - `_helpers` already imported
  date constants and `_parse_iso_date` from `utils`. Consolidating
  removes the cross-module dependency between two files with the
  same conceptual role. `_preview_text`, `_validate_date_bounds`,
  `_parse_date_or_today`, `_require`, and the `_MIN_ALLOWED_DATE`
  constant now live in `utils.py`. Callers update their import path
  from `._helpers` to `.utils`.

- **`utils._get_json_raw` and its endpoint dispatch table are removed.**
  The function was a vestigial REST-shim from an earlier era when these
  MCP tools wrapped an HTTP API; every `_get_json_raw("foo", params=p)`
  call has been rewritten to call `logic.foo(**p)` directly. Tools in
  `tools.py` now import from `logic` directly. Net effect: ~50 lines
  removed from `utils.py`, one less indirection layer, and tool call
  graphs are now visible to grep without chasing a string-keyed dispatch.

- **`test_mcp/test_large_catalog_server.py` is removed.** The fixture
  was a standalone runnable for exercising FastMCP's
  `BM25SearchTransform` search-facade against a synthetic large tool
  catalog. Long-catalog handling now lives on the client side, so the
  server has no search-facade surface to load-test.
- BM25-related comment in
  `nextgen_mcp/middleware/_input_validation_middleware.py` simplified
  - the underlying rationale (MCP protocol already associates tool
  result with call) stands; the search-facade leak motivation no
  longer applies.

### Fixed

- Latent `NameError` in `tools.py`: `_preview_text` was used in
  `query_output_file_from_output_selector` and `query_output_file` tool
  bodies but never imported. Tests passed only because the line was
  unreachable during collection. Added to the `_helpers` import.
- Python-level name collision between the `lookup_hydrofabric_feature`
  MCP tool function and the `logic.lookup_hydrofabric_feature`
  function. The tool's Python def is renamed to
  `lookup_hydrofabric_feature_tool` (the MCP-facing name stays
  `lookup_hydrofabric_feature` via the decorator's `name=` arg),
  consistent with every other tool in the file.
- Pre-existing relative-import typo (`..middleware`) in `_mcp.py`,
  `_helpers.py`, and `tools.py` corrected to `.middleware`. The
  middleware package is a subpackage of `nextgen_mcp/`, not a sibling.
- Stale `from middleware._input_validation_middleware import ...` paths
  in `test_mcp/test_middleware.py` updated to
  `from nextgen_mcp.middleware._input_validation_middleware import ...`.
- README dead references cleaned up: top-level README had a stray
  `=`, an unclosed parenthesis, and a "13 tools" smoke-gate count
  that drifted from `release.yml`. `nextgen_mcp/README.md` was
  rewritten as a short dev-loop pointer to the canonical top-level
  README - old content referenced the pre-2026-05-02 `nextgen_plugins/`
  layout, a `/sse` default, dead `NRDS_API_HOST`/`OLLAMA_HOST` env
  vars, removed `create_plotly_chart_*` tools, and a `.devcontainer/`
  directory that no longer exists in this repo.
- `validators.py` and `validations.py` merged into a single
  `validation.py`. The duplicate `Forecasts` / `FORECASTS` `Literal`
  collapsed to one canonical `FORECASTS`.

## [0.2.0] - 2026-05-04

### Removed (BREAKING - tool surface)

- **`create_plotly_chart_from_output_selector` is removed.** Use
  `query_output_file_from_output_selector` to fetch rows, then call
  the host's chart-creation tool (e.g., `create_plotly_chart` on
  tethysdash) with the rows as inline data.
- **`create_plotly_chart_from_parquet_output_file` is removed.** Use
  `query_output_file` (or `query_output_file_from_output_selector`)
  for the data, then chain into the host's chart-creation tool.
- **`build_hydrofabric_feature_map_config` is renamed and reshaped
  to `lookup_hydrofabric_feature`.** New return shape is data-only:
  `{rows, pmtiles_layer, bbox}` (or `{rows: [], pmtiles_layer: null,
  bbox: null}` on empty match). The host is responsible for any
  map rendering or visualization assembled from this data.

### Why

Renderable payloads (Plotly figure JSON, map-config blobs) returned
by an MCP server require the consuming host to know that server's
specific render contract. Returning `{figure: {...}}` or
`{highlight: {...}, camera: {...}}` from this server made the host
either reach into engine internals to dispatch the payload as a
grid item or silently drop it on the floor. Real-world result:
charts succeeded server-side but never reached the dashboard, and
the LLM, with no signal of dispatch failure, told the user the
chart had been generated.

After this release, NRDS MCP returns data only. Any host pairing
with this server owns its own rendering envelopes. See the
chatbox-core `_engine_dispatched` contract (0.3.0) for in-band
dispatch feedback that backs this separation.

### Migration

Replace single-step calls with two-step chains:

| Before (single tool) | After (data + host render) |
|---|---|
| `create_plotly_chart_from_output_selector(...)` | `query_output_file_from_output_selector(...)` → host chart tool |
| `create_plotly_chart_from_parquet_output_file(...)` | `query_output_file(...)` → host chart tool |
| `build_hydrofabric_feature_map_config(...)` | `lookup_hydrofabric_feature(...)` → host map tool |

The remaining 11 tools (lists, resolvers, queries) are unchanged.

### Internal cleanup

- `nextgen_mcp/rest.py` - chart helpers
  (`create_plotly_chart_from_output_file`,
  `create_plotly_chart_from_parquet_output_file`) deleted.
  `build_hydrofabric_feature_map_config` replaced with the data-only
  `lookup_hydrofabric_feature` (uses new `_bbox_from_row` helper plus
  the existing `_duckdb_lookup_hydrofabric_feature`,
  `_normalize_record`, `_get_feature_center` from `utils_rest.py`).
- `nextgen_mcp/utils.py` - corresponding imports + dispatch entries
  removed; new dispatch entry for `lookup_hydrofabric_feature` added.

### Changed (BREAKING - URL path, carried over from prior unreleased)

- **Default transport switched from SSE to Streamable HTTP.** The MCP
  endpoint moves from `/sse` to `/mcp` (FastMCP default for streamable-http).
  The legacy `/sse` URL returns 404 in default config.
- **Migration:** clients with hardcoded `.../sse` URLs (e.g., tethysdash's
  saved MCP server config) must update to `.../mcp`. chatbox-core's
  `pickTransport()` auto-detects from the URL suffix, so only the URL
  string changes - no code change in consumers.

### Fixed

- `ALLOWED_ORIGINS` is now read from the env var (was previously a
  hardcoded localhost-only Python list, ignoring the deploy command's
  `--set-env-vars=ALLOWED_ORIGINS=*`). Browser-based MCP clients
  (MCP Playground, hosted MCP Inspectors) at non-localhost origins
  now succeed CORS preflight against the deployed server. The
  `ALLOW_CREDENTIALS` flag auto-derives: `False` when `ALLOWED_ORIGINS=["*"]`
  (CORS spec forbids the combination), `True` when origins are explicit.

### Added

- Tag-driven Cloud Run auto-redeploy. `release.yml` extended with a
  `deploy` job that authenticates to GCP via Workload Identity Federation
  (no JSON key), runs `gcloud run deploy` against the AR remote-repo image,
  and smokes `/health` before reporting success. The deploy command also
  configures a Cloud Run startup probe on `/health` so failed revisions
  never receive traffic. `workflow_dispatch` trigger added with a required
  `tag` input for re-deploying an existing tag from the Actions UI.

### Changed

- Release workflow gains a `concurrency:` group (`cloud-run-deploy-nrds-mcps`)
  that serializes deploys; parallel tag pushes wait their turn rather than
  racing. Build job (`release`) only runs on `push: tags: 'v*'`; manual
  re-deploys via `workflow_dispatch` skip build and go straight to deploy.
- Deploy job's smoke gate strengthened: after the existing `/health`
  poll, the workflow now probes MCP `tools/list` via FastMCP client
  (pinned to `fastmcp==3.2.4`) and fails the workflow if fewer than 13
  tools are registered. Catches tool-init crashes that leave `/health`
  green but the MCP tool registry broken. Threshold (`EXPECTED_MIN_TOOLS=13`)
  is inline in `release.yml`; bump explicitly when adding/removing tools.



- Public test deployment on Google Cloud Run at
  `https://nrds-mcps-43707369422.us-central1.run.app` (project `ibis-436806`,
  region `us-central1`). Image served via Artifact Registry **remote
  repository** `nrds-mcps-remote` that transparently proxies
  `ghcr.io/aquaveo/nrds-mcps`. Cloud Run pulls from
  `us-central1-docker.pkg.dev/ibis-436806/nrds-mcps-remote/aquaveo/nrds-mcps:<TAG>`
  and AR fetches from ghcr.io on cache miss. Public unauthenticated
  ingress; no AWS credentials needed (NRDS S3 bucket is public). See
  README's "Test Deployment" section for redeploy procedure and
  tool-call examples.

### Changed

- Redeploy workflow simplified to a single `gcloud run deploy` command -
  no more manual `docker pull` / `tag` / `push` mirror step. AR remote
  repo handles ghcr.io→AR proxying transparently.

## [0.1.0] - 2026-05-02

First deployable container image.

### Added

- Multi-stage `Dockerfile` (Python 3.11-slim builder + slim runtime) producing
  a non-root image with `HEALTHCHECK` polling `GET /health` every 30 s.
- `/health` route returning `{"status":"ok"}` for liveness probes.
- `nextgen_mcp/requirements.lock` - full transitive closure (99 pinned
  packages) for reproducible builds.
- GitHub Actions CI: Python smoke import + Docker build + container smoke
  (start image, poll `/health`, stop) on every push and PR.
- GitHub Actions release workflow: on `v*` tag, multi-arch (amd64 + arm64)
  build pushed to `ghcr.io/aquaveo/nrds-mcps:VERSION` and `:latest` with
  build provenance + SBOM attestations.
- README rewrite as deployment-facing documentation: docker run quick-start,
  env-var table, healthcheck, endpoints, local-dev fallback.

### Notes

- Image size: ~750 MB (numpy/pandas/pyarrow account for most). Slim base
  used; further reduction (distroless, alpine) deferred - alpine risks
  musl/glibc compatibility for prebuilt scientific Python wheels.
- The `scripts/setup-mcp.sh` developer workflow is unchanged; container
  is the deploy path.
- `test_mcp/test_large_catalog_server.py` is a runnable load-test fixture,
  not pytest tests; not run in CI.

[Unreleased]: https://github.com/Aquaveo/nrds_mcps/compare/v0.4.1...HEAD
[0.4.1]: https://github.com/Aquaveo/nrds_mcps/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/Aquaveo/nrds_mcps/compare/v0.3.0...v0.4.0
[0.1.0]: https://github.com/Aquaveo/nrds_mcps/releases/tag/v0.1.0
