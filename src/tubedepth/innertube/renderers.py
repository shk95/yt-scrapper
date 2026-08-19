"""Reading InnerTube responses without pretending to understand their shape.

Two rules, and both exist because of specific ways this goes wrong.

Search by renderer name, never by path. YouTube reshuffles the containers
around a renderer far more often than it renames the renderer itself: the
related-video list used to sit under
twoColumnWatchNextResults.secondaryResults.results[].compactVideoRenderer and
today it is a lockupViewModel somewhere else entirely. A fixed-path reader
returns nothing for that, which is indistinguishable from a video with no
related videos.

Distinguish "nothing here" from "we cannot read this". An empty result is only
accepted when the response itself says it is empty. yt-dlp returns an empty
list for a community tab it can no longer parse, and an empty list nobody
questions is how a broken scraper stays deployed for weeks.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from ..errors import ExtractionError

RENDERER_SUFFIXES = ("Renderer", "ViewModel", "Model")


def find_all(node: Any, name: str) -> Iterator[Mapping[str, Any]]:
    """Yield every mapping stored under `name`, at any depth.

    A matching key may hold a list rather than one mapping — YouTube keeps the
    channel subscriber string in `metadataParts`, which is a list of entries.
    Descending only into mappings walks straight past it and reports the field
    as absent, which is how "no subscriber count" gets confused with "we cannot
    read it".
    """
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key == name:
                if isinstance(value, Mapping):
                    yield value
                elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
                    for entry in value:
                        if isinstance(entry, Mapping):
                            yield entry
            yield from find_all(value, name)
    elif isinstance(node, Sequence) and not isinstance(node, str | bytes):
        for item in node:
            yield from find_all(item, name)


def observed_renderers(node: Any) -> list[str]:
    """Every renderer-shaped key present. The first thing to look at when a parser breaks."""
    seen: set[str] = set()

    def walk(current: Any) -> None:
        if isinstance(current, Mapping):
            for key, value in current.items():
                if key.endswith(RENDERER_SUFFIXES):
                    seen.add(key)
                walk(value)
        elif isinstance(current, Sequence) and not isinstance(current, str | bytes):
            for item in current:
                walk(item)

    walk(node)
    return sorted(seen)


def flatten_text(node: Any) -> str | None:
    """Read a display string out of any of the four shapes YouTube uses.

    All four are live in the same response today, so this cannot be reduced to
    one: simpleText is the legacy form, runs is the common one, content belongs
    to the viewModel family, and a bare string turns up in newer surfaces.
    """
    if node is None:
        return None
    if isinstance(node, str):
        return node
    if isinstance(node, Mapping):
        if "simpleText" in node:
            return str(node["simpleText"])
        if "content" in node and isinstance(node["content"], str):
            return str(node["content"])
        runs = node.get("runs")
        if isinstance(runs, Sequence):
            return "".join(str(run.get("text", "")) for run in runs if isinstance(run, Mapping))
    return None


def collect(
    node: Any,
    *,
    accepted: Sequence[str],
    contract: str,
    empty_markers: Sequence[str] = (),
) -> list[Mapping[str, Any]]:
    """Find every renderer whose key is in `accepted`.

    An empty result is allowed only when the response says it is empty — one of
    `empty_markers` is present, such as the message renderer YouTube uses for a
    channel with no community posts. Otherwise this raises, because a silent
    empty list and a genuine absence look identical, and that ambiguity is what
    keeps a broken parser in production.

    Older names stay in `accepted` on purpose, so a rollback on YouTube's side
    does not break the parser in the other direction.
    """
    found = [entry for name in accepted for entry in find_all(node, name)]
    if found:
        return found
    if any(next(find_all(node, marker), None) is not None for marker in empty_markers):
        return []
    observed = observed_renderers(node)
    raise ExtractionError(f"{contract}: expected one of {list(accepted)}, observed {observed[:12]}")
