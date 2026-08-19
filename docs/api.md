# REST API reference

Everything this service serves, what it answers, and what each answer means.
The Korean translation is [`api.ko.md`](api.ko.md); when the two disagree, this
file is right.

The version this describes is the one the package reports — `GET /healthz`
returns it, and [`../CHANGELOG.md`](../CHANGELOG.md) records what changed in it.

## Conventions

| | |
| --- | --- |
| Base URL | `http://127.0.0.1:8080` by default — the API binds to loopback |
| Authentication | `X-API-Key: ytd_...` on everything under `/v1` |
| Request body | JSON, `Content-Type: application/json` |
| Response body | JSON, always an object |
| Errors | `{"error": {"code": "...", "message": "..."}}` |
| Timestamps | RFC 3339, UTC |

`/v1` is the version of the HTTP contract, not of the package. A change that
would break a client written against this document gets `/v2`; the package
version moves for reasons a client never notices.

**There is no TLS here, and the key travels in a header.** Binding to anything
other than loopback without a reverse proxy in front puts that key on the wire
in clear text.

An interactive OpenAPI document is served at `/docs`, generated from the same
route definitions this file describes by hand.

## Authentication

Keys are minted on the machine that runs the service:

```sh
uv run tubedepth key create --label ingest
# ytd_4f3a9c21_9d1c...  ← printed once, stored only as a SHA-256 hash
```

Send it on every `/v1` request:

```sh
curl -s -H "X-API-Key: $KEY" localhost:8080/v1/sources
```

A missing, malformed, unknown or revoked key all get the same 401 with code
`unauthenticated`, so the endpoint cannot be used to learn which of the four it
was. `/healthz` and the dashboard at `/` take no key — a deployment has to be
diagnosable before anyone holds credentials.

Each key carries an allowance, 60 requests per minute by default, and going
over it is 429 with code `rate_limited`.

**The allowance is counted in one process.** Two API processes each grant the
full allowance to the same key. This is honest for the single-instance
deployment this project is built for; if you run more, the number means
nothing.

## What a job is

Collection is asynchronous because extraction is slow — a full comment harvest
runs for minutes. A client submits a job, gets an id, and either polls or is
called back.

```
POST /v1/jobs  ─┬─→ 200 + the result           a fresh artifact already existed
                └─→ 202 + job_id  →  queued → running →  succeeded → GET .../result
                                                      ├→ failed     error_code says why
                                                      └→ cancelled  it never ran
```

The 200 is not an optimisation detail a client can ignore: it is the normal
outcome for anything asked for twice inside its freshness window, and it costs
no poll cycle. Pass `"refresh": true` to force collection anyway.

A submission carrying `"refresh": true` is therefore always 202 and never 200 —
there is no cached answer it would accept. The flag travels with the job rather
than being spent on the request, so the worker collects again when it reaches
it, and a retry of that job is still a forced collection. **This is what makes
`GET /v1/artifacts` a history rather than a cache:** a forced collection records
a new observation, and one that quietly answered from the cache would not.

## Kinds

What a job can ask for. `GET /v1/sources` returns this same table from the
registry, so it is never out of date; the copy here says what each one is for.

<!-- kinds:start -->

| kind | target | lane | cost | fresh for | what it collects |
| --- | --- | --- | --- | --- | --- |
| `video.metadata` | video | youtube | standard | 6h | chapters, the 100-bucket most-replayed heatmap, tags, exact publish time, licence, caption track list |
| `video.transcript` | video | youtube | standard | 30d | caption text in the video's own language, human-written preferred |
| `video.comments` | video | youtube | expensive | 24h | every comment, threaded by `parent_id`, with pinned/hearted/verified flags |
| `video.sponsor_segments` | video | sponsorblock | cheap | 6h | SponsorBlock segments (community data, CC BY-NC-SA 4.0) |
| `video.related` | video | youtube | cheap | 1h | the related-videos rail |
| `video.bundle` | video | youtube | expensive | 6h | metadata, transcript, sponsor segments and related videos in one job; whatever is missing is named in `degradations`. Comments are excluded on purpose — folding a minutes-long harvest in would make every bundle the most expensive job in the system |
| `channel.about` | channel | youtube | cheap | 7d | join date, country, links, **exact total view count**, description, tags, avatar |
| `channel.community` | channel | youtube | cheap | 6h | community posts |
| `channel.videos` | channel | youtube | cheap | 6h | a channel's uploads |
| `playlist.items` | playlist | youtube | cheap | 6h | a playlist's entries |
| `search.videos` | query | youtube | cheap | 6h | search results |
| `trending.videos` | region | youtube_data_api | cheap | 15m | what YouTube itself calls popular in one region, in its order. The only kind that reports a ranking rather than an observation, and the only one that spends Google API quota instead of the per-address budget |

