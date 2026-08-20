# Things that already cost someone an afternoon

Append-only. This file must never be rewritten to reflect the current state —
that is `status.md`'s job, and mixing the two is how findings get deleted.

**Search this file by the error text, not by reading it.** Headings are the
literal message you will see, so `grep` finds the entry that matches what is in
front of you.

**Bar for adding an entry:** it cost more than a quarter of an hour to work out,
*and* it will happen again.

---

## yt-dlp

### `Sign in to confirm you're not a bot`

YouTube's bot check. It is about the **address**, not the video: datacenter and
VPN IPs are its primary target, residential ones far less so.

Do **not** retry — automatic retry is how a soft block becomes a hard one. The
egress controller classifies this as `BLOCKED`, quarantines that egress only,
and retries on a different one. If it happens on `direct`, the whole YouTube
lane is affected and `/healthz` will say so.

First fix is always `just update-ytdlp`. Second is reading yt-dlp's issue
tracker. Debugging this project comes third.

### `ModuleNotFoundError: No module named 'yt_dlp'`

The `yt-dlp` on `PATH` is a separate isolated install and is *not* the one this
project uses. Everything here runs through `uv run`, which uses the version
`uv.lock` pins. Run `uv sync --extra dev --frozen`.

This also matters for fixtures: they are recorded against the locked version,
so invoking the `PATH` binary produces output that may not match.

### `published_at` is null for a video that plainly has an upload date

yt-dlp stopped returning `timestamp` for at least some videos on 2026-08-18 —
three consecutive live extractions of `dQw4w9WgXcQ` came back with
`timestamp=None` while `upload_date='20091025'` stayed. The exact instant is
the field the official Data API cannot give, so it is worth having, but it is
genuinely sometimes absent.

`published_date` carries the coarse date and is populated from `upload_date`
whenever it is there. Do not "fix" this by deriving the instant from the date;
midnight UTC is not when the video went up, and a fabricated instant is worse
than an absent one.

Fixtures recorded before that date still carry `timestamp`, and the parser
handles both shapes on purpose — a parser that only handled the newer one
would break on every recording and on every video YouTube still answers fully.

---

## SQLite

### `database is locked`

Two causes, and they need different fixes.

**The database is on `/mnt/c`.** WAL needs real POSIX locking and drvfs does not
provide it reliably. Move the database onto the Linux filesystem;
`tool/doctor.sh` checks this.

**A deferred transaction tried to upgrade to a write lock.** pysqlite opens
transactions as deferred reads and upgrades on the first write, and a *failed
upgrade* raises `SQLITE_BUSY` immediately, ignoring `busy_timeout` entirely.
This is why the engine is created with `isolation_level=None` and the job claim
issues its own `BEGIN IMMEDIATE`. If you see this from new code, that code is
writing inside a transaction it opened by reading.

---

## Third-party services

## `duplicate column name: …` from `tubedepth migrate`

