# Definition of done

Items must be observations, not qualities. "Works correctly" cannot be checked;
"returns 404 for a deleted record" can. If an item cannot be observed, it is not
a definition of done — it is a hope.

Code that compiles is not finished work. This file says what "finished" means,
so a session cannot declare a milestone complete on the strength of a green
build.

**The rule for anything you cannot verify here:** do not quietly skip it and do
not claim it. Open a blocked issue naming what is missing, and say so in the
pull request.

```sh
gh issue create --label blocked --label blocked/<what-is-missing> \
  --title "<milestone>: <what still needs checking>" \
  --body "<what was built, what was tested, and the steps to close this>"
```

---

## Every change

- [ ] `tool/checks/format` and `tool/checks/lint` are clean
- [ ] `tool/checks/test` passes with the socket guard active
- [ ] New behaviour has a test — or a note in the pull request saying which host
      capability makes one impossible
- [ ] Commits follow Conventional Commits
- [ ] `docs/status.md` updated if a decision was made that is expensive to
      reverse, with the reasoning — not just the conclusion
- [ ] No committed file contains a signed `googlevideo.com` URL, a caption URL,
      or WireGuard key material

---

## M0 — Repository skeleton

- [ ] A fresh clone runs `uv sync --extra dev` without error
- [ ] `tool/doctor.sh` on a clone with no `core.hooksPath` exits non-zero and
      prints the exact command that fixes it
- [ ] `tool/doctor.sh` reports whether `TUBEDEPTH_DATABASE_URL` is set and
      whether the PostgreSQL server it names is reachable, failing on either
      gap
- [ ] `git commit -m "bad message"` is rejected by the `commit-msg` hook
- [ ] `tool/checks/test` exits 69 on a host with no `uv`, and exits 1 when
      `REQUIRE_NATIVE=1` is set
- [ ] `just check` is green
- [ ] `just --list` shows every recipe with a one-line description

## M1 — Domain core and egress skeleton

- [ ] `tubedepth job submit --kind static.echo` then `tubedepth job show <id>`
      reports `succeeded`
- [ ] Two workers started against one seeded queue never run a job twice
- [ ] A worker killed with SIGKILL mid-job has that job requeued once its lease
      expires, with the attempt counted
- [ ] Cancelling a `queued` job moves it to `cancelled` without the source
      ever running
- [ ] The AIMD controller halves its window on a throttle and grows it by one
      per full window of successes — asserted with an injected clock and a
      seeded RNG, with no sleeping and no network
- [ ] A quarantined egress is not selected on the next attempt
- [ ] `test_no_module_outside_the_egress_package_constructs_a_transport_directly`
      passes

## M2 — PostgreSQL: join the fleet's shared server (#14, #15, #16)

- [ ] `tubedepth_runtime` is refused `CREATE TABLE tubedepth.anything (id int)`
      with a permission-denied error
- [ ] `alembic_version` is in the `tubedepth` schema, not `public`
- [ ] `SELECT tablename FROM pg_tables WHERE schemaname = 'public'` returns no
      row this project created
- [ ] A SQLite index seeded with one row in each of the six tables round-trips
      through `tubedepth transfer` to a PostgreSQL target with every column of
      every row equal — not merely the row count
- [ ] `tubedepth prune` against a store with payload files and zero artifact
      rows exits non-zero and deletes nothing, unless
      `--sweep-without-an-index` is given
- [ ] Opening a migrated database through `_database()` (any CLI command)
      issues no `CREATE`/`ALTER`/`DROP`/`TRUNCATE` statement — asserted by
      hooking every engine's `before_cursor_execute`, not inferred
- [ ] `Database(url)` raises `ConfigurationError` for a SQLite URL;
      `Database(url, allow_sqlite_source=True)` still opens one
- [ ] A connection whose `search_path` does not lead with `tubedepth` is
      refused by `verify_placement()` before any query runs
- [ ] Running `alembic` autogenerate against a database holding a foreign
      schema with a sentinel table does not propose touching it
- [ ] Every column of every table under `information_schema.columns` for
      `table_schema = 'tubedepth'` has `data_type = 'timestamp with time
      zone'` for each instant column — none is `timestamp without time zone`
- [ ] `TUBEDEPTH_DATABASE_URL` unset makes every command refuse before
      opening a connection, naming the variable, rather than falling back to
      a file

## M4.5 — Egress pool

- [ ] Two wireproxy egresses start and each reports a public address **different
      from `direct`**
- [ ] An egress whose probe returns the same address as `direct` is marked
      failed and is not selectable
- [ ] Killing a wireproxy process causes a restart with backoff; five crashes in
      ten minutes disables the egress and `/healthz` says so
- [ ] Third-party jobs alternate across every ready egress
- [ ] An injected bot-check quarantines only the egress that saw it, and the
      retry runs on a different one
- [ ] A SponsorBlock 404 leaves egress health unchanged
- [ ] The rendered wireproxy config is outside the repository, mode 0600, and is
      removed when the egress stops

## M5 — Comment harvest

- [ ] A harvest of a video with over 1,000 comments completes with replies
      linked to their parents and the pinned/hearted/verified flags present
- [ ] Cancelling a running harvest terminates its subprocess within five seconds
      and the job reports `cancelled`
- [ ] A segment lookup queued behind three running harvests starts within one
      second — measured, not assumed
- [ ] The payload is a gzip file on disk and no multi-megabyte row exists in
      the database