<!-- kinds:end -->

**To enumerate a channel completely, use `playlist.items` on its uploads
playlist** — the channel id with `UC` swapped for `UU`. `channel.videos` reads
the `/videos` tab, which holds neither Shorts nor past live streams at any cap.
Measured on one 697-video channel, 2026-08-20:

| | items | requests |
| --- | --- | --- |
| `playlist.items` on `UU…` | **698** | **8** |
| `channel.videos` | 474 | 16 |

So the uploads playlist is both wider and cheaper — it pages a hundred at a
time where the tab pages thirty. The 224 it adds are 216 Shorts, 3 past live
streams, and 5 entries that appear in the grid with titles and view counts and
cannot be watched at all; those five become jobs that fail as `not_found`,
because nothing in a flat listing distinguishes them from a live video.

Mind the cap: `TUBEDEPTH_LISTING_LIMIT` is deployment-wide, so at its default
of 100 this returns 100 of the 698.

`target` is what the `target` field of a submission has to name. A video
accepts an id, a `youtu.be` link or a `watch?v=` URL; a channel accepts an id,
an `@handle` or a channel URL — normalisation happens before the job is
recorded, so the ledger stores one canonical form.

`lane` is which upstream the request goes to, and `cost` is how the queue rates
it. Both exist so that a comment harvest cannot starve everything else.

---

## `GET /healthz`

Unauthenticated. Whether the service is up, what version it is, and what the
queue and each source have been doing.

```sh
curl -s localhost:8080/healthz
```

```json
{
  "status": "ok",
  "version": "0.1.0",
  "queued": 3,
  "running": 1,
  "sources": [
    {
      "kind": "video.metadata",
      "status": "ok",
      "consecutive_failures": 0,
      "last_success_at": "2026-08-19T09:12:44Z",
      "last_failure_at": null,
      "last_error_code": null,
      "last_error_message": null
    }
  ]
}
```

`lanes` is what the rate controller currently allows on each route, written by
the worker because the controller's state is a dict in the worker's memory and
dies with the process.

```json
{
  "lanes": [
    {
      "egress": "direct",
      "lane": "youtube",
      "window": 3.5,
      "in_flight": 1,
      "quarantine_streak": 0,
      "quarantined_until": null,
      "observed_at": "2026-08-20T09:12:44Z"
    }
  ]
}
```

`window` is a **measured** ceiling rather than a setting: it halves when an
upstream refuses and grows back on success, so a window well under one is the
number that explains a queue draining slowly. `quarantined_until` is null while
the route is open, and present means nothing will be attempted on it until then
— which from outside is indistinguishable from an empty queue unless something
says so.

`status` stays `"ok"` while individual sources are not, because this endpoint is
read by things that restart processes and one broken parser is not a reason to
cycle an API whose other ten kinds are still collecting. The bad news is in
`sources`, where a person reads it — and in `last_error_message` rather than
`last_error_code`. The code says `parse_mismatch`; the message names the
renderer that stopped matching, which is the difference between knowing a
source is broken and knowing what to change.

A source's `status` distinguishes causes that need different fixes:

| value | meaning | what fixes it |
| --- | --- | --- |
| `ok` | recent successes | — |
| `degraded` | one recent failure | usually nothing; watch it |
| `broken` | our parser stopped matching | a code change — check `degradations` for `parse_mismatch` |
| `blocked` | the address is being refused | a different egress, or waiting |
| `stale` | nothing has exercised it lately | run a job |
| `unknown` | never tried on this instance | run a job |

`unknown` and `stale` are deliberately not green. A dashboard showing healthy
for something nobody has run is worse than one admitting it does not know.

---

## `GET /v1/control`, `PATCH /v1/control`

Whether the worker is claiming, and the only way to tell it not to.

```sh
curl -s -X PATCH -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
     -d '{"paused": true, "reason": "watching a quota"}' \
     localhost:8080/v1/control
```

```json
{ "paused": true, "reason": "watching a quota", "changed_at": "2026-08-20T09:12:44Z" }
```

**This does not reach into the worker.** The API and the worker are separate
processes on purpose — a yt-dlp crash must not take the API down — so nothing
here can stop anything directly. It writes a row the worker reads at the top of
each drain, and `tubedepth work` drains and exits with its unit restarting it
every ten seconds, so a pause takes effect within about that.

**A job already running finishes.** Pausing means claim nothing; it is not a
cancellation, and the extraction in flight keeps spending requests until it is
done. To stop one of those, cancel it.

