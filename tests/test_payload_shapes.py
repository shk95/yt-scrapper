"""Nothing forces a `schema_version` bump when a payload model changes.

`tests/test_migrations.py` catches the database half of this by running
autogenerate against a migrated database and requiring it to find nothing.
There was no payload-side equivalent, and the gap is not theoretical: commit
`31e87bc` added `published_date` to `VideoMetadata` and left
`video.metadata` at `"1"`, so every artifact collected before it is still
served from cache with that field null, indistinguishable from a video that
genuinely has none.

Two harms, and they need different checks. The loud one is a model that
*rejects* old bytes — `POST /v1/jobs` then answers 500 for every target with a
cached artifact. The quiet one is a model that *accepts* them and they mean
something different or are missing a field, which is what `fingerprints.py`
says the version exists to prevent. The quiet one is the common case, so this
fires on any shape change rather than only on incompatible ones.

The lock file is append-only on purpose. If a kind's computed shape disagrees
with what is recorded for its *current* version, recording refuses — so there
is no way to go green except to bump, and a blind regenerate cannot launder a
missed one. Editing a recorded version by hand is still possible and shows up
in review as a change to a historical record, which is the right amount of
friction.

**Green here means no shape change went unrecorded. It never means no bump was
needed** — a normalizer whose output shape is unchanged can still change what
the fields mean, and nothing mechanical catches that.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from tubedepth.sources import SourceRegistry, default_registry

LOCK = Path(__file__).parent / "payload_shapes.json"

# Recorded because they change what a stored payload can hold. `title` and
# `description` are not: pydantic puts a class docstring straight into
# `description`, `src/tubedepth/schemas.py` is roughly half prose, and the
# culture here is to keep that prose current. Under a check whose only remedy
# is a bump — and a bump discards the cache and severs every series at the
# version boundary — a false positive costs history. That is what earns the
# right to make this a hard failure.
CONSTRAINTS = (
    "enum",
    "const",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "pattern",
)


def _resolve(node: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """Follow `$ref` until it is a schema.

    Inlined rather than named, so renaming a nested model class without
    changing any of its fields is not a shape change. It is not one.
    """
    seen: set[str] = set()
    while "$ref" in node:
        name = str(node["$ref"]).rsplit("/", 1)[-1]
        if name in seen:  # pragma: no cover - no self-referencing model exists yet
            return {}
        seen.add(name)
        node = defs.get(name, {})
    return node


def _type_name(node: dict[str, Any], defs: dict[str, Any]) -> str:
    resolved = _resolve(node, defs)
    if "anyOf" in resolved:
        return "|".join(sorted(_type_name(member, defs) for member in resolved["anyOf"]))
    named = resolved.get("type")
    if named == "string" and resolved.get("format"):
        return f"string({resolved['format']})"
    if named == "array":
        return "list"
    return str(named) if named else "any"


def _bodies(node: dict[str, Any], defs: dict[str, Any]) -> list[dict[str, Any]]:
    """The sub-schemas whose fields belong at this path.

    A `X | None` field is one field, not two, so the union is unwrapped and the
    null arm dropped.
    """
    resolved = _resolve(node, defs)
    if "anyOf" in resolved:
        members = [_resolve(member, defs) for member in resolved["anyOf"]]
        return [member for member in members if member.get("type") != "null"]
    return [resolved]


def _walk(node: dict[str, Any], defs: dict[str, Any], path: str, out: list[str]) -> None:
    for body in _bodies(node, defs):
        if body.get("type") == "array" and "items" in body:
            items = body["items"]
            out.append(f"{path}[] {_type_name(items, defs)} required")
            _walk(items, defs, f"{path}[]", out)
        for name, child in sorted(body.get("properties", {}).items()):
            where = f"{path}.{name}" if path else name
            state = "required" if name in set(body.get("required", ())) else "optional"
            resolved = _resolve(child, defs)
            extra = "".join(f" {key}={resolved[key]!r}" for key in CONSTRAINTS if key in resolved)
            out.append(f"{where} {_type_name(child, defs)} {state}{extra}")
            _walk(child, defs, where, out)


def shape_of(model: type[BaseModel], *, mode: str = "validation") -> list[str]:
    """One line per field path, sorted, carrying only what changes the bytes.

    Defaults are omitted deliberately, and it costs a true positive.
    `model_dump_json` writes every field, so a stored payload always carries a
    value and a default never applies when reading one back — while including
    them would fire on `tags: list[str] = []` becoming
    `Field(default_factory=list)`, which is a pure refactor.
    """
    schema = model.model_json_schema(mode=mode)  # type: ignore[arg-type]
    defs = schema.get("$defs", {})
    lines: list[str] = []
    _walk(schema, defs, "", lines)
    return sorted(set(lines))


def shape_for_kind(registry: SourceRegistry, kind: str) -> list[str]:
    """A kind's shape, with a composite's parts expanded.

    `VideoBundle.parts` is `dict[str, Any]`, so every part model is invisible to
    the schema — while `CollectionService._assemble` stores each part's
    `model_dump()` inside it. Without this a `video.metadata` change genuinely
    changes `video.bundle` payloads and `video.bundle` stays at "1" forever.
    """
    source = registry.get(kind)
    lines = shape_of(source.payload_model)
    for part in getattr(source, "parts", ()) or ():
        held = registry.get(part)
        prefix = f"parts.{part}@{held.schema_version}"
        lines += [f"{prefix}.{line}" for line in shape_of(held.payload_model)]
    return sorted(set(lines))


def recorded() -> dict[str, dict[str, Any]]:
    return json.loads(LOCK.read_text()) if LOCK.exists() else {}


def _current(registry: SourceRegistry) -> dict[str, tuple[str, list[str]]]:
    return {
        kind: (registry.get(kind).schema_version, shape_for_kind(registry, kind))
        for kind in registry.kinds()
    }


def drift(registry: SourceRegistry, lock: dict[str, dict[str, Any]]) -> list[str]:
    """Every kind whose current shape is not what its current version recorded."""
    problems: list[str] = []
    for kind, (version, shape) in sorted(_current(registry).items()):
        held = lock.get(kind, {}).get(version)
        if held is None:
            problems.append(f"{kind}@{version} has no recorded shape")
            continue
        if held["shape"] != shape:
            diff = "\n".join(
                difflib.unified_diff(
                    held["shape"], shape, lineterm="", n=0, fromfile="recorded", tofile="now"
                )
            )
            problems.append(f"{kind}@{version} changed shape without a bump:\n{diff}")
    return problems


def record(registry: SourceRegistry, lock: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Append the current shapes. Never rewrites one already recorded.

    This refusal is the load-bearing part, not the hashing. Without it the
    remedy for a failing check is to regenerate, which is exactly how a missed
    bump gets laundered into the record.
    """
    updated = {kind: dict(versions) for kind, versions in lock.items()}
    for kind, (version, shape) in _current(registry).items():
        held = updated.setdefault(kind, {}).get(version)
        if held is not None and held["shape"] != shape:
            raise AssertionError(
                f"{kind} is still at schema_version {version} and its shape changed. "
                "Bump the version in its source module, then record again — or, if the old "
                "payloads are wrong rather than merely differently shaped, add the old "
                "version to that source's `retracted_versions`."
            )
        updated[kind].setdefault(version, {"shape": shape})
    return updated


