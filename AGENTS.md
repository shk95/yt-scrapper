# Working on this repository

An **asynchronous job-queue API** that collects the YouTube video and channel
data the official Data API does not expose. Python + FastAPI + SQLAlchemy +
SQLite, the package is `tubedepth`, everything runs through `uv`.

*[한국어](AGENTS.ko.md)*

## Every session starts here

1. `tool/doctor.sh` — the toolchain and the git hooks. Do not skip it: a clone
   has no hooks until `core.hooksPath` is set, and an old SQLite tells you
   inside a worker, as an `OperationalError`.
2. `gh issue list --label blocked` — work an earlier session could not finish
   because it needed something this host did not have.
3. `docs/status.md` — where things stand, and the decisions that are expensive
   to reverse.

## Where the work is

**`gh api repos/:owner/:repo/milestones` — the milestones are the work that
has an order.** Each milestone's description says why that work exists; each
issue says what to do. An open issue with no milestone is standalone work.

The plan's M0–M9 in `docs/plan.md` are **all finished and kept only as a
record** — do not pick work from there.

**Issues and `docs/status.md` hold different things.** Issues hold state (what
is open, who is doing what); status.md holds the reasoning (why it was decided
this way, what was measured). Read only one and you are missing either the
*what* or the *why*. When opening an issue, link the relevant section of
status.md; when a decision is expensive to reverse, write it in status.md —
a closed issue is not read again.

**Write links as absolute URLs.** `../blob/dev/…` in an issue body resolves on
the issue page and does not resolve in a notification email or for anything
reading through the API.

**When something breaks in a way that makes no sense**, grep
`docs/troubleshooting.md` for the error text before investigating. The headings
are the actual messages. Do not read it from the top — it is a lookup table.

## Rules that are expensive to break

- **yt-dlp is pinned with no upper bound.** The missing `<` in
  `yt-dlp>=2026.7` is not an oversight. The real fix for a YouTube breakage is
  almost always an upgrade, and a cap turns the one-line `just update-ytdlp`
  into "edit pyproject first". **When extraction breaks, the first move is
  `just update-ytdlp`, not debugging this code.**
- **Never store a URL with an expiry.** Caption `timedtext`/`json3` URLs and
  the signed `googlevideo.com` URLs in yt-dlp's `formats` expire within hours.
  In an artifact that guarantees a later 403; in a fixture, pre-commit's
  gitleaks reads it as a credential. The transcript job *uses and discards* the
  URL.
- **Never make an empty result and a mismatched parser the same thing.** The
  InnerTube parsers walk by renderer name rather than by fixed path, and raise
  `ExtractionError` when the expected renderer count is zero *and* the response
  carries no marker saying it is empty of its own accord. A silent `[]` is how
  a broken scraper stays deployed for weeks.
- **`ExtractionError` — a parser problem — never touches egress health.** The
  day YouTube renames a renderer, a misfiring classifier quarantining every
  address must be structurally impossible. The classifier defaults to
  `NEUTRAL`: an unrecognised failure does not burn a healthy address. See
  `decisions/001`.
- **Do not construct `httpx.AsyncClient(` or `YoutubeDL(` outside
  `src/tubedepth/egress/`.** An architecture test greps for it. When two
  transport layers disagree about whether a proxy is in use, the disagreement
  is invisible and it leaks the origin IP.
- **WireGuard config lives outside the repository**, in
  `~/.config/tubedepth/wireguard/` (0700). The rendered runtime config goes to
  `$XDG_RUNTIME_DIR` (tmpfs, 0600) and is removed on exit. `.gitignore` is a
  backstop, not a defence — a key that reaches history survives deleting the
  file.
- **Keep the database on a Linux filesystem.** `/mnt/c` (drvfs) does not
  reliably provide the POSIX locks WAL needs, and the symptom is intermittent
  `database is locked`. `tool/doctor.sh` checks it.

## Workflow

**Code, comments, docstrings and commit messages are all written in English.**