Alembic is behind the schema: the column is already in the database, but
`alembic_version` does not know it. The boot path issues no DDL any more
(#14) — nothing opens the database and quietly adds a column any more — so
this is no longer something a running deployment causes on its own between a
model change and the migration. It still happens to a database that was
opened by the *old* code before #14 shipped, back when every boot repaired
missing columns and left `alembic_version` untouched, or to a database that
predates migrations entirely and was built with `Database.create_schema()`
outside `tubedepth migrate`.

Check what is real before doing anything:

```sh
uv run python -c "
import sqlite3; c = sqlite3.connect('var/tubedepth.db')
print(c.execute('SELECT * FROM alembic_version').fetchone())
print(sorted(r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")))"
uv run alembic heads
```

If the schema already matches the head — the tables and columns are all there,
which is what the startup repair does — then the fix is to record that rather
than to replay it:

```sh
uv run tubedepth migrate --stamp
```

**Do not stamp a database whose schema does not actually match.** Stamping is a
claim that the migrations have run, and a wrong claim is silently believed
forever after. If only *some* of the head's changes are present, `--stamp` to
the last revision that is genuinely applied and upgrade from there.

`--stamp` exists for the one-time case of a database that predates migrations;
this is the same shape arriving a different way.

## `stored payload for … does not fit schema version …`

A payload model changed and its source's `schema_version` did not, so the cache
holds bytes the current shape rejects.

Nothing is broken and nothing is lost — the collection path treats it as a miss
and re-collects, which is the correct answer to a question the stored bytes no
longer answer. What it costs is requests against the one per-address budget,
one per affected target, until the version is bumped.

The fix is the bump. `just check` will already be failing with
`… changed shape without a bump`, naming the kind and the line; bump the
version in that source module and run `just record-payload-shapes`.

Before that check existed this failed differently and much worse: the
`ValidationError` reached FastAPI's default handler, so `POST /v1/jobs`
answered 500 for every target that had a cached artifact.

## `this connection's search_path leads with …, not 'tubedepth'`

`_database()` refuses to proceed before touching a table (#16). The
connection's `search_path` does not lead with this service's schema, which
means unqualified names — every table this codebase creates, including
`alembic_version` — would resolve into whatever schema does lead, most often
`public`, the one three other services on the shared PostgreSQL instance also
use. Nothing about that fails on its own: it works until someone else's
migration, or a `pg_dump -n tubedepth`, meets the tables sitting where they
should not be.

The cause is always the same: `deploy/postgres-bootstrap.sql`'s
`ALTER ROLE ... IN DATABASE ... SET search_path = tubedepth, pg_catalog` was
never run against this host, or was run against the wrong role or the wrong
database. This is the gap CI cannot see — CI always bootstraps from that file,
a host set up by hand may not have.

The fix is to run the bootstrap file's `ALTER ROLE` statement for the role
this deployment logs in as, against the database it connects to, then
reconnect — a session already open when the `ALTER ROLE` runs keeps its old
`search_path` until it reconnects:

```sql
ALTER ROLE tubedepth_runtime IN DATABASE <the database name>
  SET search_path = tubedepth, pg_catalog;
```

## `no schema at …` from any command

A fresh `--data-dir`, or one pointed at a database nothing has migrated yet.
`_database()` — what every CLI entry point opens the database through —
checks that the schema exists before handing it to the command, and refuses
rather than letting the first query fail deeper down with `no such table`
(#14: the boot path issues no DDL, so it cannot silently build one for you
either).

The fix is what the message says:

```sh
uv run tubedepth migrate --data-dir <the same --data-dir>
```

## `table jobs has no column named api_key_id`

The database file predates the column. `create_all` never alters a table it
already finds, so a schema change lands in new databases only, and the old one
fails at the first INSERT that touches the missing column rather than at
startup.

Nothing repairs this at open time (#14) — the boot path issues no DDL, so no
command run against the file adds the column for you any more. The fix is
`tubedepth migrate`: every column any model has ever gained is one of the
revisions it runs in order, so running it against this file brings it to the
schema the code expects.

If `tubedepth migrate` itself then fails with `duplicate column name`, that
means the file already has the column from before #14 shipped — see that
entry above, and use `--stamp` rather than running the migration again.

## `no caption track in the video's own language: <tag>`

Not a failure of extraction. The video has neither written captions nor a
transcription in the language it is actually in, which is common for music and
ambience uploads with captions turned off. Translations of some other language's
track are deliberately not an answer here. The job fails terminally and is not
retried, because retrying cannot change it.

The variant `the video reports no language and has no caption track in ko, en`
means both language signals were absent — old uploads with no ASR — and the
configured fallback found nothing either.

## `api/timedtext?...&tlang=ko answered 429 on egress direct`

The `tlang=` parameter marks an auto-translated caption track, and those draw on
a budget separate from everything else YouTube gives this address — three or
four requests, then 429, then served again about six minutes later (polled each
minute: 429 at +1 through +5, 200 at +6).

**Nothing is wrong with the address.** Measured in the same second as one of
these 429s: the plain caption track answered 200, the ASR track answered 200,
and a full metadata extraction of another video succeeded in 1.2 s. Do not
quarantine the egress over this; the rate controller never sees it, because
`video.transcript` falls through to the next ranked candidate and the job
succeeds.

The budget is **per address, not per video** — a different video's first
translation request is refused while this one is exhausted. So the visible
symptom under a bulk Korean-first sweep **of English videos** is that the first
few come back in Korean and the rest come back in English. Check `language` and
`is_automatic` in the payload before looking for a parser problem.

**This should no longer be reachable through `video.transcript`**, which now
filters `tlang=` tracks out of the candidates entirely. Seeing it means
something else fetched a caption URL — check what, because that path is
rationed and the caller probably does not know it.

## `youtube asked for proof we are not a bot, fetching: <id>`

The bot check. This is the one yt-dlp failure that is about the address rather
than the video, and it is deliberately raised as a `RateLimitedError` so the
rate controller quarantines that lane instead of treating it as a bad video.

Before doing anything else, upgrade: `just update-ytdlp`. That fixes this more
often than any other step. After that, export a cookie jar in Netscape format
and point `TUBEDEPTH_COOKIES_FILE` at it; the worker carries it into every
extraction, and refuses to start if the path is wrong rather than quietly
sending nothing. Use an account you can afford to lose — a jar is a session.

`--impersonate` is the rung after that and **is not implemented**: it needs a
`curl_cffi` dependency nobody has taken. This line used to name it as though
it were available, alongside the cookie variable that nothing read.

A different egress is last and on this host is probably backwards — see the
proxy section of the README. Do not raise concurrency to make up the lost
throughput either; that is the input that produced the check.

## `video cannot be watched from here: <id>`

Private, deleted, members-only, age-gated or region-blocked. Terminal and not
retried, because waiting does not make a private video public. It says nothing
about the address and does not touch the rate controller.

Both phrasings of the unavailable message are matched (`Video unavailable` and
`This video is unavailable`) — the second was found only by running against a
withdrawn video, having written the first from an invalid id.
