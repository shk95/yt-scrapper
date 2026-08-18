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
| M4 — dislikes, SponsorBlock | done |
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

**Third-party sources are on their own lanes, and that is the point.**
Neither `video.dislikes` nor `video.sponsor_segments` touches YouTube, so their
cost comes out of somebody else's budget and the per-address YouTube tolerance
— which is what actually caps this project — is untouched by them. Return
YouTube Dislike documents 100 requests a minute and 10,000 a day; SponsorBlock
publishes no figure.

Dislike numbers are labelled estimates in the model itself (`is_estimate`,
`source`), not only in the documentation. They are reconstructed from an
archive plus extension telemetry, and a field called `dislikes` sitting beside
a real `likes` invites exactly the wrong reading.

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

### Caption selection ranks language first, Korean first of all

`video.transcript` prefers Korean, then English, and **language outranks
provenance**. The first requested language the video has any track for wins;
only then does the best track *within* that language get picked — a manual one,
else the original transcription (yt-dlp's `-orig` key), else the translation
into that language.

The consequence, stated plainly because it is the part that looks wrong in a
log: an English video whose uploader wrote captions by hand returns the Korean
machine translation of the machine transcription instead. Both lossy steps are
real. It is still the right answer here — a faithful transcript in a language
the reader cannot read is worth nothing — and it is a decision, not a fallout.
The opposite rule (manual-first across languages) was written first and
overruled.

Reversing it is one loop: rank the tiers outside the languages rather than
inside. `test_korean_is_taken_ahead_of_a_manual_english_track` pins the current
direction and fails the moment that loop is inverted.

The `-orig` tier is worth keeping under either order. Plain `ko` on an English
video is translated; `ko-orig` on a Korean video is the transcription itself,
and preferring the bare key takes a needless round trip through the translator
on every Korean video.

### The ranking is a preference, and the fetch is allowed to refuse it

**Auto-translated caption tracks (`tlang=`) draw on a separate, small,
per-address budget.** Measured, in this order, on the direct line:

| Observation | Result |
| --- | --- |
| Plain manual track, 6 in a row | 200 × 6 |
| Translated track, 3 in a row (rested ~30 min) | 200 × 3 |
| Translated track, next request | **429 on the 2nd** |
| Plain + ASR track immediately after that 429 | 200, 200 |
| Full metadata extraction immediately after | succeeded in 1.2 s |
| Translated track **of a different video**, first ever request | **429** |

Three things follow, and only the first was guessed correctly at first:

1. The budget is **separate**. A 429 on a translation does not mean the address
   is in trouble: plain tracks, ASR tracks and yt-dlp extraction all kept
   working in the same second. Treating it as a lane-wide throttle would be a
   large overreaction to a small, local refusal.
2. The budget is **per address, not per video**. A fresh video's first
   translation request is refused while another video's is exhausted. So a bulk
   sweep does not get a few translations per video — it gets a few in total,
   then none for a while.
3. It **refills on the order of half an hour**, not seconds.

An earlier version of this section said translations are "throttled far
harder", from one loop where they answered 429 four times out of four. That
loop ran on a budget the same session had already spent. The endpoint is not
permanently stingy; it is separately and cheaply exhaustible. The distinction
matters because the first story argues for avoiding translations and the second
argues for spreading them across addresses.

So `TranscriptSource.collect` walks the ranked candidates and drops to the next
on any `UpstreamError` instead of failing the job, and the job then reports
success — which is right, because nothing about the address is wrong.

What this costs, and it is the honest limit of Korean-first at scale: **on
English videos a Korean-preferring sweep degrades to English after the first
few, silently.** Each such job also spends one refused request before falling
back. `language`, `name` and `is_automatic` in the payload are therefore load
bearing — a client that assumes Korean because it asked for Korean is wrong.
The last failure is re-raised when every candidate refuses, so a genuinely
blocked address still fails the job rather than quietly returning nothing.

This is also the first measured argument for the egress pool that has nothing
to do with RYD: the translation budget is per address, so exits multiply it
where they cannot multiply YouTube extraction.

Asking for two languages does not fetch two transcripts. One job yields one
track. Per language fan-out would be a second kind, not a parameter — the
fingerprint covers kind and target only.

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

**Proxying is deferred, not forgotten.** Proton VPN exits are datacenter
address space, which is what YouTube's bot check targets, and the direct line
here is a residential KT connection that currently works. The pool's real case
is Return YouTube Dislike's documented 10,000/day, and that source does not
exist yet. Revisit when collection actually starts getting blocked, or when
the third-party sources land.

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
`json3` GETs all draw on the same per-IP Google tolerance, while RYD and
SponsorBlock each have their own budget and their own 429. Routing eligibility
is still keyed on backend. Collapsing the two axes would let RYD's documented
100/min throttle SponsorBlock, and would leak caption fetches out of the YouTube
budget so the measurement stops being true.

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

Already seeded there, from measurements taken while planning: the RYD browser
User-Agent requirement, RYD's documented daily cap, YouTube's bot check, the
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