Queued jobs stay queued and nothing is failed on the way in, so resuming is the
whole of the undo. `reason` is optional and worth filling in: a pause nobody can
explain an hour later is a pause nobody dares lift.

No row yet means nobody has ever paused this, which is reported as running
rather than as an error.

---

## `GET /v1/sources`

What this build can collect, read from the registry — so a source added in code
documents itself here without anyone editing a list.

```sh
curl -s -H "X-API-Key: $KEY" localhost:8080/v1/sources
```

```json
{
  "video.metadata": {
    "kind": "video.metadata",
    "target": "video",
    "lane": "youtube",
    "cost": "standard",
    "freshness_seconds": 21600,
    "cache_parameters": {}
  }
}
```

---

## `POST /v1/jobs`

Ask for data. Answers 202 with a job, or 200 with the result if a fresh one
already exists.

| field | type | default | |
| --- | --- | --- | --- |
| `kind` | string | required | one of the kinds above |
| `target` | string | required | id, handle, URL or search query |
| `refresh` | bool | `false` | collect even if a fresh artifact exists |
| `webhook_url` | URL | `null` | called once when the job reaches a terminal state |

```sh
curl -s -X POST -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
     -d '{"kind":"video.metadata","target":"https://youtu.be/dQw4w9WgXcQ"}' \
     localhost:8080/v1/jobs
```

**202 Accepted** — queued. `Location` carries the job's URL.

```json
{
  "job_id": "j_2f7c1d9a",
  "kind": "video.metadata",
  "target": "dQw4w9WgXcQ",
  "state": "queued",
  "attempt_count": 0
}
```

**200 OK** — a fresh artifact existed, and the body is the collected data
itself, not a job. Distinguish the two by status code, not by shape.

Failure modes worth a branch: an unknown `kind` or an unparseable `target` is
422 `invalid_request`; a malformed `webhook_url` is rejected here rather than
stored, because a bad URL stored is a delivery that fails on every sweep
forever.

---

## `POST /v1/jobs/batch`

One kind, many targets, one request. Answers 202.

```sh
curl -s -X POST -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
     -d '{"kind":"video.metadata","targets":["dQw4w9WgXcQ","nfgdJyL-Jmg"]}' \
     localhost:8080/v1/jobs/batch
```

```json
{
  "queued": [{ "job_id": "j_2f7c1d9a", "kind": "video.metadata", "target": "nfgdJyL-Jmg", "state": "queued", "attempt_count": 0 }],
  "held": [{ "target": "dQw4w9WgXcQ", "digest": "b9f4c0e2..." }]
}
```

**This is not a convenience.** A key is allowed sixty requests a minute, so a
hundred-video sweep submitted one target at a time is rate-limited before it is
half done — the difference between an API that can express a sweep and one that
can run it.

**All or nothing.** Every target is normalised before anything is queued, so one
bad id refuses the whole batch with 422 rather than queueing the other
ninety-nine and answering 202. A partial sweep is the worst outcome available:
the caller believes it ran, and the gap surfaces later as an absence nobody is
looking for.

At most 500 targets; more is 422. Unlike `POST /v1/jobs` this never returns a
payload — a target already held is named with its `digest`, which is what
`GET /v1/artifacts/{digest}` takes. Returning a hundred bodies would make a
submission a bulk download.

---

## `GET /v1/jobs/{job_id}`

One job's current state.

```sh
curl -s -H "X-API-Key: $KEY" localhost:8080/v1/jobs/$JOB
```

```json
{
  "job_id": "j_2f7c1d9a",
  "kind": "video.metadata",
  "target": "dQw4w9WgXcQ",
  "state": "succeeded",
  "attempt_count": 1,
  "error_code": null,
  "error_message": null,
  "payload_bytes": 26417,
  "created_at": "2026-08-19T09:12:31Z",
  "finished_at": "2026-08-19T09:12:44Z"
}
```

| state | |
| --- | --- |
| `queued` | waiting for a worker |
| `running` | a worker holds a lease on it, renewed while it works |
| `succeeded` | the result is at `/v1/jobs/{job_id}/result` |
| `failed` | `error_code` and `error_message` say why; retries are already spent |
| `cancelled` | asked for and no longer wanted; it never ran |

404 `not_found` for an id this instance does not have.

---

## `GET /v1/jobs/{job_id}/result`

The collected data, verbatim — the stored payload, not a re-encoding of it.

```sh
curl -s -H "X-API-Key: $KEY" localhost:8080/v1/jobs/$JOB/result
```

