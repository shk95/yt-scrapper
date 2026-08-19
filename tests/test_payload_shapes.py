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
from pydantic import BaseModel, computed_field

from tubedepth.sources import SourceRegistry, default_registry

LOCK = Path(__file__).parent / "payload_shapes.json"

# The canonical form the lock was written with. Improving the walker changes
# every recorded shape at once, which is indistinguishable from every model
# drifting unless the lock says which form produced it — so it does, and the
# two failures get different messages. Bumping this is the only thing that lets
# recording rewrite a shape already recorded, and it is a deliberate edit here
# rather than something a regenerate can do on its own.
FORM = 2

# Required-ness is deliberately absent from a line. `model_dump_json` writes
# every field, so a stored payload always carries a value and whether the model
# calls it required never decides if old bytes read back — while a field that
# is genuinely new fires on its path line existing at all. Recording it caught
# nothing that was not already caught, and fired on annotation tidy-ups whose
# only remedy under a forcing check is discarding a month of observations.
#
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
            out.append(f"{path}[] {_type_name(items, defs)}")
            _walk(items, defs, f"{path}[]", out)
        # A fixed-length tuple is `prefixItems`, not `items`, so the branch
        # above never saw one — `tuple[int, str]` and `tuple[str, int]` were
        # the same line. Positional, because for a tuple the position is the
        # meaning.
        for index, member in enumerate(body.get("prefixItems", ())):
            out.append(f"{path}[{index}] {_type_name(member, defs)}")
            _walk(member, defs, f"{path}[{index}]", out)
        # A mapping's value type lives here. Reading only `properties` made
        # `dict[str, int]` and `dict[str, str]` identical.
        values = body.get("additionalProperties")
        if isinstance(values, dict):
            out.append(f"{path}{{}} {_type_name(values, defs)}")
            _walk(values, defs, f"{path}{{}}", out)
        for name, child in sorted(body.get("properties", {}).items()):
            where = f"{path}.{name}" if path else name
            resolved = _resolve(child, defs)
            extra = "".join(f" {key}={resolved[key]!r}" for key in CONSTRAINTS if key in resolved)
            out.append(f"{where} {_type_name(child, defs)}{extra}")
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
    if mode == "validation":
        # Computed fields are absent from the validation schema and present in
        # every payload `model_dump_json` writes, so adding one changed the
        # bytes and nothing here noticed. The serialization schema has them.
        # Recorded as a delta rather than wholesale, so the common case where
        # the two agree adds no lines.
        written = set(shape_of(model, mode="serialization"))
        lines += [f"serialized: {line}" for line in written - set(lines)]
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


def recorded() -> dict[str, Any]:
    return json.loads(LOCK.read_text()) if LOCK.exists() else {"form": FORM, "kinds": {}}


