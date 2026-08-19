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
tool/doctor.sh                        # toolchain, SQLite, hooks
uv sync --extra dev
just check                            # format + lint + the offline suite

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

Two systemd **user** units live in `deploy/`. Neither needs root, and neither
can quietly acquire it.

```sh
cp deploy/tubedepth-*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tubedepth-api tubedepth-worker
loginctl enable-linger $USER    # or a reboot looks exactly like a crash
```

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
| [`CHANGELOG.md`](CHANGELOG.md) | what changed in each release |
| [`docs/releasing.md`](docs/releasing.md) | how a release is cut |
| [`AGENTS.md`](AGENTS.md) | how to work in this repository |

`README.md`, `docs/api.md` and `CHANGELOG.md` are the originals; the `.ko.md`
files beside them are translations. Everything else is Korean.

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
  probably would not.** Hence `TUBEDEPTH_EGRESS_ALLOW_VPN_FOR_YOUTUBE`
  defaulting to `0`. Turning it on is a *measurement*, not a fix.
- **The proxy pool has no measured case behind it.** The original quantitative
  argument was Return YouTube Dislike's documented daily cap; **removing that
  source removed the argument with it.** The one remaining third party is
  SponsorBlock, whose limits are undisclosed. Nothing has been measured that
  more exits would definitely improve. Measure it before building the pool.
- **Only residential or mobile proxies actually raise YouTube throughput**, at
  roughly $5–15/GB. `ExternalProxyEgress` makes that a configuration change
  rather than a code change.
- **Check your ProtonVPN concurrent-connection quota.** Each wireproxy process
  takes a slot, so a pool competes with your phone and laptop. The free plan is
  unsuitable.

**Throughput differs by two orders of magnitude between kinds**

| kind | thousands per hour? |
| --- | --- |
| SponsorBlock · cache hits | yes — cache hits are the only axis fully under our control |
| video metadata · related · search | measured 8,417/hour (40 jobs, concurrency 8). Sustained load unmeasured |
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
404 both, InnerTube `/next` and `/browse` reachable, SQLite 3.46.1, wireproxy
1.1.3 available, four parallel metadata extractions in 3.11s. **Not yet
checked**: the bot-check threshold under sustained load, what triggers a PO
token, **a request that actually leaves through a VPN egress** (no config
exists, so nothing has ever gone out through a proxy), how AIMD converges under
real load, and long multi-worker operation.

## License

MIT. See [`LICENSE`](LICENSE).
