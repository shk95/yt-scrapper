# Status and handover notes

Rewritten freely as the project moves. Anything that must survive a rewrite —
an error and its fix — belongs in [`troubleshooting.md`](troubleshooting.md).

Last updated: 2026-08-18.

---

## Where things stand

| Milestone | State |
| --- | --- |
| M0 — repository skeleton | done |
| M1 — domain core | done, except lease reaping and cancellation |
| M2 — first yt-dlp source | done (video.metadata) |
| M4 — transcripts | done (video.transcript); third-party sources not started |
| M5 — comments | done (video.comments) |
| queue wired end to end | **done** — enqueue → work → jobs collects for real |
| M6 — discovery | done (channel.videos, search.videos, playlist.items) |
| worker concurrency + AIMD wired | done |
| caching + dedup + retention | done |
| M4 — SponsorBlock | done (dislikes removed, see below) |
| M7 — InnerTube trio | done |
| M3 — HTTP API and auth | done |
| M4.5 — egress pool | deferred; see "decisions" below |

Nothing under `src/tubedepth/` exists yet beyond the package marker. The plan
this is being built from lives outside the repository at
`~/.claude/plans/encapsulated-herding-dolphin.md`.

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

**Caching, measured 2026-08-18.** The same channel sweep, twice:

| sweep | wall clock | YouTube requests |
| --- | --- | --- |
| cold (101 jobs) | 168.7 s | ~300 |
| warm (101 jobs) | **1.3 s** | **0** |

Throughput against YouTube is capped by YouTube, so not asking twice was the
only large multiplier left, and it is a 129× one on a repeat sweep.

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

**Still not done in the queue:** cancellation. `DELETE`-style stopping of a
running job does not exist, so a comment harvest started by mistake runs to
completion.

Lease reaping and retries landed on 2026-08-18 and were verified against a
simulated crash: a job left in `running` by a worker that never released it
was returned to the queue by the next worker to start, and completed. `JobRepository.claim` takes a lease and counts attempts, but nothing
yet returns an expired one to the queue, so a worker killed mid-job strands
its row in `running`.

---

## This machine

Prefer `tool/doctor.sh` over reading this section — the script reports what is
actually here; this table reports what was here when someone last edited it.

WSL2, Ubuntu 26.04, 16 CPU / 15 GiB, kernel 6.18.33.2. Python 3.14.4, uv 0.12.1,
Go 1.26.5, SQLite 3.46.1, `wireproxy` available via nixpkgs at 1.1.3.
**No Docker, no podman, no passwordless sudo.** `systemctl --user` works.
Direct egress is a residential KT line in KR.

---

## Decisions that are expensive to reverse

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

Verify the clone per project-scaffold `decisions/006` — follow the README in
order, using nothing you happen to know. That check always finds something.

Then either finish the queue's unfinished half (lease reaping, cancellation,
retries) or start M3, the HTTP API over the same service layer. The API is the
larger user-visible step; the reaping is the thing that will bite first in
unattended use.

M1 built the domain core with **zero network**, and the egress package
skeleton lands with it — `Egress.build`, `DirectEgress`, the selector and the
AIMD controller are all pure logic and fully testable offline. Landing the
"every transport comes from an egress" invariant on day one is cheap;
retrofitting it after five sources exist is a rewrite.
