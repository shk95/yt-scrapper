# Status and handover notes

Rewritten freely as the project moves. Anything that must survive a rewrite —
an error and its fix — belongs in [`troubleshooting.md`](troubleshooting.md).

Last updated: 2026-08-19.

---

## Where things stand

Everything the plan called M0–M9 is built, plus a dashboard it did not ask for.
Two kinds it specified are deliberately absent, each with its reasons recorded
below: `video.dislikes` was removed, and `channel.profile` was cancelled when
its content turned out to arrive in a response another source already makes.

| Area | State |
| --- | --- |
| Collection — 11 kinds | metadata, transcript, comments, sponsor segments, related, community, about, channel videos, playlist items, search, bundle |
| Queue | claim, retry, backoff, lease reaping, lease renewal, cancellation, cost lanes |
| Rate control | AIMD per (egress, lane), quarantine, verdict classification |
| Storage | content-addressed gzip blobs, artifact index, age retention, orphan sweep |
| API | jobs, artifacts, results, sources, health — behind `X-API-Key` |
| Dashboard | `/`, self-contained, reads the same `/v1` routes |
| Health | per-source status written by the worker, read by the API |
| Delivery | signed webhooks on job completion |
| Deployment | systemd user units, Alembic migrations |
| Egress pool | deferred; its case left with the dislikes source — GitHub milestone 1 |

The plan is kept at [`plan.md`](plan.md) as history. Where it and this file
disagree, this file is right.

The collection path works end to end against real YouTube:

```
tubedepth enqueue video.metadata dQw4w9WgXcQ nfgdJyL-Jmg
tubedepth enqueue video.transcript dQw4w9WgXcQ
tubedepth enqueue video.comments dQw4w9WgXcQ
tubedepth work        # 4 jobs, 29s: 26KB + 16KB metadata, 10KB transcript, 97KB comments
```

Collected and verified by hand: 27 tags on one video and 13 on the other (the
Data API returns these only to the owner), 11 chapters, 100 ranked
most-replayed buckets, 61 timed caption segments, and 200 comments threaded
into 24 top-level and 176 replies with the pinned, hearted and verified flags.

M0 was verified rather than assumed, on 2026-08-18:

```
tool/doctor.sh                                    ✓ ready (sqlite 3.46.1, hooks on)
tool/doctor.sh, core.hooksPath unset              exit 1, names the fix command
.githooks/commit-msg  "bad message"               exit 1
.githooks/commit-msg  "feat(egress): ..."         exit 0
.githooks/commit-msg  84-char subject             exit 1
tool/checks/{format,lint,test}                    all pass, 3 tests
tool/checks/test with no uv on PATH               exit 69 (unverified)
  ... plus REQUIRE_NATIVE=1, as CI sets           exit 1 (failure)
```

**Measured, 2026-08-18.** Forty metadata jobs, same videos each run:

| concurrency | wall clock | jobs/hour |
| --- | --- | --- |
| 1 | 59.4 s | 2,424 |
| 4 | 25.5 s | 5,645 |
| 8 | 17.1 s | 8,417 |

**The rate controller is now the limiter, and that was verified rather than
assumed.** Holding threads at twelve and moving only the AIMD window ceiling:

| threads | window ceiling | wall clock |
| --- | --- | --- |
| 12 | 2 | 32.0 s |
| 12 | 6 | 15.8 s |
| 12 | 12 | 16.9 s |

Throughput tracks the ceiling, not the thread count, so the controller is
demonstrably in the path. The last row is the useful one: past roughly six
concurrent extractions the bottleneck stops being us. That is the number the
plan wanted measured instead of guessed, and `TUBEDEPTH_WINDOW_CEILING` is how
an operator asks for a different one.

**Transcripts, measured 2026-08-19 — and the measurement found two bugs.**
Three sweeps of forty fresh videos each, one channel, concurrency 8:

| sweep | outcome | jobs/hour | what changed |
| --- | --- | --- | --- |
| 1 | 43 jobs, 4 m 55 s | ~525 | as found |
| 2 | 40 jobs, 3 m 43 s | ~648 | requeue no longer spends attempts |
| 3 | 20 jobs, **13 s** | **~5,400** | a video's failings are NEUTRAL to the route |

Sweep 1's timeline is the whole story: twenty-two of forty jobs finished in the
first fifteen seconds, then it fell to roughly one per fifteen seconds and
stayed there. Nothing about YouTube changed during those four minutes. Seven
videos in the batch had captions turned off, and each of those failures was
reported to the controller as throttling, doubling the lane's minimum interval
— 1s, 2s, 4s, 8s, 16s. The tail rate was our own ceiling.

**One channel end to end, measured 2026-08-20.** The first sustained run — 474
`video.metadata` jobs fanned out from one listing, concurrency 8, alongside the
hourly sampler:

| | |
| --- | --- |
| jobs | 474, **0 failed** |
| of those, actually collected | 374 (100 were cache hits the sampler already held) |
| wall clock | 430 s |
| collection throughput | **~3,100 jobs/h** |
| lane at the end | window **6.0 — the ceiling** — quarantine streak 0 |

Three things came out of it, and two were surprises.

*Sustained load is not slower than a short sweep.* Every earlier figure here
came from forty-job runs; this is the first one long enough to be called
sustained, and the window sat at its ceiling throughout with the lane never
quarantined. YouTube did not push back at all. Anywhere this file says
behaviour under sustained load is unmeasured, it now is — for one channel, at
this size.

*That is only sayable because the lane is now recorded.* Before the same day,
the controller's state lived in the worker's memory and "it never quarantined"
would have been a guess. It is in `lane_health`.

*The cap was not the constraint.* The Data API reports 697 videos for the
channel; `channel.videos` returned **474** with the per-listing cap at 700, so
it did not bind. The difference is the `/videos` tab itself, which is what
issue #2 said would happen and deliberately kept as a separate question —
Shorts and past streams are not in that tab at any cap. Raising the limit
widens *how many*, never *what*.

*No geo-blocks, and that is not an answer.* Zero failures carried a country
marker. One Korean beauty channel is a narrow corpus for that question —
region blocking concentrates in music, sport and broadcast clips — so this is
not the measurement milestone 1 asks for, and that milestone stays open.

Sweep 3 had **eight** caption-less videos out of twenty, twice the proportion,
and did not slow down at all. That is the confirmation: the collapse was the
verdict mapping, not the videos and not the address.

A transcript costs one extraction plus one caption fetch, so it should sit
somewhere near the metadata figure rather than an order below it. It now does.

Two lessons worth keeping. The first is that a rate controller with a wrong
verdict mapping is worse than none: it converts content-level disappointments
into a self-inflicted rate limit, and every symptom points at YouTube. The
second is that neither bug was visible in a single job, in the test suite, or
in a ten-job trial — both needed a sweep large enough for the interval to
compound.

**Lane isolation, measured 2026-08-19.** Three comment harvests queued
*ahead* of twenty-five dislike jobs, concurrency 8: every dislike job finished
at 09:30:42–43 and the harvests at 09:30:58, 09:31:03 and 09:31:17. Cheap work
does not queue behind expensive work even when it was asked for later, which is
the M5 criterion. Honest limit of that test: three harvests against eight
slots cannot saturate the worker, so it demonstrates interleaving rather than
proving the expensive-lane cap. Twelve harvests would.

