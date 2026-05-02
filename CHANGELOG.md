# Changelog

All notable changes to NRDS MCP Server container images will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Image tags follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

- Redeploy workflow simplified to a single `gcloud run deploy` command —
  no more manual `docker pull` / `tag` / `push` mirror step. AR remote
  repo handles ghcr.io→AR proxying transparently.

## [0.1.0] — 2026-05-02

First deployable container image.

### Added

- Multi-stage `Dockerfile` (Python 3.11-slim builder + slim runtime) producing
  a non-root image with `HEALTHCHECK` polling `GET /health` every 30 s.
- `/health` route returning `{"status":"ok"}` for liveness probes.
- Env-var configurable `MCP_HOST`, `MCP_PORT`, `MCP_TRANSPORT` (defaults
  `0.0.0.0`, `9000`, `sse` — backwards-compatible with the pre-container
  hardcoded values).
- `nextgen_mcp/requirements.lock` — full transitive closure (99 pinned
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
  used; further reduction (distroless, alpine) deferred — alpine risks
  musl/glibc compatibility for prebuilt scientific Python wheels.
- The `scripts/setup-mcp.sh` developer workflow is unchanged; container
  is the deploy path.
- `test_mcp/test_large_catalog_server.py` is a runnable load-test fixture,
  not pytest tests; not run in CI.

[Unreleased]: https://github.com/Aquaveo/nrds_mcps/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Aquaveo/nrds_mcps/releases/tag/v0.1.0