Documentation splits by who reads it. **Four documents are read from outside
this repository — `README.md`, `docs/api.md`, `CHANGELOG.md` and this file —
so English is the original and the `.ko.md` beside it is the translation. Edit
both.** A translation updated on one side only is a wrong document, and the
mechanically checkable parts (routes, kinds, error codes, versions) are checked
against both by `tests/test_documentation_is_true.py`. What a check reads is
marked with an HTML comment such as `<!-- kinds:start -->` rather than located
by heading — headings are the part that gets translated.

The remaining documents (status, troubleshooting, definition-of-done,
releasing, plan) are for contributors and exist **in Korean only**. Technical
nouns stay in English on both sides (`revision`, `egress`, `lane`, `renderer`).

The version is written in exactly one place, `src/tubedepth/__init__.py`. The
procedure for raising it is in [`docs/releasing.md`](docs/releasing.md), and
raising it alone makes a test complain that the CHANGELOG disagrees.

Branches: `master` (releases) ← `dev` (integration, the default) ←
`feature/<name>` · `fix/<name>`. Branch from `dev` and merge to `dev`. Do not
commit to `master` directly.

Commits are [Conventional Commits](https://www.conventionalcommits.org); the
`commit-msg` hook refuses anything else. **Every commit stands on its own with
`just check` green** — a commit that only works with the next one is a commit
nobody can bisect through.

Finished means `docs/definition-of-done.md` is satisfied. When an item cannot
be checked on this host, open an issue labelled `blocked` and
`blocked/<what-is-missing>` rather than skipping it quietly.

## This host

- WSL2 (Ubuntu 26.04), 16 CPU / 15 GiB. `systemctl --user` works.
- **Docker is available; passwordless sudo is not.** Checked 2026-08-20:
  server 29.6.2, compose v5.3.1, works without sudo and has pulled images. This
  line read "no Docker" for a long time, and two decisions rest on that false
  premise — choosing **wireproxy** over Gluetun for the egress proxy
  (userspace WireGuard, no root: `nix profile install nixpkgs#wireproxy`), and
  the description of milestone 1. **Both need deciding again.**
- **The wall clock jumps** after Windows sleep and resume. Intervals, windows
  and quarantine deadlines all use `time.monotonic()`.
- The direct line is a residential IP (KT), which makes it **the best egress
  this has against YouTube.** A VPN exit is a datacenter address and the first
  thing a bot check looks at, so exits are for the third-party (SponsorBlock)
  lane by default.

## Parallel sessions

Use a worktree rather than switching branches:

```sh
tool/worktree.sh new <name> feature
tool/worktree.sh list
tool/worktree.sh done <name>
```

**What does not parallelise**: the one SQLite database file, the port range
wireproxy binds (`27100+`), and ProtonVPN's concurrent-connection quota. One
session at a time holds those.

## Layout

| path | what is there |
| --- | --- |
| `src/tubedepth/sources/` | one kind of data = one module. A new source is one file here plus one import line in `__init__.py` |
| `src/tubedepth/egress/` | the proxy pool. **The only place a transport client is constructed** |
| `src/tubedepth/services/` | business rules, shared by the CLI and the API |
| `src/tubedepth/api/` | a thin layer over the services. No business logic here |
| `tests/fixtures/` | recorded responses. What lets CI run with no network |
| `docs/api.md` | the REST reference. A new route means editing this and `api.ko.md`, or CI fails |
| `docs/definition-of-done.md` | what "done" means, per milestone |
| `docs/releasing.md` | how a release is cut, and where the version lives |
| `docs/status.md` | where things stand, and the decisions behind them |
| `docs/shared-postgres.md` | the rules for sharing one PostgreSQL between services. Fleet-wide, so it applies outside this repository too |
| `docs/troubleshooting.md` | errors that have already cost someone an afternoon. Grep it, do not read it |
| `CHANGELOG.md` | what changed per release. Accumulates under `Unreleased` and is fixed at release time |
