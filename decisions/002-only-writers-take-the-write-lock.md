# Only writers take the write lock

**The rule.** `Database.session()` opens a transaction that takes SQLite's
write lock on its first statement, which is what makes the queue's claim safe.
Anything that only reads uses `Database.session(readonly=True)`, which takes no
lock and is refused if it tries to write.

**What went wrong without it.** The engine emits `BEGIN IMMEDIATE` on the
`begin` event, so *every* transaction was a writer — including a route that
counted two rows. Twelve concurrent clients against a worker running 22
transcript jobs put `GET /healthz` at a p99 of 1,434 ms while `GET /v1/sources`,
which touches no database at all, sat at 335 ms under the same load. WAL exists
precisely so that readers never block writers, and one event handler was opting
out of it on every route.

After: 19.9 ms. Seventy-two times better on the tail, from a change that adds
no cleverness — it stops removing a guarantee SQLite already offers.

The same mistake had already been made once, inside `_repair_existing_tables`:
reflecting the schema on one connection while altering on another deadlocked
against itself, because both transactions were IMMEDIATE.

**Why a separate engine rather than a flag.** The read-only engine has no
IMMEDIATE hook to forget and sets `PRAGMA query_only=ON` once per connection.
A session that took no write lock but accepted writes is the one shape that
must not exist: two of those interleave exactly the way IMMEDIATE was added to
prevent.

**What would have to change.** Moving to Postgres. There the claim becomes
`FOR UPDATE SKIP LOCKED`, readers never block regardless, and the distinction
stops earning anything. Until then it is not a tuning knob — it is the
difference between an API that stays responsive under load and one that queues
behind the worker.
