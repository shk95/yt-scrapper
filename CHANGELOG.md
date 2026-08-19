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

- **A sampler, so a history starts accumulating.** `tubedepth-sample.timer` in
  `deploy/` forces a re-collection of a watch list every hour; the list is
  `~/.config/tubedepth/watchlist.txt`, one video id per line, and
  `deploy/watchlist.example.txt` shows the format. Off unless you enable it.
  Velocity is the difference between two observations and observations only
  accumulate in real time, so this is worth starting before anything needs it.
- **`tubedepth enqueue --refresh` and `--from-file`.** The first puts the same
  forced collection on the command line that `POST /v1/jobs` has; the second
  reads targets one per line, so a schedule points at a list instead of
  carrying thirty ids on its `ExecStart`. A list that cannot be read is refused
  rather than treated as empty.

### Added

- **The dashboard can drive the queue, not just watch it.** Submit a
  collection, force past the freshness window, cancel a job, ask again for one
  that failed, read a result, and click a digest to read the observation behind
  it — the cell that used to be dead text. A submission answered 200 says so
  rather than looking like nothing happened, and a cancelled-but-running job
  says that the extraction is still spending requests. No server change: every
  one of those routes already existed.
- **`trending.videos` — what YouTube itself calls popular.** The trending page
  was retired and there is no ranking left to scrape, but the Data API's
  `chart=mostPopular` outlived it: verified 2026-08-20, 200 results per region.
  This is the only kind that reports a ranking rather than an observation —
  everything else here becomes a trend by being collected twice, and YouTube's
  own ordering cannot be reconstructed from any number of samples.

  It has its own lane because it spends Google's quota rather than the
  per-address YouTube budget everything else competes for, so a quarantine on
  one must not throttle the other. Set `TUBEDEPTH_DATA_API_KEY`; without it
  that one kind fails as a configuration error and nothing else is affected.

  The payload is a `VideoListing`, so `--then` already works:
  `tubedepth enqueue trending.videos KR --then video.metadata` turns one queued
  region into a metadata job per trending video.
- **CI refuses a payload model change that does not bump `schema_version`.**
  `tests/test_migrations.py` has always caught the database half of this; there
  was no payload-side equivalent, and one bump was already missed in the
  history. The check records a pruned shape per kind and version in an
  append-only lock, so the only way to make it pass is the bump it is asking
  for — a blind regenerate is refused. Composite kinds expand their parts, so
  a change to `video.metadata` correctly moves `video.bundle` too. Green means
  no shape change went unrecorded; it never means no bump was needed.
- **`GET /v1/artifacts/{digest}`.** The list route has always handed out
  digests and nothing could dereference them; reaching an old payload meant
  having kept the job id that produced it, and retention deletes artifacts
  without touching job rows, so those two age apart. The payload comes back
  **verbatim** — no model is in the path, so an observation an older normalizer
  wrote still reads. `payload_fields` against `current_fields` is the honest
  answer to what an old observation lacks: a field never collected is absent,
  which says more than a null.
- **`410 retracted`.** A source can declare a version whose payloads are wrong
  rather than old — `channel.about` v1 read the home tab as the about panel —
  and reading one is refused instead of laundered. 410 and not 404, because the
  observation happened.
- **`tubedepth backfill-schema-versions`.** Attributes payloads collected
  before the version was recorded, by recomputing fingerprints against the
  versions a kind has had. Rows that match nothing are left blank and reported
  by kind rather than guessed at.
- **`tubedepth capture-fixture --innertube <surface>`.** InnerTube fixtures
  had no recording path at all, so the four in the tree were made by hand and
  the redaction that strips session identity and signed `googlevideo` URLs ran
  only if whoever made them remembered to call it. Recording goes through the
  same helpers the sources use, so a fixture is what production receives.
  `browse-channel-about` is deliberately not recordable — its data sits behind
  a runtime continuation, and a half-right fixture for the surface that has
  already broken once is worse than none.
- **`TUBEDEPTH_COOKIES_FILE` now does what the troubleshooting guide said it
  did.** Point it at a Netscape-format cookie jar and the worker carries it
  into every extraction. A path that is not there is refused at startup rather
  than dropped, because silently ignoring a typo behaves exactly like the
  version that read nothing at all.

### Fixed

- **The cache key no longer ignores half its inputs.** A source's parameters —
  a listing's `limit`, a comment harvest's `sort`, a transcript's language
  preference, a bundle's parts — were frozen at construction and left out of
  the fingerprint, so a listing capped at 100 would have answered a request for
  1,000 the moment that cap became configurable. Six kinds' fingerprints move
  once as a result and their caches go cold; the other five are byte-identical
  and untouched. `collect` and `cached` now build the key in one place, because
  fixing one without the other is worse than fixing neither.
- **An expensive kind is queued with fewer attempts than a cheap one.**
  `Job.max_attempts` documented itself as set when a job is queued and nothing
  set it, so every kind took the column default of three — and three failed
  comment harvests against one target spend around a hundred requests of the
  one per-address budget everything here competes for. Expensive kinds now get
  two; cheap and standard keep the three they had.
- **`tubedepth work --once` delivers its callbacks and reaps stale leases.**
  It called `run_once`, which is the primitive and does neither, so the one
  invocation with no next run to catch up was the one that skipped the
  bookkeeping — a job it finished was never announced. `--once` is now
  `drain(limit=1)`: one path with a bound rather than two paths that disagree.
- **Retention no longer unlinks a payload a current observation still uses.**
  The store is content-addressed, so two observations that collected identical
  bytes are one file — which `GET /v1/artifacts` teaches readers to expect,
  since equal digests are how "nothing changed" is read. Pruning unlinked on
  every expiring row without checking, so the older of two identical
  observations took the payload of the newer one with it. The surviving row
  then served a silent cache miss and its job answered 500. Nothing had aged
  out yet on any store built so far, so this had not fired.
- **A job whose result has aged out answers 404 instead of 500.** Retention
  removes artifacts and never touches job rows, so this is the ordinary end
  state of an old job. It reached FastAPI's default handler as an unhandled
  `FileNotFoundError`.
- **`refresh` now reaches the worker.** `"refresh": true` on `POST /v1/jobs`
  skipped the API's own cache check and was then discarded, so the job it
  created was served from the cache anyway: it succeeded, pointed at the
  payload collected hours earlier, and recorded no new observation. Anything
  polling faster than a kind's freshness window was collecting nothing while
  reporting success. The flag is a column on the job now, so it survives the
  queue and a retry. A database that already exists gains the column from the
  startup repair, so a running deployment does not break — but run `tubedepth
  migrate` anyway, or Alembic's version table stays behind and the next
  migration tries to add a column that is already there.

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

- **Documentation.** A REST reference at [`docs/api.md`](docs/api.md) covering
  every endpoint, the job lifecycle, cursor paging, the error codes and the
  signed webhook contract. `README.md`, `docs/api.md` and this changelog are
  English originals with Korean translations beside them; the contributor
  documents stay Korean. The claims a machine can check — routes, kinds, error
  codes, versions — are checked against every copy.
- **One version, in one place.** `pyproject.toml` reads it from
  `src/tubedepth/__init__.py`, and a test refuses a state where the package and
  this changelog disagree. [`docs/releasing.md`](docs/releasing.md) is the
  procedure.

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
