"""Assertions that the documentation still describes this project.

Two documentation defects were found by reading rather than by any check, and
both were in the first thing a reader meets. The README's opening example
called a route that never existed, and its milestone table said the project had
not started while every milestone was done.

Prose cannot be tested in general. These check the narrow, mechanical claims —
route names, command names, kind names, error codes, version numbers — which is
exactly the class both defects fell into.

Translations make that worse rather than better: a second copy of every
mechanical claim, in a language the person editing the first copy may not read.
So every check here runs against the translation as well as the original, and
the region a check reads is marked with an HTML comment rather than found by
its heading — a heading is prose, and prose is the part that gets translated.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parent.parent

# Documents that make mechanical claims about this project. Both members of a
# translated pair appear, because a stale translation is a wrong document.
DOCUMENTS = [
    ROOT / "README.md",
    ROOT / "README.ko.md",
    ROOT / "docs" / "api.md",
    ROOT / "docs" / "api.ko.md",
    ROOT / "docs" / "status.md",
    ROOT / "docs" / "troubleshooting.md",
    # AGENTS is read from outside this repository — by whoever is contributing,
    # and by every coding agent pointed at it — which is why it became an
    # English original with a translation rather than staying Korean-only. It
    # names commands and paths, so it makes mechanical claims like the rest.
    ROOT / "AGENTS.md",
    ROOT / "AGENTS.ko.md",
]

# Documents this project promises exist: linked from other documents, or named
# by the workflow. A dead link in a README is the cheapest possible bug to
# prevent and one of the more embarrassing to ship.
REQUIRED = [
    ROOT / "README.md",
    ROOT / "README.ko.md",
    ROOT / "docs" / "api.md",
    ROOT / "docs" / "api.ko.md",
    ROOT / "CHANGELOG.md",
    ROOT / "CHANGELOG.ko.md",
    ROOT / "docs" / "releasing.md",
    ROOT / "AGENTS.md",
    ROOT / "AGENTS.ko.md",
    # Linked from both READMEs and from AGENTS, and the document the PostgreSQL
    # milestone rests on. It was reachable from nothing at all until 2026-08-20.
    ROOT / "docs" / "shared-postgres.md",
]

# The capability table, marked rather than located by heading. The kinds are
# named elsewhere in these documents — `video.dislikes` and `channel.profile`
# appear where the limits are explained, which is a record of what is *not*
# available, and a check that could not tell those apart would forbid
# documenting a removal.
KINDS_REGION = re.compile(r"<!-- kinds:start -->(.*?)<!-- kinds:end -->", re.DOTALL)

# The HTTP error table, and the job `error_code` vocabulary beneath it. Marked
# for the same reason the kinds are: both are lists of backticked words in a
# document that is otherwise full of backticked words, and only inside the
# marks can a check say "exactly these and no others".
ERROR_CODES_REGION = re.compile(
    r"<!-- error-codes:start -->(.*?)<!-- error-codes:end -->", re.DOTALL
)
JOB_ERROR_CODES_REGION = re.compile(
    r"<!-- job-error-codes:start -->(.*?)<!-- job-error-codes:end -->", re.DOTALL
)
BACKTICKED = re.compile(r"`([^`]+)`")

# A route section heads itself with its own operations — ``## `GET /v1/jobs```
# — and one heading covers two: ``## `GET /v1/control`, `PATCH /v1/control```.
# These are identical literals in both languages, which is what makes them
# parseable in a document whose prose is translated.
ROUTE_IN_HEADING = re.compile(r"`(GET|POST|PATCH|PUT|DELETE) (/[A-Za-z0-9_{}/-]*)`")

METHODS = frozenset({"get", "post", "patch", "put", "delete"})

CHANGELOG_RELEASE = re.compile(r"^## \[([^\]]+)\]", re.MULTILINE)

# The REST reference and its translation, which several checks below read as a
# pair. A claim corrected on one side only is a wrong document, not half a
# right one.
API_DOCUMENTS = [ROOT / "docs" / "api.md", ROOT / "docs" / "api.ko.md"]


def served_document(tmp_path: Path) -> dict[str, Any]:
    """The OpenAPI paths object: every path served, and the operations under it."""
    from tubedepth.api.application import create_application
    from tubedepth.database import Database
    from tubedepth.payload_store import PayloadStore

    # No connection is opened for route enumeration, so a URL that never
    # resolves is fine — but it does have to be PostgreSQL, since the
    # cutover (#15) `Database` refuses anything else.
    application = create_application(
        database=Database("postgresql+psycopg://u:p@h:5432/fleet"),
        payloads=PayloadStore(tmp_path / "payloads"),
    )
    # From the OpenAPI document rather than `application.routes`: recent
    # FastAPI keeps an included router as one object instead of flattening its
    # routes, so walking `routes` finds `/healthz` and nothing under `/v1`.
    # The schema is also the public answer to "what does this serve".
    return application.openapi()["paths"]


def served_paths(tmp_path: Path) -> set[str]:
    return set(served_document(tmp_path))


def served_operations(tmp_path: Path) -> set[tuple[str, str]]:
    """Every (method, path) pair served, methods lowercased as OpenAPI writes them."""
    return {
        (method, path)
        for path, operations in served_document(tmp_path).items()
        for method in operations
        if method in METHODS
    }


def documented_sections(text: str) -> dict[tuple[str, str], str]:
    """Each route section's body, keyed by every operation its heading claims.

    A section runs from its `## ` heading to the next one, so the subsections
    under it count as part of it — which is what lets a worked recipe live
    beside the endpoint it belongs to.
    """
    found: dict[tuple[str, str], str] = {}
    for part in re.split(r"^## ", text, flags=re.MULTILINE)[1:]:
        heading, _, body = part.partition("\n")
        for method, path in ROUTE_IN_HEADING.findall(heading):
            found[(method.lower(), path)] = body
    return found


def every_domain_error() -> set[str]:
    """Every `TubedepthError` subclass name, walked rather than listed.

    `__subclasses__()` answers with direct subclasses only, and
    `RateLimitedError` sits under `UpstreamError` — a flat read documents ten
    of the eleven and misses exactly the one a throttled job reports.
    """
    from tubedepth import errors

    def below(base: type) -> Iterator[type]:
        for derived in base.__subclasses__():
            yield derived
            yield from below(derived)

    return {derived.__name__ for derived in below(errors.TubedepthError)}


def normalise(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path).rstrip("/")


def matches_a_served_route(mentioned: str, served: set[str]) -> bool:
    """Whether a path a document shows is one of the routes the API serves.

    Compared segment by segment, because a template segment matches whatever a
    document wrote in its place: `/v1/jobs/{job_id}` is the route,
    `/v1/jobs/j_2f7c1d9a` is what an example shows, and `/v1/jobs/$JOB` is what
    a shell snippet shows.

    This replaced a rule that rewrote any run of eight or more alphanumerics
    into a parameter, which read `/v1/artifacts` as `/v1/{}` and reported a
    route that is served as one that is not. Matching against the real
    templates needs no guess about what an identifier looks like.
    """
    written = mentioned.strip("/").split("/")
    for route in served:
        expected = route.strip("/").split("/")
        if len(written) == len(expected) and all(
            wanted.startswith("{") or wanted == actual
            for actual, wanted in zip(written, expected, strict=True)
        ):
            return True
    return False


@pytest.mark.parametrize("document", REQUIRED, ids=lambda p: p.name)
def test_the_documents_this_project_promises_exist(document: Path) -> None:
    assert document.exists(), f"{document.relative_to(ROOT)} is missing"


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda p: p.name)
def test_every_v1_route_in_a_worked_example_is_served(document: Path, tmp_path: Path) -> None:
    """The README promised `/v1/videos/{id}/metadata` for a day.

    Only fenced examples, not prose. A document has to be able to say a route
    does *not* exist — status.md records exactly that about the convenience
    aliases — and a check that could not tell the two apart would forbid
    documenting an absence. What must be true is narrower and more useful:
    anything a reader could copy and run does run.

    Templated segments are normalised, because documentation writes a real id
    where the route has a parameter: `/v1/jobs/abc123` and `/v1/jobs/{job_id}`
    are the same route to a reader and different strings to a router.
    """
    if not document.exists():
        pytest.skip(f"{document.name} does not exist")

    examples = "\n".join(re.findall(r"```[a-z]*\n(.*?)```", document.read_text(), re.DOTALL))
    served = served_paths(tmp_path)
    mentioned = {match.rstrip("/") for match in re.findall(r"/v1/[A-Za-z0-9_{}$/-]+", examples)}

    for path in sorted(mentioned):
        assert matches_a_served_route(path, served), (
            f"{document.name} mentions {path}, which the API does not serve"
        )


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda p: p.name)
def test_every_cli_command_the_documents_mention_exists(document: Path) -> None:
    if not document.exists():
        pytest.skip(f"{document.name} does not exist")

    from tubedepth.cli import application

    registered = {
        info.name or (info.callback.__name__ if info.callback else "")
        for info in application.registered_commands
    }
    mentioned = set(re.findall(r"tubedepth ([a-z][a-z-]+)", document.read_text()))
    # Words that follow the binary name without being subcommands.
    mentioned -= {"key", "is"}

    unknown = mentioned - registered
    assert not unknown, f"{document.name} mentions `tubedepth {unknown}`, which does not exist"


@pytest.mark.parametrize(
    "document",
    [
        ROOT / "README.md",
        ROOT / "README.ko.md",
        ROOT / "docs" / "api.md",
        ROOT / "docs" / "api.ko.md",
    ],
    ids=lambda p: p.name,
)
def test_the_capability_tables_list_exactly_the_kinds_that_are_registered(document: Path) -> None:
    """A table of capabilities is the most useful thing in a README and the
    first to go stale, because adding a source does not touch it.

    Four tables now say the same thing in two languages. The one that rots is
    whichever the person adding a source does not read.
    """
    if not document.exists():
        pytest.skip(f"{document.name} does not exist")

    from tubedepth.sources import default_registry

    region = KINDS_REGION.search(document.read_text())
    assert region is not None, f"{document.name} has no <!-- kinds:start --> region"

    registered = set(default_registry().kinds())
    # The prefixes are listed rather than matched loosely, so a stray backticked
    # word in the region is not mistaken for a kind. A new target family — the
    # chart endpoint brought `trending` — has to be added here, which is a
    # deliberate speed bump on inventing one.
    listed = set(
        re.findall(r"`((?:video|channel|playlist|search|trending)\.[a-z_]+)`", region.group(1))
    )

    assert registered <= listed, f"{document.name} omits: {sorted(registered - listed)}"
    assert listed <= registered, (
        f"{document.name} lists kinds that do not exist: {sorted(listed - registered)}"
    )


@pytest.mark.parametrize("document", API_DOCUMENTS, ids=lambda p: p.name)
def test_the_api_reference_documents_every_route_that_is_served(
    document: Path, tmp_path: Path
) -> None:
    """The counterpart to the check above, and the one that catches omissions.

    That one forbids documenting a route which does not exist. This one forbids
    serving a route which is not documented — the failure that actually
    happens, because adding an endpoint does not force anyone to open a
    reference. The dashboard is exempt by construction: it is
    `include_in_schema=False`, so it is not in the document this reads.
    """
    if not document.exists():
        pytest.skip(f"{document.name} does not exist")

    text = document.read_text()
    mentioned = {
        normalise(match) for match in re.findall(r"/(?:v1|healthz)[A-Za-z0-9_{}/-]*", text)
    }

    undocumented = {normalise(path) for path in served_paths(tmp_path)} - mentioned
    assert not undocumented, f"{document.name} does not document: {sorted(undocumented)}"


@pytest.mark.parametrize("document", API_DOCUMENTS, ids=lambda p: p.name)
def test_the_api_reference_documents_every_error_code_the_api_can_return(document: Path) -> None:
    """Error codes are the part of an HTTP contract a client writes a branch
    against, so an undocumented one is a branch nobody wrote."""
    if not document.exists():
        pytest.skip(f"{document.name} does not exist")

    from tubedepth.api.application import STATUS_BY_ERROR

    text = document.read_text()
    missing = {label for _, _, label in STATUS_BY_ERROR if f"`{label}`" not in text}
    assert not missing, f"{document.name} does not document error codes: {sorted(missing)}"


@pytest.mark.parametrize("document", API_DOCUMENTS, ids=lambda p: p.name)
def test_the_api_reference_has_a_section_for_every_operation_and_for_no_other(
    document: Path, tmp_path: Path
) -> None:
    """Per operation, where the check above is per path.

    A path check passes while half its methods are undocumented: `/v1/jobs`
    serves `GET` and `POST`, `/v1/jobs/{job_id}` serves `GET` and `DELETE`,
    and either half alone satisfies "the path is mentioned". Both directions
    are asserted, because a section for something not served is an instruction
    that answers 405 to whoever follows it.

    `GET /` is exempt by construction: the dashboard is
    `include_in_schema=False`, so it is not in what this compares against.
    `GET /healthz` is not exempt — it is served, unauthenticated, and has its
    own section.
    """
    if not document.exists():
        pytest.skip(f"{document.name} does not exist")

    served = served_operations(tmp_path)
    documented = set(documented_sections(document.read_text()))

    undocumented = sorted(f"{method.upper()} {path}" for method, path in served - documented)
    invented = sorted(f"{method.upper()} {path}" for method, path in documented - served)
    assert not undocumented, f"{document.name} has no section for: {undocumented}"
    assert not invented, f"{document.name} heads a section for what is not served: {invented}"


@pytest.mark.parametrize("document", API_DOCUMENTS, ids=lambda p: p.name)
def test_the_error_table_lists_no_code_the_api_cannot_return(document: Path) -> None:
    """The other direction from the check above, which only catches omissions.

    A code the document lists and the API never emits is a branch a client
    wrote for nothing, and it survives exactly because the check that would
    have caught it only looked the other way. Scanning the whole document
    cannot do this — it is full of backticked words that are not error codes —
    so the table is marked and only what the marks contain is read. That puts
    one rule on the region: everything backticked inside it is an error code,
    which is why the meanings beside them are written without any.
    """
    if not document.exists():
        pytest.skip(f"{document.name} does not exist")

    from tubedepth.api.application import STATUS_BY_ERROR

    region = ERROR_CODES_REGION.search(document.read_text())
    assert region is not None, f"{document.name} has no <!-- error-codes:start --> region"

    listed = set(BACKTICKED.findall(region.group(1)))
    emitted = {label for _, _, label in STATUS_BY_ERROR}
    assert listed == emitted, (
        f"{document.name}'s error table and the API disagree: "
        f"listed and never emitted {sorted(listed - emitted)}, "
        f"emitted and not listed {sorted(emitted - listed)}"
    )


@pytest.mark.parametrize("document", API_DOCUMENTS, ids=lambda p: p.name)
def test_every_query_parameter_is_named_in_its_own_endpoints_section(
    document: Path, tmp_path: Path
) -> None:
    """A filter documented nowhere is a filter nobody passes.

    `GET /v1/artifacts` described five parameters in one sentence of prose and
    the sentence went stale without anyone noticing, which is the shape of
    every documentation defect this file exists for. Named in *its own*
    section rather than anywhere in the document: `kind` appears on four
    routes, and finding it once says nothing about the fifth.
    """
    if not document.exists():
        pytest.skip(f"{document.name} does not exist")

    sections = documented_sections(document.read_text())
    missing: list[str] = []
    for path, operations in served_document(tmp_path).items():
        for method, operation in operations.items():
            if method not in METHODS:
                continue
            # A missing section is the check above's to report, not this one's.
            body = sections.get((method, path), "")
            missing += [
                f"{method.upper()} {path} takes {parameter['name']}"
                for parameter in operation.get("parameters", [])
                if parameter.get("in") == "query" and f"`{parameter['name']}`" not in body
            ]

    assert not missing, f"{document.name} does not name: {sorted(missing)}"


@pytest.mark.parametrize("document", API_DOCUMENTS, ids=lambda p: p.name)
def test_the_job_error_code_vocabulary_is_the_one_the_worker_actually_writes(
    document: Path,
) -> None:
    """A second vocabulary, under a field name the first one also uses.

    A job's `error_code` is `type(error).__name__` — the worker writes it that
    way at all four of its failure sites — plus the literal `lease_expired`
    the reaper writes. The reference showed `upstream_error` in its jobs-list
    example and claimed `/healthz` reports `parse_mismatch`: values from the
    HTTP table, which no row has ever held.
    """
    if not document.exists():
        pytest.skip(f"{document.name} does not exist")

    region = JOB_ERROR_CODES_REGION.search(document.read_text())
    assert region is not None, f"{document.name} has no <!-- job-error-codes:start --> region"

    listed = set(BACKTICKED.findall(region.group(1)))
    # Every domain error, because the worker writes whichever one reached it,
    # and the one value that is not an exception at all.
    written = every_domain_error() | {"lease_expired"}
    assert listed == written, (
        f"{document.name}'s job error codes and the code disagree: "
        f"listed and never written {sorted(listed - written)}, "
        f"written and not listed {sorted(written - listed)}"
    )


def released_versions(changelog: Path) -> list[str]:
    return [name for name in CHANGELOG_RELEASE.findall(changelog.read_text())]


def test_the_package_version_is_the_newest_release_the_changelog_records() -> None:
    """One version, in one place, and a changelog that agrees with it.

    A release is three edits — the version, the changelog entry, the tag — and
    the one people forget is whichever is not in front of them. This check
    fails on two of the three.
    """
    from tubedepth import __version__

    released = [name for name in released_versions(ROOT / "CHANGELOG.md") if name != "Unreleased"]
    assert released, "CHANGELOG.md records no release"
    assert released[0] == __version__, (
        f"the package is {__version__} and CHANGELOG.md's newest release is {released[0]}"
    )


def test_both_changelogs_record_the_same_releases() -> None:
    """A translation that stops being updated stops being a translation."""
    original = released_versions(ROOT / "CHANGELOG.md")
    translated = released_versions(ROOT / "CHANGELOG.ko.md")
    assert original == translated, (
        f"CHANGELOG.md records {original} and CHANGELOG.ko.md records {translated}"
    )


def test_the_version_is_defined_in_exactly_one_place() -> None:
    """`pyproject.toml` reads the version from the package rather than
    repeating it, so bumping it is one edit and cannot half-succeed."""
    manifest = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert "version" not in manifest["project"], (
        "pyproject.toml pins a version of its own; it should be dynamic and read from the package"
    )
    assert "version" in manifest["project"].get("dynamic", []), (
        'pyproject.toml must declare `dynamic = ["version"]`'
    )


JUSTFILE = ROOT / "Justfile"


def _justfile_invocations() -> list[tuple[str, list[str]]]:
    """Every `uv run tubedepth …` a recipe body runs, as (subcommand, options).

    Recipe bodies only — a command named in a comment is prose, and the
    comments here explain why a recipe exists rather than claiming it runs.
    """
    found: list[tuple[str, list[str]]] = []
    for line in JUSTFILE.read_text().splitlines():
        stripped = line.strip()
        if not line.startswith((" ", "\t")) or "tubedepth " not in stripped:
            continue
        words = stripped.split("tubedepth ", 1)[1].split()
        if not words:
            continue
        found.append((words[0], [word for word in words[1:] if word.startswith("--")]))
    return found


def test_every_command_the_justfile_runs_exists() -> None:
    """The Justfile is a runbook, and nothing checked its claims.

    `tests/test_deployment_units.py` makes exactly this assertion about
    `deploy/*.service`, and the check above makes the subcommand half of it
    about six `.md` files. The Justfile was in neither's scope, so five of its
    twelve recipes drifted into naming a command and three options that do not
    exist — including the only documented way to re-record the fixtures the
    regression suite rests on.
    """
    from tubedepth.cli import application

    registered = {
        info.name or (info.callback.__name__ if info.callback else "")
        for info in application.registered_commands
    }

    unknown = sorted({name for name, _ in _justfile_invocations() if name not in registered})

    assert not unknown, f"the Justfile runs commands that do not exist: {unknown}"


def test_every_option_the_justfile_passes_exists() -> None:
    """The other half, and the one that catches a flag removed from a command."""
    import subprocess
    import sys

    missing: list[str] = []
    for subcommand, options in _justfile_invocations():
        if not options:
            continue
        help_text = subprocess.run(
            [sys.executable, "-m", "tubedepth.cli", subcommand, "--help"],
            capture_output=True,
            text=True,
            env={"COLUMNS": "200", "PATH": "/usr/bin:/bin"},
        ).stdout
        missing += [f"{subcommand} {option}" for option in options if option not in help_text]

    assert not missing, f"the Justfile passes options that do not exist: {missing}"


def test_every_route_the_dashboard_calls_is_served(tmp_path: Path) -> None:
    """The dashboard is the third surface that names routes, and had no check.

    `deploy/*.service` is checked for the commands it runs and the Justfile now
    is too, because a file that names something which does not exist fails only
    when someone runs it — and for the dashboard that means in a browser, on a
    page whose whole job is to be trusted when something is already wrong.

    One path is skipped on purpose: `/v1/${table}` is built from a select whose
    options are `jobs` and `artifacts`, so the literal is not in the source.
    Both of those are covered by their own entries here.
    """
    dashboard = (ROOT / "src/tubedepth/api/dashboard.html").read_text()
    served = {normalise(path) for path in served_paths(tmp_path)} | {"/healthz"}

    called = {
        normalise(re.sub(r"\$\{[^}]*\}", "{}", path))
        for path in re.findall(r'["`](/(?:v1/|healthz)[^"`?\s]*)', dashboard)
    }
    unknown = sorted(path for path in called - served if path != "/v1/{}")

    assert not unknown, f"the dashboard calls routes that are not served: {unknown}"


def test_the_two_agents_files_have_not_drifted_apart() -> None:
    """A translation gains a rule and the original does not, or the reverse.

    Structure rather than text, because the text is the part that is supposed
    to differ. Headings translate and prose translates; **the number of
    sections and the number of rules do not**, and adding a rule to one side
    only is the failure that actually happens — nobody forgets to translate a
    document they are writing, they forget the one they are not looking at.

    Aimed at the expensive-rules list in particular. That list is the reason
    this file is read at all, and an entry missing from the side someone reads
    is a rule that does not exist for them.
    """
    original = (ROOT / "AGENTS.md").read_text()
    translation = (ROOT / "AGENTS.ko.md").read_text()

    def sections(text: str) -> int:
        return len(re.findall(r"^## ", text, re.MULTILINE))

    def rules(text: str) -> int:
        # The expensive-rules list, which is the run of top-level bullets
        # between its heading and the next one. Bold-opened, since every entry
        # states its rule in bold and continuation lines do not.
        body = re.split(r"^## ", text, flags=re.MULTILINE)
        listed = [part for part in body if "`yt-dlp>=" in part]
        assert len(listed) == 1, "the expensive-rules section could not be located"
        return len(re.findall(r"^- \*\*", listed[0], re.MULTILINE))

    assert sections(original) == sections(translation), (
        f"AGENTS.md has {sections(original)} sections and AGENTS.ko.md has {sections(translation)}"
    )
    assert rules(original) == rules(translation), (
        f"AGENTS.md lists {rules(original)} expensive rules and "
        f"AGENTS.ko.md lists {rules(translation)}"
    )
