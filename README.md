# tubedepth

A self-hosted API that collects the YouTube video and channel data the official
Data API does not expose.

*[한국어](README.ko.md)*

Chapters, the "most replayed" heatmap, tags, exact publish times, caption text,
whole comment threads, SponsorBlock segments, related videos, channel About
panels, community posts. A client submits a **job** with an API key and
collects normalised JSON.

```sh
curl -X POST -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
     -d '{"kind":"video.metadata","target":"dQw4w9WgXcQ"}' localhost:8080/v1/jobs
# 202 + job_id → poll → chapters, most_replayed (100 buckets), tags, published_at …
# a fresh result already stored answers 200 with the data, and no job is created
```

Full endpoint reference: [`docs/api.md`](docs/api.md).

## What it collects

<!-- kinds:start -->

| kind | what you get | via the official API |
| --- | --- | --- |
| `video.metadata` | chapters, a 100-bucket heatmap, tags, exact publish time, licence, caption track list | tags for the owner only; the rest has no field |
| `video.transcript` | caption text, in the video's own language, human-written preferred | not without the owner's OAuth |
| `video.comments` | every comment, threaded by `parent_id`, with pinned, hearted and verified flags | possible, but the quota does not survive it |
| `video.sponsor_segments` | SponsorBlock segments (community data, CC BY-NC-SA 4.0) | absent |
| `video.related` | the related-videos rail | absent |
| `video.bundle` | four of the above in one job; anything missing is named in `degradations` | — |
| `channel.about` | join date, country, links, **exact total view count**, description, tags, avatar | mostly absent |
| `channel.community` | community posts | absent |
| `channel.videos` · `playlist.items` · `search.videos` | listings — fan out to per-item collection with `--then` | possible, and it spends quota |
| `trending.videos` | what YouTube itself calls popular, in its own order | the chart endpoint, which is what this uses |

<!-- kinds:end -->

`tubedepth sources` and `GET /v1/sources` always report the real list.

## Why it exists

Data API v3 withholds a great deal about videos that are public. `snippet.tags`
comes back only to the video's owner, caption text needs the owner's OAuth, and
chapters, the heatmap, related videos and community posts have no field at all.
Comments are available and cost more quota than bulk collection can afford.

## Getting started

```sh
git config core.hooksPath .githooks   # a fresh clone arrives with hooks off
tool/doctor.sh                        # toolchain, PostgreSQL reachability, hooks
uv sync --extra dev
just check                            # format + lint + the test suite (needs Docker)

uv run tubedepth key create --label local   # the secret is printed once
uv run tubedepth serve --port 8080 &        # the API, on 127.0.0.1 by default
uv run tubedepth work --concurrency 6       # the worker, a separate process
```

```sh
KEY=ytd_...
curl -s -X POST -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
     -d '{"kind":"video.metadata","target":"https://youtu.be/dQw4w9WgXcQ"}' \
     localhost:8080/v1/jobs                  # 202 + job_id, or 200 with a cached result
curl -s -H "X-API-Key: $KEY" localhost:8080/v1/jobs/$ID/result
```

**A key's rate limit is counted inside one process.** Run two API processes and
each grants the same key its own allowance — this is written for a single
instance, and anywhere else the number means nothing.

## Dashboard

```sh
uv run tubedepth serve --port 8080
```

`http://localhost:8080/` shows queue state, per-source health, a 24-hour
completion trend, and a record browser over jobs and artifacts. The page itself
needs no key; you type one into the browser and it rides along as `X-API-Key`
on every read after that. Keys come from `tubedepth key create`.

It references no external resource, so it loads on a private network with no
route to the internet.

## Deployment

systemd **user** units live in `deploy/`. None needs root, and none can quietly
acquire it. Two of them are the service itself:

```sh
mkdir -p ~/.config/tubedepth
echo 'TUBEDEPTH_DATABASE_URL=postgresql+psycopg://tubedepth_runtime:...@host/db' \
  > ~/.config/tubedepth/worker.env
cp ~/.config/tubedepth/worker.env ~/.config/tubedepth/database.env   # api.service reads this one
chmod 0600 ~/.config/tubedepth/worker.env ~/.config/tubedepth/database.env

cp deploy/tubedepth-api.service deploy/tubedepth-worker.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tubedepth-api tubedepth-worker
loginctl enable-linger $USER    # or a reboot looks exactly like a crash
```