**409 `conflict`** if the job exists but has not finished. Not a 404: the
difference between "wait" and "you asked for something that does not exist" is
the difference between a client that retries and one that gives up.

**404 `not_found`** if the job finished and its result has since aged out of
retention. This is the ordinary end state of an old job rather than an error —
retention removes artifacts and never touches the job ledger, so a job stays
answerable about what it did long after what it collected is gone. **Results
are not permanent; the job ledger is.** A client that needs the data beyond the
retention window has to store it when it fetches it.

Every payload carries a `degradations` list. It is empty on a clean collection
and names what could not be had otherwise — a `video.bundle` whose comments
were disabled, or a surface whose renderer no longer matches, which appears as
`parse_mismatch`. **An empty list is a promise; a missing part always has a
name.** Silently returning less than was asked for is the failure this project
is built to make impossible.

---

## `DELETE /v1/jobs/{job_id}`

Stop a job that is no longer wanted. The row survives — a queue that forgets
what it was told to stop cannot answer why nothing arrived.

```sh
curl -s -X DELETE -H "X-API-Key: $KEY" localhost:8080/v1/jobs/$JOB
```

**Read the `state` that comes back; it is the answer.**

- `cancelled` — it never ran, and never will.
- `running` — the request was recorded. The job will not be retried and will
  not hand back a result, but the extraction already in flight is still
  spending requests until it finishes. Answering `cancelled` here would
  announce that a cost had stopped when it had not.

---

## `GET /v1/jobs`

The job ledger, newest first.

| parameter | |
| --- | --- |
| `state` | `queued`, `running`, `succeeded`, `failed`, `cancelled` |
| `kind` | one of the kinds above |
| `target` | the canonical target, as stored |
| `since` / `until` | RFC 3339, filtering on `created_at` |
| `limit` | default 50, capped at 500 |
| `cursor` | from the previous page |

```sh
curl -s -H "X-API-Key: $KEY" 'localhost:8080/v1/jobs?state=failed&limit=20'
```

```json
{
  "jobs": [{ "job_id": "j_2f7c1d9a", "state": "failed", "error_code": "upstream_error" }],
  "cursor": "MjAyNi0wOC0xOVQwOToxMjozMSswMDowMHxqXzJmN2MxZDlh"
}
```

---

`cache_parameters` is what, besides kind and target, makes a source's answer a
different answer: a listing's cap, a comment harvest's sort and cap, a
transcript's language preference. **These are the values in effect in the
process that answered.** `tubedepth serve` and `tubedepth work` read the
environment once each in separate processes, and if they disagree the API
computes a different cache key than the worker records — so it stops matching
what the worker writes while still matching rows from before the change.
Comparing this route between the two is how that is caught.

## `GET /v1/artifacts`

What was actually collected, as opposed to what was asked for. Takes `kind`,
`target`, `since`, `until`, `limit` and `cursor`, filtering on `fetched_at`.

```sh
curl -s -H "X-API-Key: $KEY" 'localhost:8080/v1/artifacts?target=dQw4w9WgXcQ'
```

```json
{
  "artifacts": [
    {
      "kind": "video.metadata",
      "target": "dQw4w9WgXcQ",
      "schema_version": "1",
      "digest": "b9f4c0e2...",
      "byte_count": 26417,
      "fetched_at": "2026-08-19T09:12:44Z",
      "fresh_until": "2026-08-19T15:12:44Z"
    }
  ],
  "cursor": null
}
```

`schema_version` is which version of that kind's normalizer wrote the bytes.
It is `null` for anything collected before the column existed — the fingerprint
carries the version and is a SHA-256, so it cannot be recovered from the row.
**Two observations with different `schema_version` values are not directly
comparable**: a bump means the shape changed, and a field one of them has may
simply not have been collected by the other.

The artifact table appends rather than overwrites, so filtering by `target`
gives one video's history — how its counts moved over time. The job ledger
cannot answer that; this is the table that keeps it.

`digest` is the content address of the stored payload: two collections that
produced identical bytes share one. Equal digests across two `fetched_at`
values mean nothing changed.

---

---

## `GET /v1/artifacts/{digest}`

One observation, addressed by its content. This is how history is read: the
list route hands out digests, and this is what dereferences them.

```sh
curl -s -H "X-API-Key: $KEY" localhost:8080/v1/artifacts/b9f4c0e2...
```

```json
{
  "digest": "b9f4c0e2...",
  "kind": "video.metadata",
  "target": "dQw4w9WgXcQ",
  "fetched_at": "2026-08-19T09:12:44Z",
  "schema_version": "1",
  "current_schema_version": "1",
  "payload_fields": ["chapters", "most_replayed", "tags", "view_count"],
  "current_fields": ["chapters", "most_replayed", "published_date", "tags", "view_count"],
  "payload": { "...": "the bytes as they were collected" }
}
```

