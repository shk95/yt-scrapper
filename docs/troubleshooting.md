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

### `403 Client Error: Forbidden for url: https://returnyoutubedislikeapi.com/votes`

The request went out without a browser `User-Agent`. RYD answers 403 to
urllib's default agent and 200 to a browser one — verified 2026-08-18.

Every non-yt-dlp request gets its `User-Agent` from the egress it was leased,
so if you are seeing this, something constructed an `httpx.AsyncClient` outside
`src/tubedepth/egress/`. The architecture test exists to catch exactly that.

If the UA is present and it is still 403, the **address** is blocked, not the
request.

### `429 Too Many Requests` from `returnyoutubedislikeapi.com`

RYD documents **100 requests per minute and 10,000 per day, per client**. The
daily figure is the one you hit: one address sustains roughly 400 dislike
lookups an hour averaged over a day.

`[lane.ryd] daily_budget` exists to stop before this happens. Seeing it means
the budget is set too high, or more egresses are needed. It is not a bug.

## `table jobs has no column named api_key_id`

The database file predates the column. `create_all` never alters a table it
already finds, so a schema change lands in new databases only and the old one
fails at the first INSERT rather than at startup.

`create_schema()` now repairs this itself for nullable columns — run any command
that opens the database (`tubedepth jobs`, `tubedepth work`) and the column is
added. A column that is required and has no default cannot be filled in for
existing rows; that case refuses by name instead, and the fix is to migrate the
file by hand or delete it if it holds nothing worth keeping.

## `no caption track in any requested language: ko, en`

Not a failure of extraction. The video has no Korean or English track of any
kind — not manual, not automatic, not translated — which is common for music and
ambience uploads with captions disabled. The job fails terminally and is not
retried, because retrying cannot change the answer.

## `api/timedtext?...&tlang=ko answered 429 on egress direct`

The `tlang=` parameter marks an auto-translated caption track, and YouTube
rations that endpoint far more tightly than the track it translates from.
Measured back to back on one address: four requests for the Korean translation
of dQw4w9WgXcQ, four 429s; four requests for the English track it derives from,
four 200s, interleaved seconds apart.

Nothing is wrong with the address — a 429 here does not mean the line is
blocked, and quarantining it would be an overreaction. `video.transcript`
already handles this by dropping to the next ranked candidate, so the visible
symptom is a transcript that came back in English when Korean was asked for.
Check `language` and `is_automatic` in the payload before assuming a parser
problem. The budget refills; it is not a permanent state.