A mixed sweep the same day — 25 dislikes, 25 sponsor-segment lookups and 3
harvests — completed 53 jobs in 38 s with nothing failed.

**The API under a working worker, measured 2026-08-19 — and it found a bug.**
Twelve concurrent clients against a worker running 22 transcript jobs at
concurrency 8:

| endpoint | p99 before | p99 after | |
| --- | --- | --- | --- |
| `GET /healthz` (one COUNT) | 1,434 ms | **19.9 ms** | 72× |
| `GET /v1/sources` (no database) | 335 ms | 539 ms | unchanged, run-to-run noise |
| `POST /v1/jobs` (a write) | 936 ms | 1,466 ms | still serialises, correctly |

The engine emits `BEGIN IMMEDIATE` for **every** transaction, which is what
makes claiming safe — and it made every API read a writer. A route that only
counted rows took the write lock and queued behind the worker. WAL exists
precisely so readers never block writers, and one event handler was opting out
of it on every route.

The comparison against `/v1/sources` is what identified it: that route touches
no database at all and was four times *faster* at the same concurrency, so the
cost was in the database access rather than in the process.

`Database.session(readonly=True)` uses a second engine with no IMMEDIATE hook
and `PRAGMA query_only=ON`, and the read routes take it. Separate engine rather
than a flag so the guarantee is structural: there is no hook to forget, and a
read-only session that tried to write would be refused rather than silently
becoming a writer — which is the one shape that must not exist, since two of
those interleave exactly the way IMMEDIATE was added to prevent.

Writes still serialise against the worker and always will: there is one write
lock in SQLite and no flag changes that. A submission under a busy worker costs
around a second at p99, which is a job-submission endpoint behaving as a job
queue rather than a problem to optimise away. If it ever becomes one, the fix
is Postgres, not tuning.

**The expensive-cost cap, proven 2026-08-19.** Twelve comment harvests queued
*ahead* of twenty-five sponsor-segment lookups, concurrency 8, so the harvests
alone could saturate the worker: `comments ×3 → sponsor_segments ×25 →
comments ×9`. The cheap work went through the middle of the harvest backlog
rather than behind it. The earlier lane test could only show interleaving
because three harvests cannot fill eight slots; this one exceeds the cap
(`int(8 × 0.5) = 4`) and holds.

**Caching, measured 2026-08-18.** The same channel sweep, twice:

| sweep | wall clock | YouTube requests |
| --- | --- | --- |
| cold (101 jobs) | 168.7 s | ~300 |
| warm (101 jobs) | **1.3 s** | **0** |

Throughput against YouTube is capped by YouTube, so not asking twice was the
only large multiplier left, and it is a 129× one on a repeat sweep.

**Two defects in retention, found 2026-08-19 by looking at the real store.**

*Orphaned payloads were unreachable.* `prune` walks artifact rows and deletes
their payloads, so a file with no row could never be found — and those are
produced routinely rather than exceptionally, because `tubedepth collect` takes
no database at all and leaves one behind on every CLI collection. Thirteen were
sitting in the working store. The sweep now walks the store itself, with a one
hour grace period: payloads are written before their rows on purpose, so every
successful collection is briefly an orphan and a sweep without grace would
delete the result of a job still committing.

*The ceiling did not measure the disk.* It summed `byte_count`, which is the
*uncompressed* payload size, and reported 4.5 MiB for a store holding 0.91 MiB
of files. A 50 GiB ceiling would have fired at roughly 10 GiB of real use. Gzip
is the entire reason the blob store exists, so a size that ignores it is not a
size. It now stats the files.

Both errors also ran in the other direction: 261 small files occupied 2.6 MiB
of disk against 0.91 MiB of content, because a 4 KiB block holds a 1 KiB
payload. So the reported figure was wrong high on large payloads and wrong low
on small ones, and the two effects do not scale together. What is measured now
is file bytes; block overhead is left to the filesystem, and the fan-out that
makes it matter is documented in `payload_store.py`.

**Storage is bounded by age, with a 50 GiB backstop.** The ceiling is not a
target and nothing tries to fill it; reaching it means the retention age is too
generous for what is being collected, so `tubedepth prune` reports it and exits
non-zero rather than silently evicting. What `--max-age-days` buys is a bounded
window of history: how a video's counts moved over the last month is a free
by-product of caching, and older than that is not kept.

**`video.sponsor_segments` is on its own lane, and that is the point.** It
never touches YouTube, so its cost comes out of somebody else's budget and the
per-address YouTube tolerance — which is what actually caps this project — is
untouched by it. SponsorBlock publishes no rate figure, so the lane's limits
are discovered by the controller rather than configured.

It was one of two. See "Dislikes were removed" below for the other.

**The InnerTube surfaces are the fragile half, and the tests are shaped
around that.** Nothing reads a fixed path: YouTube reshuffles the containers
around a renderer far more often than it renames the renderer, and a
fixed-path reader returns nothing for that — indistinguishable from a video
with no related videos. Parsers search by renderer name, keep the previous
name in the accepted list so a rollback does not break them in the other
direction, and record which renderer actually matched on the payload as a
canary.

An empty result is accepted only when the response says it is empty. A channel
with no community posts says so with a message renderer; without that marker,
an empty parse raises `ExtractionError` naming what YouTube actually sent.
That is precisely the failure yt-dlp has — it returns an empty list for a
community tab it can no longer read — and an unquestioned empty list is how a
broken scraper stays deployed for weeks.

**What the fixture suite proves, and what it does not.** It proves the parsers
have not regressed against responses recorded on a known date. It proves
nothing about what YouTube is sending now; only `just contract` does that. The
mutation tests are the load-bearing ones: a suite that only ever sees a
passing fixture cannot tell you it would catch a rename, so each parser has a
test that renames its renderer in a copy of the recording and asserts the
parser raises rather than returning nothing.

**The API is a thin layer over the same services the CLI uses.** Every route
builds a request and hands it to CollectionService or the job tables; no
business logic lives under `api/`. That is what stops the two from drifting
into different answers for the same question.

Authentication is wired on the router rather than per handler, so a route
added later is protected by construction. A test walks the OpenAPI document
and asserts every `/v1/` path answers 401 without a key — verified by adding
an unprotected route and watching it fail.

**Dependencies must be defined at module level, not inside the app factory.**
Every module here uses `from __future__ import annotations`, so FastAPI
resolves annotations from the module namespace afterwards. A dependency
defined inside the factory is a local name resolution cannot see, and
`Annotated[Session, Depends(session)]` degrades silently into a required query
parameter — every route then answers 422 about an argument nobody wrote.
Collaborators are read off `app.state` instead.

**Cancellation landed 2026-08-19, and it is deliberately narrow.** A queued
job is cancelled outright — nothing is happening to it, so the state change is
the cancellation. A running job is only *marked*: its extraction is inside
yt-dlp inside a thread, and nothing here can interrupt that.

Measured end to end against a real harvest: cancel requested at 02:41:48, job
settled at 02:42:11. **Twenty-three seconds of requests went out after the
client asked it to stop.** That gap is the honest content of the feature and
the reason a running job is not moved to `cancelled` by the request itself —
saying `cancelled` while requests are still leaving would announce that a cost
had stopped when it had not. `DELETE /v1/jobs/{id}` returns the job, and the
state on it says which of the two happened.

