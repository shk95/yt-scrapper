# Decisions

One file per convention: what the rule is, what went wrong without it, and what
would have to change for it to stop being right.

That last part matters. A decision recorded without its conditions becomes
dogma, and dogma is what people delete when it gets in the way. If the condition
no longer holds, the rule should go — deliberately, not by accident.

| | Decision |
| --- | --- |
| _(none yet)_ | |

## Writing a new one

Only when something went wrong. A convention nobody has been bitten by is a
preference, and preferences belong in a style guide, not here.

The design reasoning that has *not* yet cost anyone anything lives in
[`../docs/status.md`](../docs/status.md) under "Decisions that are expensive to
reverse" — the uncapped `yt-dlp` pin, the stdlib `logging` deviation, the
subprocess-over-library choice for comment harvests, and the SQLite queue.
Each moves here the day it bites.