There is no SQLite fallback: every unit refuses to start without a working
`TUBEDEPTH_DATABASE_URL`. `deploy/postgres-bootstrap.sql` is what provisions
the roles and schema that URL points at, and `docs/shared-postgres.md` is the
regulation behind it.

The third is optional and off by default: `tubedepth-watch.timer` runs
`tubedepth watch` every hour, which queues a whole watch list forced past the
freshness window so that each pass records a new observation. That is what
turns `GET /v1/artifacts` from a cache into a history you can differentiate —
and it only accumulates in real time, so it is worth starting before anything
needs it.

```sh
mkdir -p ~/.config/tubedepth
cp deploy/watchlist.example.txt ~/.config/tubedepth/watchlist.txt
$EDITOR ~/.config/tubedepth/watchlist.txt        # one typed directive per line
cp deploy/tubedepth-watch.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tubedepth-watch.timer
```

The list is typed — `video`, `channel`, `search` or `trending`, then the
target — so one schedule collects a fixed set of videos, a channel's uploads, a
trend keyword and a region's chart at once. A directive that is not one of the
four is refused naming the line, because a typo that quietly collects nothing
is what a watch list is worst at showing you. Where there is no timer — compose
— `tubedepth watch --every 3600` stays resident instead.

**Size the list deliberately, and note the four types do not cost the same.**
A `video` line is one forced collection per firing, out of the same per-address
budget everything else draws on; thirty of them hourly is about one percent of
the measured throughput. A `channel`, `search` or `trending` line fans out to a
`video.metadata` job per video it finds, up to `TUBEDEPTH_LISTING_LIMIT`
(default 100) — **one such line can be a hundred collections, not one.**
`deploy/watchlist.example.txt` has the arithmetic. The behaviour of this system
under sustained load well above that has not been measured.

Splitting the API from the worker is not a matter of taste. yt-dlp extraction
blocks and holds memory; run them together and one comment harvest sets the p99
of `GET /v1/jobs/{job_id}`, while a yt-dlp crash takes the API with it.

The API binds to **loopback** by default. Authentication here is a header,
which is not a substitute for TLS, so put a reverse proxy in front before
exposing it.

## Documentation

| | |
| --- | --- |
| [`docs/api.md`](docs/api.md) | REST reference — every endpoint, error code and the webhook contract |
| [`docs/status.md`](docs/status.md) | where things stand, and the decisions behind them |
| [`docs/troubleshooting.md`](docs/troubleshooting.md) | errors that have already cost someone an afternoon — grep it, do not read it |
| [`docs/shared-postgres.md`](docs/shared-postgres.md) | the rules for the PostgreSQL instance this shares with the other scrapers |
| [`CHANGELOG.md`](CHANGELOG.md) | what changed in each release |
| [`docs/releasing.md`](docs/releasing.md) | how a release is cut |
| [`AGENTS.md`](AGENTS.md) | how to work in this repository |

`README.md`, `docs/api.md`, `CHANGELOG.md` and `AGENTS.md` are the originals;
the `.ko.md` files beside them are translations. Everything else is Korean.

## Versioning

The package version is written in exactly one place,
`src/tubedepth/__init__.py`, and `pyproject.toml` reads it from there.
`tubedepth version`, `GET /healthz` and the OpenAPI document all report it.

`/v1` versions the HTTP contract, separately and on its own schedule: it moves
only when a change would break a client written against
[`docs/api.md`](docs/api.md). The package version moves for reasons no client
notices.