What the mark does buy is real: the job is not retried, and it hands back no
result. The `max_comments` ceiling remains the only mechanism that actually
bounds the cost of a harvest before it starts.

**The artifact is kept, and that was a reversal.** The first version discarded
it — cancel, therefore leave nothing — which is wrong on this project's own
terms. The request to YouTube had already gone out and been paid for; dropping
the result does not un-spend it, it guarantees the next caller spends it again
against the one budget that caps this system. So the *job* is cancelled and
carries no result, while the cache keeps what was fetched, keyed by video
rather than by who asked. Wanting collected data to disappear is a retention
and access-control problem, and those are different mechanisms.

**Lease renewal was missing until 2026-08-19**, and the method to do it had
existed since the lease did — written, tested, and never called. The reaper
returns any `running` job whose lease has expired, so a comment harvest running
for tens of minutes against a fifteen minute lease would be handed to a second
worker while the first was still going: two harvests, one result, twice the
requests against the same address. Exactly the failure the lease was introduced
to prevent, caused by the lease.

The worker now holds a heartbeat thread for the duration of each job, renewing
at a third of the lease so two missed beats still leave margin. A thread
because the work it covers is blocking and inside a thread of its own — nothing
here can ask yt-dlp how it is getting on.

Worth noting how it was found: not by a test, but by grepping for callers while
looking at something else. A method with tests and no callers is invisible to
every check this repository runs.

Lease reaping and retries landed on 2026-08-18 and were verified against a
simulated crash: a job left in `running` by a worker that never released it
was returned to the queue by the next worker to start, and completed. `JobRepository.claim` takes a lease and counts attempts, but nothing
yet returns an expired one to the queue, so a worker killed mid-job strands
its row in `running`.

---

## What is actually running here

Recorded 2026-08-20, because the deployment has diverged from `deploy/` in one
way that nothing else would tell you.

| unit | state |
| --- | --- |
| `tubedepth-worker` | enabled and running |
| `tubedepth-sample.timer` | enabled, hourly |
| `tubedepth-api` | **not installed** — the dashboard and every `/v1` route are reachable only by running `tubedepth serve` by hand |

**The installed worker unit carries `TUBEDEPTH_LISTING_LIMIT=700`; the copy in
`deploy/` has it commented out.** That was set to sweep one 697-video channel
in full. If the API unit is installed later it **must carry the same value** —
`serve` and `work` each read it once, and a disagreement makes the API compute
a different cache key than the worker records, so it stops matching what the
worker writes while still matching rows from before the change.

The sampler's watch list is `~/.config/tubedepth/watchlist.txt`: 100 video ids
from `@director_pihyunjung`, enumerated 2026-08-20. It is operator data and
deliberately outside the repository.

Store as of that day: 1,556 artifacts, 1,938 jobs, two active API keys. The
oldest artifact is about a day old, so **nothing has reached the thirty-day
retention age yet** — the first prune that actually deletes anything has not
happened, and the shared-blob fix landed before it can.

## This machine

Prefer `tool/doctor.sh` over reading this section — the script reports what is
actually here; this table reports what was here when someone last edited it.

WSL2, Ubuntu 26.04, 16 CPU / 15 GiB, kernel 6.18.33.2. Python 3.14.4, uv 0.12.1,
Go 1.26.5, SQLite 3.46.1, `wireproxy` available via nixpkgs at 1.1.3.
**No Docker, no podman, no passwordless sudo.** `systemctl --user` works.
Direct egress is a residential KT line in KR.

---

## Decisions that are expensive to reverse

### PostgreSQL is where this is going, and why

**Decided 2026-08-20.** The other scrapers in this fleet already run on
PostgreSQL, and the intended shape is **one physical database with logical
boundaries per service** — a schema and a role each, not a database each.

*The reason is not performance, and pretending otherwise would set the wrong
priorities.* The rate controller is the binding constraint here, not the
database: the first sustained run held its window at the ceiling with the
quarantine streak at zero, so a faster claim buys nothing today. What decides
it is that **SQLite cannot participate in that architecture at all.** A file has
no story for "a logical boundary inside a shared physical database", so this
service is currently excluded from the structure the rest of the fleet uses.

Two things it does buy, and they are real:

*The write-lock class of bug stops existing.* `decisions/002` records it twice
and this repository produced a third instance on 2026-08-20 — `POST
/v1/jobs/batch` held a write session and called a cache lookup that opened the
write engine, deadlocking a request against a lock it was already holding.
Every new route on SQLite carries that landmine.

*One operational story.* With several scrapers, the one that is different is
the one whose backup gets forgotten and whose restore nobody remembers. That
tax does not shrink with scale.

**The rules for the shared database are in [`shared-postgres.md`](shared-postgres.md).**
Two of them can damage another service — autogenerate dropping tables it cannot
see the models for, and `alembic_version` collisions — so they are not optional.

**Scope, measured.** What is actually bound to SQLite: `database.py` (two
engines, four PRAGMAs, the `BEGIN IMMEDIATE` hook, `_repair_existing_tables`),
`migrations/env.py` (default URL, `render_as_batch`), the URL in `cli.py`, and
three test modules using raw `sqlite3`. Roughly 150 lines.

**The claim is already portable**, which is the part that lowers the risk most.
`JobRepository.claim` is a SELECT followed by an UPDATE guarded on
`state == QUEUED` with a rowcount check. Under PostgreSQL's READ COMMITTED two
workers can pick the same candidate, but the second UPDATE re-evaluates against
the committed row version and matches nothing. `FOR UPDATE SKIP LOCKED` is an
optimisation, not a correction.

**Cutover, not dual dialect** — supporting both doubles the test surface
forever to preserve a property (runs with no server) that fleet consistency has
made unwanted. And **the tests move too**: "production on PostgreSQL, tests on
SQLite" is how a dialect bug ships, which is exactly what the deadlock above
was. Docker is available on this host (see AGENTS.md), so a compose file and a
CI service container are the whole cost.

### The order this is being done in

1. ~~**Check it runs here**~~ — done 2026-08-20, and it found two things. See
   below.
2. ~~**Stop reconnecting six times a minute**~~ — done. The worker was a poll
   loop made of process restarts, which is a local inefficiency against a file
   and a fleet-budget problem against a shared instance. See `Worker.serve`.
