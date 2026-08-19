# Changelog

Every notable change to this project, in the format of
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
[semantic versioning](https://semver.org).

*[한국어](CHANGELOG.ko.md)*

Two things are versioned here and they move independently. **The package
version** is the one below, written in `src/tubedepth/__init__.py` and reported
by `tubedepth version` and `GET /healthz`. **The `/v1` HTTP contract** moves
only when a change would break a client written against
[`docs/api.md`](docs/api.md).

Before 1.0, a minor bump may change the shape of collected payloads. Stored
artifacts are keyed by each source's `schema_version`, so an old payload stays
readable as what it was.

How a release is cut: [`docs/releasing.md`](docs/releasing.md).

## [Unreleased]

### Added

- REST API reference at [`docs/api.md`](docs/api.md): every endpoint, the job
  lifecycle, cursor pagination, the error-code table and the signed webhook
  contract.
- Korean translations of the outward-facing documents — `README.ko.md`,
  `docs/api.ko.md`, `CHANGELOG.ko.md`. The English files are the originals.
- This changelog, and [`docs/releasing.md`](docs/releasing.md).

### Changed

- The package version is defined in one place. `pyproject.toml` declares it
  dynamic and reads `src/tubedepth/__init__.py`, so a release is one edit that
  cannot half-succeed.
- `README.md` is now English; its Korean text moved to `README.ko.md`.
- The documentation checks in `tests/test_documentation_is_true.py` run against
  translations too, locate the capability table by an HTML marker rather than
  by a heading, assert that every served route and every error code appears in
  the API reference, and assert that the package version and both changelogs
  agree.

### Fixed

- The route check no longer rewrote any run of eight or more alphanumerics into
  a path parameter, which read the served `/v1/artifacts` as a route that does
  not exist. Paths are matched against the real route templates instead.

## [0.1.0] - 2026-08-19

Everything the plan called M0–M9, plus an operator dashboard it did not ask
for. Not yet exercised under sustained load; see the honest limits in the
[README](README.md).

### Added

- **Collection, 11 kinds.** `video.metadata`, `video.transcript`,
  `video.comments`, `video.sponsor_segments`, `video.related`, `video.bundle`,
  `channel.about`, `channel.community`, `channel.videos`, `playlist.items`,
  `search.videos`. Listings fan out to per-item collection with `--then`.
- **Job queue.** Durable claim, retry with backoff, lease reaping, lease
  renewal while a job runs, cancellation, and cost lanes so a comment harvest
  cannot starve sub-second work.
- **Egress control.** An AIMD rate controller per (egress, lane), quarantine,
  and verdict classification that defaults to `NEUTRAL` — an unrecognised
  failure never burns a working address, and a parser mismatch never touches
  egress health at all.
- **Storage.** Content-addressed gzip payloads, an artifact index, age-based
  retention and an orphan sweep.
- **HTTP API** behind `X-API-Key`: jobs, artifacts, results, sources, health.
  Cursor-paged listings with filters and time ranges.
- **Operator dashboard** at `/`, self-contained, reading the same `/v1` routes.
  It loads on a network with no route to the internet.
- **Per-source health**, written by the worker and read by the API, which tells
  `broken` (our parser) from `blocked` (the address) from `stale` (nothing has
  run it).
- **Signed webhooks** on job completion. HMAC-SHA256 over timestamp and body,
  so a recorded delivery cannot be replayed later.
- **Deployment**: systemd user units for the API and the worker, needing no
  root, and Alembic migrations.
- Identifier normalisation from every URL form a video, channel, playlist or
  query arrives in.

### Removed

- **`video.dislikes`**, deliberately. The source served a reconstructed
  estimate that nobody can adjudicate against an original YouTube no longer
  publishes. Its removal also removed the only measured argument for a proxy
  pool — recorded in [`docs/status.md`](docs/status.md).
- **`channel.profile`**, cancelled before it was built: its contents arrive in
  a response `channel.about` already makes.

### Fixed

- `channel.about` read the home tab instead of the about panel.
- API reads no longer take the write lock, which had put a route that counts
  rows at p99 1,434 ms against 335 ms for one that touches no database.
- The worker renews its lease while a job runs, so a long comment harvest is no
  longer reaped as dead and retried.
- The query indexes the plan specified and nobody had added.

[Unreleased]: https://github.com/slopindustries/yt-scrapper/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/slopindustries/yt-scrapper/releases/tag/v0.1.0
