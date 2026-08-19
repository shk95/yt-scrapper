# An unknown result must not burn a healthy address

**The rule.** A failed job tells the rate controller something about the route
it used only when the failure is *about* the route. `RateLimitedError` means
blocked; `UpstreamError` means throttled; everything else — a missing caption
track, a private video, a parser that no longer matches, an unexpected Python
exception — is `Verdict.NEUTRAL` and changes nothing.

**What went wrong without it.** The worker reported every domain failure that
was not a `RateLimitedError` as `THROTTLED`, which halves the lane's window and
doubles its minimum interval. A forty-job transcript sweep at concurrency 8
finished twenty-two jobs in its first fifteen seconds and then fell to roughly
one per fifteen seconds for three and a half minutes. The only failures in it
were seven videos whose uploaders had turned captions off.

Each one doubled the interval — 1s, 2s, 4s, 8s, 16s — and the tail rate was
that ceiling. Nothing about YouTube had changed. The system had rate-limited
itself over facts about seven videos, and every symptom pointed outward.

Cost: roughly 8× throughput, and it took a sweep large enough for the interval
to compound before anything was visible. A single job, the whole test suite and
a ten-job trial all looked fine.

**The mirror image, added at the same time.** yt-dlp reports a private video, a
network blip and a bot check as the same `DownloadError` with the reason in the
message, and nothing translated them — so the one failure that *is* evidence
about the address could never reach the controller either. Making the default
neutral without also classifying the bot check would have traded over-reaction
for silence.

**What would have to change for this to stop being right.** If a failure class
were added that genuinely indicates a bad route and is not a subclass of
`UpstreamError`, the mapping in `verdict_for_error` would need it. The rule
survives that; the table is meant to grow. What must not return is the default:
unknown stays neutral, because a classifier that guesses is one that can
quarantine every address the day YouTube renames a renderer.