def _kinds(lock: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return dict(lock.get("kinds", {}))


def _current(registry: SourceRegistry) -> dict[str, tuple[str, list[str]]]:
    return {
        kind: (registry.get(kind).schema_version, shape_for_kind(registry, kind))
        for kind in registry.kinds()
    }


def drift(registry: SourceRegistry, lock: dict[str, Any]) -> list[str]:
    """Every kind whose current shape is not what its current version recorded."""
    if lock.get("form") != FORM:
        # Not model drift. Every shape moved because the walker did, and saying
        # "eleven kinds changed without a bump" would send someone looking for
        # eleven model changes that did not happen.
        return [
            f"the lock was written with canonical form {lock.get('form')} and this is form "
            f"{FORM}; the walker changed, not the models. Run `just record-payload-shapes`."
        ]
    kinds = _kinds(lock)
    problems: list[str] = []
    for kind, (version, shape) in sorted(_current(registry).items()):
        held = kinds.get(kind, {}).get(version)
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


def record(registry: SourceRegistry, lock: dict[str, Any]) -> dict[str, Any]:
    """Append the current shapes. Never rewrites one already recorded.

    This refusal is the load-bearing part, not the hashing. Without it the
    remedy for a failing check is to regenerate, which is exactly how a missed
    bump gets laundered into the record.
    """
    # A form change is the one thing that may rewrite history, because nothing
    # about any model moved — see FORM.
    reforming = lock.get("form") != FORM
    updated = {kind: dict(versions) for kind, versions in _kinds(lock).items()}
    for kind, (version, shape) in _current(registry).items():
        held = updated.setdefault(kind, {}).get(version)
        if held is not None and held["shape"] != shape and not reforming:
            raise AssertionError(
                f"{kind} is still at schema_version {version} and its shape changed. "
                "Bump the version in its source module, then record again — or, if the old "
                "payloads are wrong rather than merely differently shaped, add the old "
                "version to that source's `retracted_versions`."
            )
        updated[kind][version] = (
            {**(held or {}), "shape": shape} if reforming else (held or {"shape": shape})
        )
    return {"form": FORM, "kinds": updated}


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
    kinds = {kind: dict(versions) for kind, versions in lock["kinds"].items()}
    version = registry.get("video.metadata").schema_version
    kinds["video.metadata"][version] = {"shape": ["view_count integer"]}

    problems = drift(registry, {"form": FORM, "kinds": kinds})

    assert any(problem.startswith("video.metadata@") for problem in problems)


def test_recording_refuses_to_rewrite_a_shape_already_recorded() -> None:
    """The guarantee that makes a forcing check safe rather than a nuisance."""
    registry = default_registry()
    version = registry.get("video.metadata").schema_version
    tampered = {"form": FORM, "kinds": {"video.metadata": {version: {"shape": ["else"]}}}}

    with pytest.raises(AssertionError, match="Bump the version"):
        record(registry, tampered)


def test_recording_a_new_version_leaves_the_old_one_untouched() -> None:
    registry = default_registry()
    version = registry.get("video.metadata").schema_version
    history = {"form": FORM, "kinds": {"video.metadata": {"0": {"shape": ["gone now"]}}}}

    updated = record(registry, history)

    assert updated["kinds"]["video.metadata"]["0"] == {"shape": ["gone now"]}
    assert version in updated["kinds"]["video.metadata"]


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
    history = {"form": FORM, "kinds": {"video.dislikes": {"1": {"shape": ["estimate integer"]}}}}

    updated = record(registry, history)

    assert updated["kinds"]["video.dislikes"] == {"1": {"shape": ["estimate integer"]}}


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


def test_a_computed_field_changes_the_shape() -> None:
    """The sharpest blind spot, and the exact harm this module claims to catch.

    `model_json_schema(mode="validation")` omits computed fields, and
    `model_dump_json` writes them — so adding one changed every stored payload
    while the recorded shape did not move at all. That is the quiet harm in the
    docstring above: bytes that mean something new with the version unchanged.
    """

    class Before(BaseModel):
        view_count: int

    class After(BaseModel):
        view_count: int

        @computed_field
        @property
        def doubled(self) -> int:
            return self.view_count * 2

    assert shape_of(Before) != shape_of(After)


def test_a_maps_value_type_changes_the_shape() -> None:
    """`_walk` read `properties` and never `additionalProperties`, so
    `dict[str, int]` and `dict[str, str]` were the same line."""

    class Before(BaseModel):
        counts: dict[str, int] = {}

    class After(BaseModel):
        counts: dict[str, str] = {}

    assert shape_of(Before) != shape_of(After)


def test_reordering_a_tuple_changes_the_shape() -> None:
    """pydantic emits `prefixItems` for a fixed-length tuple, which the array
    branch never looked at — so swapping the element types was invisible."""

    class Before(BaseModel):
        pair: tuple[int, str]

    class After(BaseModel):
        pair: tuple[str, int]

    assert shape_of(Before) != shape_of(After)


def test_making_an_already_nullable_field_required_does_not_change_the_shape() -> None:
    """A false positive, and under a forcing check those cost history.

    `model_dump_json` writes every field, so a stored payload always carries a
    value and required-ness never decides whether old bytes read back. Adding a
    field fires on the path line existing at all, so the required marker was
    catching nothing this does not already catch — and firing on an annotation
    tidy-up whose only remedy is discarding thirty days of observations.
    """

    class Before(BaseModel):
        moved: int | None = None

    class After(BaseModel):
        moved: int | None

    assert shape_of(Before) == shape_of(After)
