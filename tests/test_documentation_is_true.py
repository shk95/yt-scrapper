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
from pathlib import Path

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
]

# The capability table, marked rather than located by heading. The kinds are
# named elsewhere in these documents — `video.dislikes` and `channel.profile`
# appear where the limits are explained, which is a record of what is *not*
# available, and a check that could not tell those apart would forbid
# documenting a removal.
KINDS_REGION = re.compile(r"<!-- kinds:start -->(.*?)<!-- kinds:end -->", re.DOTALL)

CHANGELOG_RELEASE = re.compile(r"^## \[([^\]]+)\]", re.MULTILINE)


def served_paths() -> set[str]:
    from tubedepth.api.application import create_application
    from tubedepth.database import Database
    from tubedepth.payload_store import PayloadStore

    application = create_application(
        database=Database(Path("/tmp/tubedepth-doc-check.db")),
        payloads=PayloadStore(Path("/tmp/tubedepth-doc-check")),
    )
    # From the OpenAPI document rather than `application.routes`: recent
    # FastAPI keeps an included router as one object instead of flattening its
    # routes, so walking `routes` finds `/healthz` and nothing under `/v1`.
    # The schema is also the public answer to "what does this serve".
    return set(application.openapi()["paths"])


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
def test_every_v1_route_in_a_worked_example_is_served(document: Path) -> None:
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
    served = served_paths()
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
    listed = set(re.findall(r"`((?:video|channel|playlist|search)\.[a-z_]+)`", region.group(1)))

    assert registered <= listed, f"{document.name} omits: {sorted(registered - listed)}"
    assert listed <= registered, (
        f"{document.name} lists kinds that do not exist: {sorted(listed - registered)}"
    )


@pytest.mark.parametrize(
    "document", [ROOT / "docs" / "api.md", ROOT / "docs" / "api.ko.md"], ids=lambda p: p.name
)
def test_the_api_reference_documents_every_route_that_is_served(document: Path) -> None:
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

    undocumented = {normalise(path) for path in served_paths()} - mentioned
    assert not undocumented, f"{document.name} does not document: {sorted(undocumented)}"


@pytest.mark.parametrize(
    "document", [ROOT / "docs" / "api.md", ROOT / "docs" / "api.ko.md"], ids=lambda p: p.name
)
def test_the_api_reference_documents_every_error_code_the_api_can_return(document: Path) -> None:
    """Error codes are the part of an HTTP contract a client writes a branch
    against, so an undocumented one is a branch nobody wrote."""
    if not document.exists():
        pytest.skip(f"{document.name} does not exist")

    from tubedepth.api.application import STATUS_BY_ERROR

    text = document.read_text()
    missing = {label for _, _, label in STATUS_BY_ERROR if f"`{label}`" not in text}
    assert not missing, f"{document.name} does not document error codes: {sorted(missing)}"


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