3. **[#14](https://github.com/slopindustries/yt-scrapper/issues/14)** — no DDL
   on the boot path. Small, independently useful, and it makes the next step
   smaller.
4. **[#15](https://github.com/slopindustries/yt-scrapper/issues/15)**, the
   cutover, deciding
   [#16](https://github.com/slopindustries/yt-scrapper/issues/16) along the way
   rather than after it.
5. **[#13](https://github.com/slopindustries/yt-scrapper/issues/13)** (a
   bundle's parts bypass every lane but its own). Independent of the database
   and currently dormant — nothing runs bundles, since the sampler collects
   `video.metadata` only. It wakes the moment anything does.
6. **[#3](https://github.com/slopindustries/yt-scrapper/issues/3) route A**,
   the delta layer, where the accumulated history pays.

#13 sits after the migration rather than before it because it is dormant while
SQLite-shaped decisions keep accruing — 2026-08-20 alone added four, one of
them the deadlock.


### Measured 2026-08-20: what the first PostgreSQL run found

Step 1 was meant to be a yes-or-no about `batch_alter_table`. Batch mode was
fine — it is a no-op on a dialect that can `ALTER` in place. Two other things
were not, and neither would have been found by reading.

**A boolean default that only SQLite accepts.** `50ee31ae8b82` added
`refresh` with `server_default=sa.text("0")`, because SQLite refuses
`ADD COLUMN … NOT NULL` without a default. PostgreSQL refuses the literal:
`column "refresh" is of type boolean but default expression is of type
integer`, and there is no implicit cast. `sa.false()` is rendered by the
dialect — `0` on SQLite, `false` on PostgreSQL — so one revision is now correct
on both. **The general form is the thing to keep**: `sa.text()` in a migration
is a dialect assumption written in a place that outlives the dialect.

**The rule in `docs/shared-postgres.md` had a bug of its own.** It said to set
`version_table_schema="tubedepth"` *and* `include_schemas=False`. Doing both
makes autogenerate propose `drop_table('alembic_version')` — alembic excludes
its own version table by comparing the configured schema against the reflected
one, and reflection under a `search_path` reports `None`, so `"tubedepth" !=
None` and the exclusion misses. The setting written down to prevent a spurious
`drop_table` produces one. The role's `search_path` alone already puts the
version table in the service's schema; the two are alternatives, not a pair.
The document is corrected, and the correction is asserted rather than
described.

Both were found by running, and both are now checks with callers rather than
paragraphs: `tests/test_postgres_migrations.py`, run by `just postgres` and by
a CI service container, against the same
[`deploy/postgres-bootstrap.sql`](../deploy/postgres-bootstrap.sql) a
deployment runs. The suite is `-m postgres` and deselected by default, for the
reason `live` is: the offline suite must stay runnable with nothing installed.

**One difference that helps.** PostgreSQL runs DDL inside a transaction, so the
failed chain rolled all four revisions back and left an empty database. That is
the opposite of SQLite, where a partial upgrade is exactly what produced the
`duplicate column name` in [`troubleshooting.md`](troubleshooting.md). After
the cutover, a failed migration is a failed migration rather than a schema
somebody has to reconstruct by hand.

**Unpaid, and worth naming.** Placing tables by `search_path` means a missing
`ALTER ROLE … SET search_path` sends them silently to `public` — the shared
schema, on a shared database. The test asserts where `alembic_version` lands,
which catches it in CI; nothing catches it on a host where someone bootstrapped
by hand. Tracked as
[#16](https://github.com/slopindustries/yt-scrapper/issues/16), to be decided
during the cutover rather than after it.


Three of these have since been paid for and moved to
[`decisions/`](../decisions/README.md), which holds only rules something has
actually gone wrong without. What stays here is reasoning that has not yet cost
anyone anything — recorded so it can be argued with, not presented as a lesson.


### Dislikes were removed, and deleted rather than archived

`video.dislikes` and the Return YouTube Dislike source it called are gone as of
2026-08-19. Restore with `git log -- src/tubedepth/sources/dislikes.py`; the
last commit holding it is the one before this line was written.

**Why it went.** Every other source in this project either returns what a
service actually knows or fails. RYD is the only one that answered confidently
about things it did not know, and the shape of that answer could not be
distinguished from real data:

| queried | rawLikes | rawDislikes | likes | dislikes | views |
| --- | --- | --- | --- | --- | --- |
| Never Gonna Give You Up | 127,979 | 6,606 | 19,341,010 | 517,520 | 1.81 bn |
| a one-day-old news clip | 1 | 0 | 334 | **0** | 7,502 |
| an id that does not exist | null | null | 0 | **0** | 0 |

The two zeros in that column are not the same claim. The middle row means "our
sample is one person and that person did not press dislike"; the bottom row
means "we have never heard of this video, and we created a row for it because
you asked". Both arrive as `dislikes: 0` next to a `likes` figure that *is*
real — measured against YouTube's own numbers, RYD's `likes` and `viewCount`
match to within a refresh lag (19,341,010 vs 19,341,061). A true number sitting
beside a fabricated one, in the same object, with no field to tell them apart.

We dropped `rawLikes`, `rawDislikes` and `deleted` at
`sources/dislikes.py:55`, which is what made it undecidable downstream too: the
evidence a client would need to judge the number was discarded before storage.
That was fixable. What was not fixable is that separating "no data" from "no
video" requires asking YouTube, and this source's entire reason to exist was
that it spends none of the YouTube budget.

So the choice was between shipping a number nobody can qualify and not shipping
it. Not shipping it.

**Why deleted and not moved to an attic.** The archive already exists and it is
git. A second copy in the tree gets no type checking and no tests while
`DataSource` keeps moving, so within a couple of months it is code that no
longer applies, still answering greps, still read by people. That is the
definition of the debt the removal was meant to avoid. The plan's own claim —
that a source is one module, one registration line and one import — is what
makes deletion cheap to undo, and this is the first real test of it: the
removal touched exactly those three places plus tests and prose.

**What went with it.** `Lane.RYD` (no production user left), `DislikeEstimate`,
the recorded fixture, and the 25 stored artifacts with their payload files. The
browser `User-Agent` stayed but its evidence left with the source — see
`troubleshooting.md`; it is now a posture rather than a measured requirement.

**What this costs elsewhere.** The proxy pool's only quantified justification
was RYD's documented 10,000/day. It no longer has one. That is recorded under
"Proxying is deferred" rather than left for someone to rediscover.

### Migrations exist, and the startup repair stays

`tubedepth migrate` runs Alembic; `--stamp` handles the one-time case every
project meets exactly once, a database that predates migrations and would
otherwise be asked to create tables it already has. The working database was
stamped rather than upgraded.

`create_schema` still runs on startup and still adds nullable columns and
missing indexes. That is not redundancy — it is the development path, where the
schema changes several times a day and writing a migration per change would
mean rewriting them all before the first release. Where the two disagree, a
test says so: autogenerate against a migrated database must find nothing to do,
which fails the moment a model changes without a migration. That failure mode
is otherwise invisible, because every developer machine builds from the models
and only the one deployment migrates.

Three decisions in the scaffolding are worth keeping:

*The URL is resolved, not configured.* A URL in `alembic.ini` is a URL in git,
and the first person to run a migration from a checkout points it at whichever
database that file happened to name.

*Custom types render as their DDL types.* `UtcDateTime` is a TypeDecorator —
it refuses naive datetimes and reattaches UTC on load, which is application
behaviour, not schema. Autogenerate emitted `tubedepth.models.UtcDateTime()`
into the migration, which failed for want of an import; adding the import would
have been worse, since every past migration would then depend on a class the
application is free to rename. A migration that breaks when application code is
refactored is one nobody can replay.

*Batch mode is on.* SQLite cannot ALTER most things in place, so without it the
first migration to rename a column or change a constraint fails when it is run
rather than when it is written.

One trap found immediately: `fileConfig` defaults to `disable_existing_loggers
=True`, so running a migration in-process switched off the application's own
logging for the rest of the process. It silenced two unrelated tests, which is
how it was noticed rather than in production.

### `refresh` is a column, because it has to outlive the request

`"refresh": true` was read once, in `POST /v1/jobs`, to skip the API's own cache
check — and then dropped. The `Job` row never carried it, so the worker called
`collect()` with the default and served the job from the cache it had just been
told to bypass. The job succeeded, pointed at a payload collected hours earlier,
and recorded no artifact. Nothing errored, nothing logged a problem, and from
outside it was indistinguishable from a fresh collection.

What that cost is not hypothetical: it is the whole premise of trend detection.
Velocity is the difference between two observations, and a poller running faster
than a kind's freshness window was recording none while reporting success.

*The flag lives on the row rather than being resolved at submission.* The
collection happens in another process, minutes later; a decision made in the
request handler is one the worker never sees. It also means a retry is still a
forced collection, which falls out of the column rather than having to be
arranged — and would have been the next silent version of the same bug.

*It is deliberately not indexed.* The claim filters on `state` and
`scheduled_at` and orders by `scheduled_at, created_at`; nothing asks this
column in a query, so an index on it would be write cost for no read.

*A forced listing does not force its follow-ups.* `--then` turns one listing
into a job per video, so propagating would multiply one flag into a collection
per video on every sweep, out of the one per-address budget everything else
draws on. Nothing needs it yet — the sampler polls a fixed list of videos
directly — so the question is left open rather than settled by whichever
behaviour happened to fall out. The watch list in the trend work should settle
it, with the arithmetic in hand.

*The migration carries a `server_default` and the model does not.* SQLite
refuses `ADD COLUMN ... NOT NULL` outright unless the statement carries a
default, so the migration must supply one — the same rule `Database._add_column`
learned earlier and wrote into its own docstring. Putting it on the model too
would have been tidier and is wrong: `_add_column` renders the column with
`CreateColumn` and then appends its own `DEFAULT`, so a server default on the
model produces `refresh BOOLEAN DEFAULT 0 NOT NULL DEFAULT 0` and the startup
repair fails on the statement. The two paths therefore differ in DDL, which
nothing depends on, and agree in behaviour, which everything does.

### Measured 2026-08-19: the trending chart is alive, `ytsearchdate` is not

Issue #3 lists three routes to trend detection and says which one is real
decides the design of the other two, so both of its unverified claims were
checked before anything was built. Both answers are in.

**`videos.list?chart=mostPopular` still returns data.** `regionCode=KR` and
`regionCode=US` each answer 200 results. So the retirement of the `/feed/trending`
*page* did not take the chart endpoint with it, and route C is available.

That settles issue #3's fork the good way. C spends **Google API quota, not the
per-address YouTube budget** everything else in this project competes for — 1
unit per request against 10,000/day, so a five-minute cadence is ~288 units.
General "what is rising on YouTube" therefore costs nothing that matters, and
routes A and B shrink to what C cannot do: velocity inside a chosen topic.

It also wants its own `Lane`. Google's quota is a different budget from
YouTube's bot tolerance, and riding on `Lane.YOUTUBE` would make one throttle
the other for no reason. Transport goes in `src/tubedepth/egress/` per the
architecture rule.

**`ytsearchdate{N}:` does not exist.** Issue #3 flagged this as remembered
rather than run, and it was wrong. yt-dlp 2026.07.04 answers
`Unsupported url scheme: "ytsearchdate5"`, and its extractor list holds only
`youtube:search`, `youtube:search_url` and `youtube:music:search_url` — there is
no date-sorted search extractor. Plain `ytsearch3:` works, so the failure is the
prefix and not the mechanism.

The obvious workaround does not work either: `search_url` with YouTube's
`sp=CAI%3D` ("sort by upload date") returns the same videos as the relevance
search, so the parameter is being dropped somewhere between us and the results.

**So route B needs building, not calling.** The likely home is this project's
own InnerTube client (`src/tubedepth/innertube/`), which already constructs
search requests and can carry a sort parameter directly instead of hoping
yt-dlp forwards one. That is a different and larger job than the "cheapest
change in this issue by a wide margin" the issue estimated, and it should be
re-estimated before it is scheduled.

### The complete enumeration of a channel already existed and nobody used it

Measured 2026-08-20 on `@director_pihyunjung`, flat extractions, direct line:

| surface | items | requests |
| --- | --- | --- |
| `playlist.items` on the uploads playlist (`UU…`) | **698** | **8** |
| `channel.videos` (the `/videos` tab) | 474 | 16 |
| `/shorts` tab | 216 | 5 |
| `/streams` tab | 3 | ~2 |

The three tabs are pairwise disjoint and all strict subsets of the uploads
playlist. `UU − /videos` is 224: **216 Shorts, 3 past live streams, and 5
entries that carry titles and view counts in the grid and cannot be watched at
all.** yt-dlp reports 698 against the Data API's 697; the extra one is an
offline live entry, so the playlist is complete.

**The uploads playlist is wider *and* cheaper.** It pages a hundred at a time
where the tab pages thirty, so it reaches more with half the requests. That
was the surprise — the assumption going in was that completeness would cost
something.

*No new kind was built, and that is the finding.* `playlist.items` collects
this today with no change at all: `normalize_playlist_identifier` passes a
`UU…` through untouched and `_extraction_target` builds the right URL. Issue
#2 left "the uploads playlist is the wider enumeration" as an open question;
the answer is that it was never closed, only unwritten.

*Tab-stitching was measured and rejected.* `/videos + /shorts + /streams` is 23
requests for 693 items against 8 for 698, and the `/shorts` tab returns
`duration: null` for every entry — throwing away one of the two fields
`ListedVideo` exists to carry.

**What was deliberately not done.** Making `channel.videos` silently *be* the
uploads playlist when handed a `UC…` is strictly wider and strictly cheaper,
and the payload shape does not change — so `just record-payload-shapes` would
pass and CI would say nothing. It would also widen every existing sweep of
every channel by roughly half and start queueing the unwatchable entries. That
is a decision with a record, not a patch, and it has not been taken.

**And the cost is on the other side.** The listing is 8 requests either way;
the fan-out goes from 474 jobs to 698, and at roughly three requests per
metadata job that is about 670 more YouTube requests per sweep. 96% of what it
buys is Shorts: 31% of that channel's catalogue and 9% of its views, median 47k
against 305k, and a fifty-second video has no chapters, a one-sentence
transcript and thin comments. For this channel the answer is no. `--then` has
no predicate, so "enumerate everything, collect metadata for the long ones" is
not currently expressible — that, not a listing kind, is what would make this
worth revisiting.

### The listing cap is a deployment setting, and both units must agree

`channel.videos`, `search.videos` and `playlist.items` stopped at 100 and
nothing at runtime could raise it, so a channel with more videos than that
could not be collected in full. `TUBEDEPTH_LISTING_LIMIT` and
`TUBEDEPTH_COMMENT_LIMIT` now do, read once per process in `default_registry()`.

*The dangerous half was already closed.* Raising the constant alone would have
been a silent wrong answer — the limit was not in the cache key, so re-running a
channel swept an hour earlier would have served the cached 100-item listing for
the request that asked for 1,000, with nothing looking wrong. That is why the
parameters went into the fingerprint first and this came second.

**The remaining hazard is that `serve` and `work` are separate processes.** Each
reads the environment once, and `default_registry()` is `@cache`d. If the two
units carry different values the API computes a different cache key than the
worker records: it stops matching anything the worker writes, *and* keeps
matching rows written before the change and serving them as 200 — a listing
collected at the old cap answering a request for the new one.

Nothing inside one process can detect that, so `GET /v1/sources` reports the
values actually in effect, and comparing that route between the two instances
is the check. The unit files carry both variables commented out together, so
the next person sets them in both places or neither.

*A bigger cap is not free.* `extract_flat` makes a listing one extraction, but
yt-dlp still walks continuations to reach a thousand, and every one of those
comes out of the per-address budget everything else draws on. It is refused
rather than defaulted when it does not parse: an operator who sets it and
silently gets the old behaviour concludes the variable does nothing, and the
sweep they ran is exactly the size they were trying to change.

### The upcast machinery is deferred, and here is what should build it

Issue #4 asks for five things. Four shipped: `Artifact.schema_version` and its
backfill, `GET /v1/artifacts/{digest}`, the invalidate half (`retracted_versions`,
which `channel.about` v1 actually needs), and the CI check that refuses a model
change without a bump. The fifth — a source declaring how to *lift* a payload
from the previous version — was not built, on purpose.

*Because there is nothing to lift.* Measured before deciding: nine sources have
never bumped, `channel.about` is the only one that has, and issue #4 itself says
its honest handling is deletion rather than a lift. `channel.about` had **zero**
stored rows. So `Upgrade` would have shipped with no instance and its tests would
have exercised a `lift` written by the test — the `renew_lease` shape exactly, in
a repository with a decision file about that.

*And because the signature would be a guess.* `Callable[[Mapping], Mapping]` fits
a rename or a default. It does not fit a field split needing data v1 never held,
and it does not fit a semantic change to an existing field. Designing the seam
before the case is how you get a seam the case does not fit.

**Build it the first time all three hold:**

1. **The kind about to bump has stored history.** Checkable, not recalled:
   ```sql
   SELECT kind, count(*) FROM (
     SELECT kind, target, count(*) n FROM artifacts GROUP BY 1,2 HAVING n>1
   ) GROUP BY 1;
   ```
   On 2026-08-20 that returned `video.metadata` and nothing else.
2. **The bump is a shape change, not a correctness fix.** If the old data is
   wrong, `retracted_versions` is already the honest answer and is built.
3. **Something reads across the version boundary** — the trend work's delta
   layer, or a second consumer of `/v1/artifacts/{digest}` that needs one shape.

The tripwire is already armed: `tests/test_payload_shapes.py` fails on the bump
that would need this, and its message names the fork — bump and record, or
retract. A deferral with a tripwire is a plan; without one it is a hope.

*Not in `decisions/`.* That directory's README sets the bar at a cost someone
has already paid and measured. Nothing here has been paid yet.

### The sampler is a timer and a text file, not a scheduler

Nothing in this project ran periodically, so the artifact table was a time
series with nothing taking samples. `tubedepth-sample.timer` fires
`tubedepth enqueue video.metadata --from-file … --refresh` every hour and that
is the whole mechanism.

*It was built now rather than with the feature that needs it.* Trend detection
is the difference between two observations, and observations only accumulate in
real time — no amount of work later produces last week's view count. Everything
else in the backlog can be built faster by adding people or agents to it; this
one cannot, so it starts first and runs while the rest is built.

*Deliberately not a scheduler.* No table of schedules, no watch-list model, no
cadence per target. The trend work will need a real watch list, and it should
inherit a generic worker control channel — the same one an operator pausing the
worker from the dashboard needs — rather than a bespoke table built here that
would then have to be replaced. A timer and a file cost nothing to throw away.

*The list lives outside the repository,* at `~/.config/tubedepth/watchlist.txt`,
next to the WireGuard config and for the same reason: which videos someone is
watching is operator data, not project data, and a checked-in list is one every
clone starts collecting.

*A missing list is a failure, not an empty sweep.* A timer firing hourly at a
file somebody moved would otherwise queue nothing, report success, and leave
the series to stop moving with nothing anywhere reporting a problem — which is
the exact shape of the `refresh` bug above, rebuilt on purpose.

*Sizing is arithmetic, and it is written down so it can be checked.* One line
is one forced collection per firing. A hundred videos hourly is 100 jobs/h
against a rate measured at **~3,100 jobs/h under sustained load** (2026-08-20,
above), so around three percent. That measurement replaced an estimate: the
figure quoted here before was taken from forty-job runs and was low.

The watch list and the cadence are still one decision and should still move
together — the headroom is larger than it looked, not unlimited, and the
sampler competes with every other lane user for the same per-address budget.

### The dashboard reads the same API as everything else

`/` serves one self-contained page: queue counts, per-source health, a
twenty-four hour completion histogram, and a filterable browser over jobs and
artifacts. It calls the same `/v1` routes a script would, rather than private
endpoints — the same rule the CLI follows against the service layer, and for
the same reason. A dashboard with its own data path can show something the API
will not, and then the two disagree about what is true.

**Unauthenticated, and that is the design rather than an omission.** The page
carries no data; it asks the browser for a key and sends it as a header. This
project's auth is a header, so requiring one to fetch the HTML would mean a key
in a URL or a cookie — two places a secret should not be. A test asserts no key
appears in the served page.

**No external references, asserted by a test.** No CDN stylesheet, script or
font. A private tool on a private network cannot assume the internet is
reachable, and an external reference tells a third party when this instance is
being looked at.

Jobs and artifacts are browsed separately because they answer different
questions. The job ledger says what was asked for and what happened to it; the
artifact table appends rather than overwrites, so filtering it by target gives
one video's history — `dQw4w9WgXcQ` currently shows metadata, comments and a
transcript collected on three different days.

Paging is keyset rather than offset: an offset re-reads what it skips and
drifts when rows arrive mid-page, which against a table a worker is actively
writing shows one job twice and misses another. The cursor is opaque base64url,
partly because an ISO timestamp's `+` becomes a space in a query string, and
partly so the ordering columns stay out of the public contract.

### Source health is recorded, because nothing could see it before

The rate controller already knew when a route was in trouble, and that
knowledge lived in the worker's memory and died with the process. Nothing
outside the worker could read it, so an operator asking "why is nothing
arriving" had only the job table — which says what failed, not whether
anything is wrong *now*.

`source_health` is one row per kind, written by the worker as it goes and read
by the API. Per source rather than per lane because that is the unanswered
question: when YouTube renames a renderer, `video.related` fails every call
while `video.metadata` beside it is fine. The lane is healthy; the source is
not.

The statuses distinguish causes that need different fixes: `broken` is our
parser, `blocked` is the address, `degraded` is one bad call, `stale` is a
source nothing has exercised lately, `unknown` is one never tried. That last
one matters more than it looks — a dashboard showing green for something nobody
has run is worse than one admitting it does not know.

**Only two failure classes count against a source**, and the reasoning is the
one the rate controller needed: `ExtractionError` (our parser no longer
matches) and `UpstreamError` (the other end refused). A video with captions
turned off fails `video.transcript` legitimately and repeatedly, and counting
it would paint a working source red during any sweep of such videos.

`/healthz` stays `status: "ok"` while individual sources are not. It is read by
things that restart processes, and one broken parser is not a reason to cycle
an API whose other nine kinds are collecting.

Verified against the running system: three kinds `healthy` after real jobs,
seven `unknown`, and a source failed three times reports `broken` with its last
error code.

### `channel.about` returned a video's description as the channel's

Found 2026-08-19 while answering what `channel.profile` would add, and it is
the worst failure shape this project has had: not an error, not an empty
result, but a plausible wrong answer that every check passed.

The source called `browse` with **no `params` at all**, so YouTube returned the
channel *home* tab. The parser searches by renderer name — correct policy, and
the reason this bit — and took the first `description` in the payload, which on
a channel with a featured video is that video's description. `country`,
`joined_text`, `links` and `name` came back null every time, which is the
entire reason this source exists rather than yt-dlp. Only the subscriber count
was right, because it appears on every tab.

Nothing caught it. The recorded fixture was named `browse-channel-home` and the
regression test asserted only `channel_id` and the subscriber string — the
limitation was written into the test instead of being recorded as a defect.

**What YouTube actually does now:** there is no About tab in the tab list at
all. The plan anticipated stale about-tab `params`; what happened instead is
that the surface moved into an engagement panel, reachable only by following a
`continuationCommand` token from the first response. So the source makes two
calls and reads the token at runtime — a hardcoded token would be a
credential-shaped string that expires, which is the failure it already had.

`parse_channel_about` now requires `aboutChannelViewModel` and raises
`ExtractionError` naming the renderers it saw instead. The home fixture is kept
deliberately as the negative case: it proves the refusal rather than a parse.

**`channel.profile` is cancelled as a separate kind.** The plan gave it its own
source and its own yt-dlp extraction for the channel description, tags, avatar
and follower count. All of those are in `channelMetadataRenderer`, which sits
in the *first* of the two responses `channel.about` already makes. Spending a
second extraction per channel to fetch fields we have already been handed is a
YouTube request bought for nothing, and YouTube requests are the one budget
that caps this system. `ChannelAbout` gained `name`, `tags`, `avatar_url`,
`view_count`, `video_count` and `handle`; the plan's thirteenth kind is not
coming.

`view_count` is worth noting on its own: the channel's lifetime view total,
exact rather than rounded. The plan recorded it as unavailable — yt-dlp reports
`None` — and it is sitting in this panel.

**Handles now resolve.** `@RickAstleyYT` passes identifier validation because
YouTube URLs use handles, and `browse` answered 400 for it: an error about a
request the caller never made. `browse_id_for` spends one `navigation/
resolve_url` round trip, and only when the target is a handle.

### A transcript is the video's own words, or it is nothing

`video.transcript` takes the video's own language and no other:

1. Captions a person wrote in that language.
2. Otherwise YouTube's transcription of it — the ASR track, marked `-orig`
   whenever translations of it also exist.

**Translated tracks are filtered out entirely**, in `_track`, so no future tier
can reach one by accident. Two reasons, and the second is what settles it: a
translation is two lossy steps from the audio, *and* it is drawn from a budget
so small that a sweep spends it in three or four requests. A policy that used
them would return the intended language for the first few videos of a run and
some other language for the rest, with no error anywhere. That makes a
transcript's language a fact about how recently the worker ran rather than a
fact about the video.

The video's language comes from `dump["language"]`, and from the `-orig`
automatic-caption key when yt-dlp reports none. Both are absent together on old
uploads — jNQXAC9IVRw (2005) has no language, no ASR, and two manual tracks with
nothing to distinguish them. That is a real state, not a parse failure, and it
is the *only* place the configured `FALLBACK_LANGUAGES` (`ko`, `en`) applies.
Refusing there would discard captions sitting in plain sight.

Regional tags are matched on the primary subtag, so a video reporting `pt`
takes its `pt-BR` track. Comparing whole tags would treat those as different
languages and fall through to nothing.

Two earlier policies were tried and are recorded here because the reasoning
that killed each is the reasoning that keeps this one. Manual-first *across*
languages ignored the caller's stated priority. Korean-first-always honoured it
by putting every English video on the rationed translation path and quietly
degrading to English mid-sweep. Both were replaced from measurement, not taste.

Measured across the cases after the change: an English video with written
captions yields those; two Korean videos yield `Korean (Original)`; an English
video with none yields `English (Original)`; jNQXAC9IVRw yields its English
track through the fallback. No request carries `tlang=` any more.

`TranscriptSource.collect` still walks the candidates and steps past an
`UpstreamError`, because the written track and the transcription are both the
video's own words and returning the lesser one beats failing. `is_automatic`
reports which arrived. The last failure is re-raised when every candidate
refuses, so a genuinely blocked address still fails the job.

Asking for a transcript does not fetch several. One job yields one track. Per
language fan-out would be a second kind, not a parameter — the fingerprint
covers kind and target only.

**`yt-dlp` is pinned with no upper bound.** Every other dependency is capped
(`pydantic>=2.9,<3`). yt-dlp breaks *forward* — when YouTube changes, an old
version stops working — so a cap converts the standard fix (`just update-ytdlp`)
into "edit pyproject.toml first". Reproducibility comes from `uv.lock`, which
pins the exact version; the cap was never what provided it. Undo this only if a
yt-dlp release ever breaks *us* in a way an upgrade cannot fix.

**The queue is SQLite, not a broker.** Celery/arq/dramatiq all need Redis or
RabbitMQ, which on a host with no container runtime becomes an undocumented
prerequisite for every clone. The structural argument, though, is that the queue
table *is* the API's read model: `GET /v1/jobs/{id}` reads the row the worker
wrote, so the dual-write inconsistency a broker introduces cannot exist. All SQL
about state transitions lives in `JobRepository.claim()` so that a move to
Postgres is one method. Undo when a second machine needs to run workers.

**Comment harvests run yt-dlp as a subprocess; everything else uses it as a
library.** The difference is cancellation: a blocking call inside a thread
cannot be interrupted, so a cancelled six-minute harvest would keep burning
quota after the client gave up. A subprocess takes SIGTERM, and it keeps a 50 MB
comment payload out of the API process's heap. The cost is ~0.5 s of interpreter
startup, which is free next to the harvest. Undo if yt-dlp ever grows a real
abort hook.

**Proxying is deferred, and its case has since evaporated.** Proton VPN exits
are datacenter address space, which is what YouTube's bot check targets, and
the direct line here is a residential KT connection that currently works. The
pool's one quantified case was Return YouTube Dislike's documented 10,000/day
— roughly 400 an hour per address, times however many exits — and that source
has been removed. SponsorBlock publishes no figure to argue from, and the
caption-translation budget that briefly looked like a second case stopped
applying when transcripts stopped requesting translations.

So the honest position on **throughput**: nothing currently measured gets
better by adding egresses, and the YouTube lane is expected to get *worse*,
since a VPN exit is the datacenter address space the bot check targets.

One argument survives, and it is a different kind. **Geo-blocked videos are a
capability problem, not a throughput one**: a video blocked here cannot be
collected here at any rate, so exits are the only mechanism and rate limits are
irrelevant to it. It needs geographic diversity — two or three countries — not
ten addresses, which is a far smaller build than the plan assumed, and it is
measurable today because `not made this video available in your country` is
already classified as `UnavailableError`.

The conditions for revisiting all of this live in GitHub milestone 1, and the
measurement that decides the surviving argument is issue 1. Both were written
so the next attempt starts from evidence rather than from this plan's original
assumptions, which have not survived contact with measurement.

**Retention protects nothing on the grounds of being the last of its kind.**
An earlier design kept the newest observation of each question regardless of
age, so a stale answer would beat none. It would not: the cache filters on
`fresh_until`, so a month-old artifact is never served. Protecting it bought no
cache hits and cost unbounded growth — the store would have grown with the
number of distinct things ever collected rather than with what is current.

**Stored datetimes are aware UTC on both sides of the database.** SQLite has no
timezone, so a value written as aware reads back naive, and a naive datetime
does not raise on comparison — it silently compares wrong. `UtcDateTime` puts
the offset back on load and refuses to store a naive value, which is the only
place that can be fixed once rather than at every call site.

**The worker collects through `CollectionService` rather than its own copy.**
It had a near-identical `_collect`, which meant the CLI consulted the cache and
the queue did not — and the queue is the side running a hundred jobs
unattended. Unifying them also revealed that the worker had been skipping
target normalization entirely.

**Egress rate control is keyed on `lane`, not on `backend`.** What rate-limits
us is a *service*, not our internal taxonomy: yt-dlp, InnerTube and caption
`json3` GETs all draw on the same per-IP Google tolerance, while SponsorBlock
has its own budget and its own 429. Routing eligibility is still keyed on
backend. Collapsing the two axes would let a third party's limit throttle
YouTube work, and would leak caption fetches out of the YouTube budget so the
measurement stops being true. That mattered more when there were two third
parties; the axis is kept because the next one will not be free either.

**stdlib `logging` in the worker, sources and services — a deliberate departure
from the house style.** No sibling repository uses a logging library; output is
`typer.echo` with the `→ ✓ ✗ ·` vocabulary. That breaks for a process running
for days under systemd: `typer.echo` from a background asyncio task has no
timestamp, no level and no ordering, and "which of the 400 harvests overnight hit
the bot check" is then unanswerable. Interactive CLI output is unchanged. Undo if
the worker ever stops being long-lived.

---

## Bugs worth remembering

**`table jobs has no column named api_key_id`.** `create_all` creates tables it
cannot find and leaves tables it finds alone, so every column added after a
database file exists is missing from that file for good. The gap does not show
up at startup — `create_schema()` reports success — it shows up at the first
INSERT, inside a worker, long after the change that caused it. Any development
database predating the API-key commit hit this. `Database._repair_existing_tables`
now adds nullable columns and refuses anything else by name; it is not a
migration tool and does not pretend to be one (nothing renames, drops or
backfills). Alembic is still the real answer and is still unbuilt.

**CI was red at step one, and had been since the checks were written.**
`tool/checks/format` runs `uv run ruff format --check`, and `uv run` installs
the project's dependencies into a missing virtualenv but *not* its extras —
ruff is a dev extra. So the first check on a fresh runner exited 2 with
`Failed to spawn: ruff`, and because it was the first step, `verify` never
reached the tests: **the suite was not running in CI at all**, on any push,
while every commit message here said it was green. It passed on every laptop,
which is exactly why it lasted — a laptop has already run `tool/checks/test`
once, and that one does sync. The tell was visible the whole time and looked
like noise: `gh run list` showed `failure` on every push for a fortnight.
Fixed 2026-08-20 by syncing in each check, with the invariant asserted in
`test_repository_hygiene.py`, since the next check will be written by copying
one of these and the sync line is the part that looks like boilerplate. **The
general form: a check that cannot fail loudly is indistinguishable from a check
that passes, and the first green run is the only proof either way.**

**Reflection deadlocked against the repair.** The first version of that repair
used `inspect(engine)` and then `engine.begin()` — two connections, and *every*
transaction here is `BEGIN IMMEDIATE`, so the reflecting one held the write lock
while the altering one waited five seconds for it and raised `database is
locked`. The engine-wide IMMEDIATE is worth this: anything that reads and writes
in one operation has to do both on one connection.

---

## Traps that will recur

Everything that has already cost time lives in
[`troubleshooting.md`](troubleshooting.md). **This file holds state and
decisions; that one holds findings**, so updating the state does not delete them.

Already seeded there: the third-party browser User-Agent finding, YouTube's
bot check, the
drvfs WAL problem, and the pysqlite deferred-transaction lock upgrade.

---

## Next

Everything the original plan called M0–M9 is built. Eleven kinds collect real
data; the queue claims, retries, reaps, renews, cancels and calls back; the API
serves it behind a key with a dashboard on top; systemd units and Alembic
migrations exist. `channel.profile` was cancelled with reasons recorded, and
`video.dislikes` was removed with reasons recorded.

**Planned and not built, deliberately.** The convenience aliases —
`/v1/videos/{id}/metadata` and friends, with `GET` meaning cache-only and
`POST` meaning ensure — are absent. The README advertised one of them in its
first example for a day, against a route that never existed. The `GET`/`POST`
split is the part worth having if they are ever added: it lets a client ask
"have you got this" without being able to trigger a fetch by accident.

**Never verified.** Two of the four entries this list opened with are now
answered, and the two that remain are GitHub issues rather than paragraphs here
— a list in a status file is not a thing anyone is assigned.

*Answered.* Sustained load is measured: 474 jobs, 0 failed, 430 s, **~3,100
jobs/hour**, the lane window at its ceiling and the quarantine streak at zero.
That replaces the ~2,150 figure quoted from sweeps of 20–50. And the units have
run — both were broken the first time they were enabled (`status=127` from a
PATH systemd does not inherit, then `Read-only file system` from
`ProtectHome=read-only` over `~/.cache/uv`), which is the argument for enabling
a unit rather than verifying it.

*Open.* **[#17](https://github.com/slopindustries/yt-scrapper/issues/17)** —
retention has never deleted for age; the first real run is currently scheduled
for whenever the store turns 30 days old, which is the wrong way to find out.
**[#18](https://github.com/slopindustries/yt-scrapper/issues/18)** — nobody has
followed the README from a fresh clone. That one is overdue: CI was red at its
first step for a fortnight for exactly the reason a README walkthrough exists to
catch, and it passed on every laptop the whole time.

**Where the work is tracked.** Milestones hold the two pieces of work large
enough to have an order: *PostgreSQL: join the fleet's shared database*
([#14](https://github.com/slopindustries/yt-scrapper/issues/14),
[#15](https://github.com/slopindustries/yt-scrapper/issues/15),
[#16](https://github.com/slopindustries/yt-scrapper/issues/16)) and *Trends:
answer what is rising* ([#3](https://github.com/slopindustries/yt-scrapper/issues/3),
where routes B and C are built and route A — the delta layer — is what is
left). This file keeps the reasoning; the issues keep the state, so that
updating one does not silently contradict the other.

**Known and deliberate.** The rate controller's state is still per process and
per run: a restart forgets which routes were in trouble, and two workers do not
share a window. The plan's `egress_health` table would fix both and there is
one worker, so it has not been worth the write-behind machinery. The dashboard
shows source health, not egress health, for the same reason.

**Deferred with a reason:** the egress pool. Its quantified case left with the
dislikes source. GitHub milestone 1 holds the conditions for revisiting it, and
issue 1 is the one measurement that could revive it.
