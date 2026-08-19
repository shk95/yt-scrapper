# Decisions

One file per convention: what the rule is, what went wrong without it, and what
would have to change for it to stop being right.

That last part matters. A decision recorded without its conditions becomes
dogma, and dogma is what people delete when it gets in the way. If the condition
no longer holds, the rule should go — deliberately, not by accident.

Everything here was written *after* the thing it describes had already gone
wrong, in this project, with a measured cost. That is the bar for a file
landing in this directory — a rule nobody has paid for yet belongs in
`docs/status.md`, where decisions are recorded without claiming to be lessons.

| | Decision | Cost of not having it |
| --- | --- | --- |
| [001](001-unknown-failures-are-neutral-to-the-route.md) | An unknown result must not burn a healthy address | ~8× throughput, invisible below a 40-job sweep |
| [002](002-only-writers-take-the-write-lock.md) | Only writers take the write lock | API p99 1,434 ms against 19.9 ms |
| [003](003-a-feature-with-no-caller-is-not-a-feature.md) | A method with tests and no callers is invisible here | leases never renewed; long jobs ran twice |

## Writing a new one

Only when something went wrong. A convention nobody has been bitten by is a
preference, and preferences belong in a style guide, not here.

The design reasoning that has *not* yet cost anyone anything lives in
[`../docs/status.md`](../docs/status.md) under "Decisions that are expensive to
reverse" — the uncapped `yt-dlp` pin, the stdlib `logging` deviation, the
subprocess-over-library choice for comment harvests, and the SQLite queue.
Each moves here the day it bites.