Releases follow [semantic versioning](https://semver.org) and are recorded in
[`CHANGELOG.md`](CHANGELOG.md), tagged `vX.Y.Z` on `master`. The procedure is
[`docs/releasing.md`](docs/releasing.md). Until 1.0, a minor bump may change
collected payload shapes; `schema_version` on each source is what a stored
artifact is keyed by.

## Status

Every stage of the plan (M0–M9) is built, plus a dashboard the plan never asked
for. **Two things the plan specified were deliberately not built**, each with
its reason on record: `video.dislikes` was removed, because nobody can
adjudicate the numbers, and `channel.profile` was cancelled, because its
contents turned out to arrive in a response `channel.about` already makes.

What has been verified and what has not is in
[`docs/status.md`](docs/status.md). Where that file and
[`docs/plan.md`](docs/plan.md) disagree, status.md is right — the plan is a
record, not an instruction.

## Honest limits

What this project **cannot** do, and what has **not been checked**.

**Impossible in principle**

- **Exact subscriber counts.** YouTube publishes a rounded value and nothing
  else — the InnerTube response is literally the string `"4.53M subscribers"`,
  and yt-dlp's integer is that string parsed. The Data API has the same limit
  and scraping does not get around it, so **no field here promises an exact
  number.** You get `subscriber_count_approximate` and the original string.
- **Trending.** YouTube retired the feed; `/feed/trending` redirects to the
  home page. This is an absence, not an omission, so nothing here imitates it.

**Fragile**

- **Related videos, channel About and community posts depend on parsing
  InnerTube renderers**, whose names change without notice —
  `compactVideoRenderer` has already become `lockupViewModel`. The dates in
  `tests/fixtures/innertube/` are when each surface last worked. A
  `parse_mismatch` in a response's `degradations` means one of them has broken.
  The fixture regressions in CI prove **our code has not regressed**; they
  prove nothing about what YouTube is sending today. That is what `just
  contract` is for.
- **You can be blocked.** Volume can earn a login or PO-token challenge. When
  extraction breaks the first move is `just update-ytdlp`, the second is the
  yt-dlp issue tracker, and debugging this code is the third.

**On proxies — the opposite of what you would expect**

- **Do not send YouTube traffic through ProtonVPN.** yt-dlp's own advice is to
  turn the VPN off and use a residential line. YouTube's bot checks target
  datacenter ranges, and every commercial VPN exit is in one. The machine this
  was developed on **has a residential IP that currently works, and a VPN exit
  probably would not.** No configuration turns this on, because nothing has
  been built to turn on; when something is, treating it as a *measurement*
  rather than a fix is the point.
- **The proxy pool has no measured case behind it.** The original quantitative
  argument was Return YouTube Dislike's documented daily cap; **removing that
  source removed the argument with it.** The one remaining third party is
  SponsorBlock, whose limits are undisclosed. Nothing has been measured that
  more exits would definitely improve. Measure it before building the pool.
- **Only residential or mobile proxies actually raise YouTube throughput**, at
  roughly $5–15/GB. `ProxiedEgress` is the seam for it.
- **Check your ProtonVPN concurrent-connection quota.** Each wireproxy process
  takes a slot, so a pool competes with your phone and laptop. The free plan is
  unsuitable.

**Throughput differs by two orders of magnitude between kinds**

| kind | thousands per hour? |
| --- | --- |
| SponsorBlock · cache hits | yes — cache hits are the only axis fully under our control |
| video metadata · related · search | **~3,100/hour sustained** (474 jobs, 0 failed, 430 s, concurrency 8). A 40-job burst reaches 8,417/hour, which is what a burst measures |
| whole comment threads | **no.** 1,000 comments = 50+ requests = 1–3 minutes, and those requests eat the IP budget metadata collection needs |

**Where this stands legally.** YouTube's terms prohibit automated access
outside the public API. That the data is publicly visible, or that the request
rate is low, does not make it permitted. This assumes a private network and a
handful of clients; it is not offered as a public service. What it collects —
captions, comments — is third-party copyrighted work, and comment author
details are personal data the moment they are stored. SponsorBlock data is CC
BY-NC-SA 4.0, so redistributing it carries attribution and non-commercial
conditions. **Dislike counts are not provided.** YouTube made them private in
late 2021, no original exists, and the source that served reconstructed
estimates was removed on purpose — the reasons are in
[`docs/status.md`](docs/status.md).

**Checked and unchecked.** Verified by hand on this machine during planning: 78
yt-dlp keys, caption json3 retrieval, 20 comments in 6.7s, SponsorBlock 200 and
404 both, InnerTube `/next` and `/browse` reachable, PostgreSQL reachable
(SQLite 3.46.1 back when that was still the backend), wireproxy 1.1.3
available, four parallel metadata extractions in 3.11s.

Since then, under sustained load: 474 jobs, none failed, the AIMD window at its
ceiling and the quarantine streak at zero — so the controller settles rather
than oscillates at this rate, and no bot check was reached. **Still not
checked**: where the bot-check threshold actually is, what triggers a PO token,
**a request that leaves through a VPN egress** (no config exists, so nothing has
ever gone out through a proxy), and long multi-worker operation. What remains
unverified is tracked as issues labelled
[`verification`](https://github.com/slopindustries/yt-scrapper/issues?q=is%3Aissue+is%3Aopen+label%3Averification)
rather than only described here.

## License

MIT. See [`LICENSE`](LICENSE).