**The payload is returned verbatim and is never re-parsed.** A payload written
by an older normalizer comes back as it was stored, because the original
observation is the thing worth keeping — re-shaping it with today's model is
how a history stops being one.

`payload_fields` and `current_fields` are computed rather than declared, and
their difference is the honest answer to "what does this old observation not
have". A field the older version never collected is **absent** from
`payload_fields`, which says more than a null would.

**410 `retracted`** if the version that collected it is one the source has
withdrawn — its payloads are wrong rather than merely old, and serving them as
history would launder a known-bad observation. Not a 404: the observation
happened, and a 404 would claim it never did.

404 `not_found` for a digest this instance never stored, and for one whose
payload has since aged out of retention.

## Pagination

Both list endpoints return `cursor`, and `null` means this was the last page —
a client stops by reading the response rather than by counting.

```sh
curl -s -H "X-API-Key: $KEY" "localhost:8080/v1/jobs?cursor=$CURSOR"
```

The cursor is opaque and keyed on the last row's timestamp and id, not an
offset. An offset re-reads what it skips and drifts when rows arrive during
paging, which on a table the worker is actively writing means showing one job
twice and missing another. Do not construct one: a cursor this API did not
issue is 422 `invalid_request`.

## Errors

Every error is the same shape.

```json
{ "error": { "code": "not_found", "message": "job not found: j_2f7c1d9a" } }
```

| status | code | meaning |
| --- | --- | --- |
| 401 | `unauthenticated` | key missing, malformed, unknown or revoked |
| 422 | `invalid_request` | unknown kind, unparseable target, cursor this API did not issue |
| 404 | `not_found` | no such job — or the video does not have the thing asked for |
| 409 | `conflict` | the job exists but has not finished |
| 410 | `retracted` | the version that collected this observation has been withdrawn |
| 429 | `rate_limited` | over the key's allowance, or an upstream refused this address |
| 502 | `parse_mismatch` | YouTube answered and our parser no longer understands it |
| 502 | `upstream_error` | an upstream answered, and the answer was unusable |
| 500 | `internal_error` | our bug |

`parse_mismatch` is kept apart from every other upstream failure and is never
retried. It is not transient and not a network problem: retrying spends
requests against an address that answered perfectly well, and the only thing
that fixes it is a code change. It is 502 rather than 500 for the same reason —
a 500 sends an operator into our tracebacks, a 502 sends them to the renderer
names in the message.

`message` is written to be shown to a person and names the offending value.

## Webhooks

Pass `webhook_url` on a submission and this service `POST`s once when the job
reaches a terminal state. It is not a replacement for polling — polling is
cheap here — but for a comment harvest running for minutes the alternative is
waking every few seconds to be told "not yet".

```http
POST /your-endpoint
Content-Type: application/json
X-Tubedepth-Timestamp: 2026-08-19T09:12:44.183726+00:00
X-Tubedepth-Signature: 4a7f...
```

```json
{
  "job_id": "j_2f7c1d9a",
  "kind": "video.metadata",
  "target": "dQw4w9WgXcQ",
  "state": "succeeded",
  "error_code": null,
  "payload_bytes": 26417
}
```

The body carries no data — fetch the result if the callback says `succeeded`.

**Verify the signature.** It is HMAC-SHA256 over `f"{timestamp}." + body`,
hex-encoded, keyed with `TUBEDEPTH_WEBHOOK_SECRET`. The timestamp is inside the
signed material rather than beside it, so a delivery someone recorded cannot be
replayed later with a fresh clock — reject anything older than your own
tolerance.

```python
material = f"{timestamp}.".encode() + body
expected = hmac.new(secret.encode(), material, hashlib.sha256).hexdigest()
hmac.compare_digest(expected, presented)
```

A callback URL travels in a job submission, so it is not a secret and cannot be
treated as one. The signature is what distinguishes a delivery from this
service from one by anyone who learned the URL.

Delivery is at-least-once and gives up after 8 attempts. Answer any 2xx to be
counted as delivered; anything else leaves the job owed and the next sweep
tries again.

## What this API does not do

- **No TLS, no OAuth, no scopes.** One header, one allowance, one machine.
- **No push of results.** The webhook says a job finished; it does not carry
  what was collected.
- **No exact subscriber count and no dislikes.** YouTube publishes neither, so
  no field promises them. See the limits section of the [README](../README.md).
- **No streaming or partial results.** A job's result exists once, whole.
