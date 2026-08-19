"""Assertions that the documentation still describes this project.

Two documentation defects were found by reading rather than by any check, and
both were in the first thing a reader meets. The README's opening example
called a route that never existed, and its milestone table said the project had
not started while every milestone was done.

Prose cannot be tested in general. These check the narrow, mechanical claims —
route names, command names, kind names — which is exactly the class both
defects fell into.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
DOCUMENTS = [ROOT / "README.md", ROOT / "docs" / "status.md", ROOT / "docs" / "troubleshooting.md"]


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
    served = {re.sub(r"\{[^}]+\}", "{}", path) for path in served_paths()}
    mentioned = {
        re.sub(r"\{[^}]+\}|\$[A-Za-z_]+|[0-9a-zA-Z_-]{8,}", "{}", match).rstrip("/")
        for match in re.findall(r"/v1/[A-Za-z0-9_{}$/-]+", examples)
    }

    for path in sorted(mentioned):
        assert path in served or f"{path}/{{}}" in served, (
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


def test_the_readme_lists_exactly_the_kinds_that_are_registered() -> None:
    """A table of capabilities is the most useful thing in a README and the
    first to go stale, because adding a source does not touch it."""
    from tubedepth.sources import default_registry

    readme = (ROOT / "README.md").read_text()
    registered = set(default_registry().kinds())
    # Only the capability table. Kinds are named elsewhere in the README —
    # `video.dislikes` and `channel.profile` appear in the limits section,
    # which explains why they are *not* available, and a check that could not
    # tell those apart would forbid documenting a removal.
    table = readme[readme.index("## 수집할 수 있는 것") : readme.index("## 왜 필요한가")]
    listed = set(re.findall(r"`((?:video|channel|playlist|search)\.[a-z_]+)`", table))

    assert registered <= listed, f"the README omits: {sorted(registered - listed)}"
    assert listed <= registered, (
        f"the README lists kinds that do not exist: {sorted(listed - registered)}"
    )