def test_every_shipped_source_has_its_current_shape_recorded() -> None:
    """The check. Run `just record-payload-shapes` after a deliberate bump."""
    problems = drift(default_registry(), recorded())

    assert not problems, "\n\n".join(problems)


def test_a_model_changed_without_a_bump_is_reported_by_kind() -> None:
    """A suite that only ever sees a passing lock cannot say it would catch one.

    Same principle as `tests/test_renderer_regression.py`: the guard is only
    worth having if something proves it fires.
    """
    registry = default_registry()
    lock = recorded()
    tampered = {kind: dict(versions) for kind, versions in lock.items()}
    version = registry.get("video.metadata").schema_version
    tampered["video.metadata"][version] = {"shape": ["view_count integer required"]}

    problems = drift(registry, tampered)

    assert any(problem.startswith("video.metadata@") for problem in problems)


def test_recording_refuses_to_rewrite_a_shape_already_recorded() -> None:
    """The guarantee that makes a forcing check safe rather than a nuisance."""
    registry = default_registry()
    version = registry.get("video.metadata").schema_version
    tampered = {"video.metadata": {version: {"shape": ["something else entirely"]}}}

    with pytest.raises(AssertionError, match="Bump the version"):
        record(registry, tampered)


def test_recording_a_new_version_leaves_the_old_one_untouched() -> None:
    registry = default_registry()
    version = registry.get("video.metadata").schema_version
    history = {"video.metadata": {"0": {"shape": ["gone now"]}}}

    updated = record(registry, history)

    assert updated["video.metadata"]["0"] == {"shape": ["gone now"]}
    assert version in updated["video.metadata"]


def test_reordering_fields_or_rewriting_a_docstring_does_not_change_a_shape() -> None:
    """Under a forcing check a false positive costs thirty days of history.

    The only way to satisfy this check is a bump, and a bump discards the cache
    and severs every series at the version boundary. So the shape must be blind
    to prose and to declaration order, and this says so.
    """

    class Before(BaseModel):
        """One thing."""

        alpha: str
        beta: int | None = None

    class After(BaseModel):
        """Something else entirely, rewritten for clarity."""

        beta: int | None = None
        alpha: str

    assert shape_of(Before) == shape_of(After)


def test_a_field_added_two_models_down_changes_its_kinds_shape() -> None:
    class Leaf(BaseModel):
        kept: str

    class Grown(BaseModel):
        kept: str
        added: int | None = None

    class Before(BaseModel):
        items: list[Leaf] = []

    class After(BaseModel):
        items: list[Grown] = []

    assert shape_of(Before) != shape_of(After)


def test_a_bundles_recorded_shape_includes_every_part_it_declares() -> None:
    """`parts` is `dict[str, Any]`, so without expansion a bundle can never
    drift — while its payloads embed each part's dump."""
    registry = default_registry()

    shape = shape_for_kind(registry, "video.bundle")

    for part in getattr(registry.get("video.bundle"), "parts", ()):
        assert any(line.startswith(f"parts.{part}@") for line in shape), part


def test_a_kind_no_longer_registered_keeps_its_recorded_history() -> None:
    """That history is what an upcaster or a backfill would need, and it is the
    one thing that cannot be reconstructed after the fact."""
    registry = default_registry()
    history = {"video.dislikes": {"1": {"shape": ["estimate integer required"]}}}

    updated = record(registry, history)

    assert updated["video.dislikes"] == {"1": {"shape": ["estimate integer required"]}}


def test_recording_the_lock_when_asked_to(request: pytest.FixtureRequest) -> None:
    """Not a check — the writer, kept beside what it writes.

    Skipped unless `--record-payload-shapes` is given, so an ordinary run can
    never quietly make itself pass.
    """
    if not request.config.getoption("--record-payload-shapes"):
        pytest.skip("pass --record-payload-shapes to update the lock")

    LOCK.write_text(
        json.dumps(record(default_registry(), recorded()), indent=1, sort_keys=True) + "\n"
    )
