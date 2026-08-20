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

### Changed

- **The boot path issues no DDL any more (#14).** `_database()`, which every
  CLI entry point goes through, used to call `create_schema()` — a
  convenience while this owned a SQLite file. On a database shared with other
  services that is rule 6 of `docs/shared-postgres.md`, and it silently broke
  migrations: a boot that added a column left `alembic_version` untouched, so
  the next `alembic upgrade` tried to add a column that was already there.
  `Database.create_schema()` still exists — it is how tests and a fresh
  `--data-dir` get a database — but it now only creates what is missing; the
  column and index repair it used to do is gone, because `tubedepth migrate`
  now covers the same gap and keeps `alembic_version` honest while doing it.
  The only schema path is `tubedepth migrate`.

### Fixed

- **`prune` refuses to sweep a payload store whose index has no rows at all.**
  The orphan sweep decides by absence — a payload no artifact row points at is
  rubbish — and that inference inverts silently when the index is empty:
  every file is an orphan and the whole store is deleted while the log reads
  like a successful sweep. It is also exactly what a half-finished database
  cutover looks like, with `TUBEDEPTH_DATABASE_URL` moved to a fresh instance
  and `TUBEDEPTH_DATA_DIR` still holding the payloads the old index knew about.
  Refusing costs one command; guessing wrong costs every observation ever
  collected, and no re-collection recovers a view count from three weeks ago.
  A store that genuinely has no index passes `--sweep-without-an-index`.
- **`GET /v1/artifacts/{digest}` no longer blames retention for bytes it cannot
  find.** The message asserted "it has aged out of retention" unconditionally,
  including for an observation two days old under a thirty-day policy. It now
  offers both explanations — retention, or an index separated from its payload
  store — and says when the observation was made.
- **The API answers what `docs/api.md` already said it answers (#21).** A
  rejection FastAPI raises before a route runs now carries the documented
  `error` shape rather than `detail`; `UnavailableError` is 404 `unavailable`
  and `ConfigurationError` 503 `not_configured` instead of 500; and `limit` is
  declared 1–500 in the OpenAPI schema and refused outside it, not clamped.

### Added

- **A Docker image, and a compose example that runs the whole thing (#22).**
  One image, `ENTRYPOINT ["tubedepth"]`, so the four services in
  `deploy/docker-compose.yml` — `migrate`, `api`, `worker`, `watch` — differ
  only by their `command:`. `migrate` is a one-shot the other three wait to see
  *complete successfully*, because the boot path issues no DDL (#14) and a
  container that migrated on start would put that back. `api` and `worker`
  share one env block through a YAML anchor rather than by review: the listing,
  comment and trending caps are part of the cache key, and two processes that
  disagree about them answer different questions. The image carries no
  `HEALTHCHECK` — it would be wrong for a worker with no endpoint and for a
  one-shot that is supposed to exit — so the API's lives in the compose file.
  The database is the external fleet one by default; `--profile local` brings
  up a PostgreSQL bootstrapped by `deploy/postgres-bootstrap.sql` itself.
  `just compose-up` is the whole command. No registry publishing.

- **`tubedepth watch <list>` — one schedule that collects by channel, by trend
  keyword and by region (#20).** The list is typed: `video`, `channel`,
  `search` or `trending`, then the target, one directive per line. A bare-id
  list could not express this — `UCxxx`, `kpop debut` and `KR` are three target
  types that no inspection of the string separates — so the type is written
  down, and a directive that is not one of the four is refused naming the line
  rather than quietly collecting nothing. Every job it queues is forced past
  the freshness window, with no per-line flag: a listing line is re-enumerated
  so new videos appear, while the per-video follow-ups it fans out to stay
  cache-governed. **A `channel`, `search` or `trending` line is up to
  `TUBEDEPTH_LISTING_LIMIT` collections, not one** — `deploy/watchlist.example.txt`
  has the arithmetic. `deploy/tubedepth-watch.timer` runs it hourly; `--every
  SECONDS` stays resident instead, for the environments with no timer, and
  re-reads the list on every pass so an edit needs no restart. A first pass
  that cannot read its list exits non-zero; a later one is logged and skipped,
  because a half-finished edit must not be what stops collection.

- **`tubedepth transfer --from <url> --to <url>`, and `tubedepth.transfer.transfer()` behind it.**
  #15 and #24 both specify the PostgreSQL cutover's data move as one line —
  "Data across: six tables" — and until now nothing in this repository has
  ever moved a row from one database to another; the alternative was a
  `pg_dump`-and-hope run by hand against 248 targets whose repeated
  observations cannot be re-collected at any price. The transfer is
  model-driven rather than a dialect-level dump: every row is read through
  the ORM and reconstructed on the target column by column, which is what
  preserves `identifier` primary keys and `fetched_at` verbatim to the
  microsecond, and — the failure a `pg_dump` cannot see coming — routes every
  instant back through `UtcDateTime`, which refuses a naive datetime rather
  than letting SQLite's timezone-less storage silently become the wrong
  instant under PostgreSQL's `timestamptz`. It refuses a target that already
  holds any rows, since `artifacts` deliberately carries no unique constraint
  on `fingerprint` and a partial second run would duplicate every
  observation with nothing to catch it. It never imports the payload store —
  rule 7 of `docs/shared-postgres.md` treats the index and the payload bytes
  under `TUBEDEPTH_DATA_DIR/payloads` as one recovery set, and this moves
  only the index half of it. `--dry-run` counts every table in the source
  and writes nothing, so an operator sees six numbers before committing to a
  cutover.
- **A sampler, so a history starts accumulating.** `tubedepth-sample.timer` in
  `deploy/` forces a re-collection of a watch list every hour; the list is
  `~/.config/tubedepth/watchlist.txt`, one video id per line, and
  `deploy/watchlist.example.txt` shows the format. Off unless you enable it.
  Velocity is the difference between two observations and observations only
  accumulate in real time, so this is worth starting before anything needs it.
  **Superseded before this release by `tubedepth watch` and the
  `tubedepth-watch` unit pair, above** — the reason it exists is unchanged,
  the format and the units are not.
- **`tubedepth enqueue --refresh` and `--from-file`.** The first puts the same
  forced collection on the command line that `POST /v1/jobs` has; the second
  reads targets one per line, so a schedule points at a list instead of
  carrying thirty ids on its `ExecStart`. A list that cannot be read is refused
  rather than treated as empty.

### Added

- **`tubedepth pause` and `tubedepth resume`.** The same row `PATCH
  /v1/control` writes, without needing the API — which was the wrong thing to
  depend on: if the API is down or was never installed, the worker is the
  process you most want to be able to stop and the one you could not.
- **`TUBEDEPTH_LISTING_LIMIT` and `TUBEDEPTH_COMMENT_LIMIT`.** The 100-item
  listing cap was a constructor default frozen at registration, so a channel
  with more videos than that could not be collected in full without editing the
  source. Safe to raise now only because the cap is in the cache key: doing it
  before that would have served a cached 100-item listing for a request that
  asked for 1,000. **Set them identically in the API and worker units** — each
  process reads them once, and `GET /v1/sources` reports the effective values
  so the two can be compared.
- **The schema answers the questions it was already recording.** Four columns
  were written on every relevant operation and read by nothing.
  `last_error_message` now reaches `/healthz` and the dashboard — the code says
  `parse_mismatch`, the message names the renderer that stopped matching, and
  only one of those tells you what to change. `api_key_id` and `claimed_by`
  reach `GET /v1/jobs`, so "which client is running away" and "which worker is
  stuck on this" no longer mean opening SQLite by hand. `tubedepth key list`
  says when each key was last used, which is the question anyone asks before
  revoking one.
- **`GET` and `PATCH /v1/control` — an operator can pause the worker.** The API
  and the worker are separate processes on purpose, so nothing in one can reach
  into the other; the control is a row the worker reads at the top of each
  drain, which the restart loop turns into a pause that takes effect in about
  ten seconds. Pausing means claim nothing: queued jobs stay queued, nothing is
  failed on the way in, and a job already running finishes — the extraction in
  flight keeps spending requests until it is done. The dashboard has the button.
- **`/healthz` reports what each route is allowed.** The rate controller's
  state lived only in the worker's memory, so a quarantined lane and an empty
  queue looked identical from outside. `window`, `in_flight` and a wall-clock
  quarantine deadline are now written by the worker and shown on the dashboard.
- **`POST /v1/jobs/batch`.** One kind, many targets, one request — and one
  charge against the sixty-a-minute allowance, which is the difference between
  an API that can express a hundred-video sweep and one that can run it. All or
  nothing: every target is normalised before anything is queued, so one bad id
  refuses the batch rather than queueing ninety-nine and answering 202. Targets
  already held come back named with their digest rather than their payload.
  The dashboard uses it whenever more than one target is given.
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
  startup repair, so a running deployment does not break. Alembic's version
  table does stay behind, and the next `tubedepth migrate` then fails with
  `duplicate column name` — see `docs/troubleshooting.md`, which says how to
  tell whether the answer is `--stamp` or an upgrade.

### Removed

- **`deploy/tubedepth-sample.{service,timer}` (#20).** Replaced by
  `deploy/tubedepth-watch.{service,timer}`, which runs `tubedepth watch`
  against the typed list instead of `tubedepth enqueue video.metadata
  --from-file … --refresh` against a bare-id one. Once `watch` exists the
  sampler pair has no recommended caller, and `decisions/003` is about exactly
  that. The new pair is still a timer plus a one-shot, for the reason the old
  one was. `enqueue --from-file` and the bare-id format it reads are unchanged
  and stay — the file `watch` reads is a different file.
- **SQLite support (#15).** The cutover completes: `Database` refuses any URL
  that is not PostgreSQL, with one deliberate exception —
  `tubedepth transfer --from` still accepts a SQLite source, because that is
  what a real cutover moves data out of. `TUBEDEPTH_DATABASE_URL` has no
  fallback any more and is required; a checkout with nothing configured now
  gets a named refusal instead of a `var/tubedepth.db` it never asked for.
  `psycopg[binary]` moved from an optional extra into `dependencies`.
  Deployment units gain a mandatory `EnvironmentFile` for the URL.
  `tool/doctor.sh`'s SQLite version check became a PostgreSQL reachability
  check. `docs/troubleshooting.md`'s SQLite entries are kept, marked
  historical.

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
