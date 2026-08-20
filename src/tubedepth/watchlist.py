"""The watch list `tubedepth watch` reads: one typed directive per line.

```
video    dQw4w9WgXcQ
channel  @director_pihyunjung
search   kpop debut
trending KR
```

The type is written down because the target alone cannot carry it. A bare-id
list — which is what `cli._targets_from_file` still reads for `enqueue
--from-file`, and which stays for that — works only while every line means the
same kind, and the release gate asks for channels, search keywords and trending
regions in one schedule. `UCxxx`, `@handle` and `kpop debut` are three target
types that no amount of inspection separates reliably from the string alone.

**A line is `<directive><whitespace><target>`, split on the first run of
whitespace only.** The target is everything after it, stripped of what
surrounds it and otherwise untouched — `search kpop debut` is a query for
`kpop debut`, not a query for `kpop` and a stray word. Normalising the target
is `normalize_target`'s job where the job is created, not the parser's.

**There is no inline comment syntax, and this is deliberate — do not add one.**
Only a line whose first non-whitespace character is `#` is a comment. `#` is
how a real search query is written (`search #shorts recap`), so a parser that
treated it as the start of a comment anywhere on the line would silently
truncate the most ordinary thing anyone would put in this file. Truncated
queries return results, so nothing downstream could ever notice.

**An unrecognised directive raises rather than being skipped.** A watch list is
read by a timer nobody watches; a typo that silently collects nothing reports
success every hour while the history stops moving. The line number is in the
message because the operator is looking at the file in an editor.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .errors import ValidationError


@dataclass(frozen=True, slots=True)
class Directive:
    """One line of the list, as the job it will become.

    `line` is the line number in the file, counting blank and comment lines,
    so it is the number the operator's editor shows.
    """

    kind: str
    target: str
    follow_up: str | None
    line: int


# The one table. The parser and the error message both read it, so they cannot
# disagree about which directives exist — a list of names copied into the
# refusal is a list that goes stale on the day a directive is added.
#
# A listing directive carries `video.metadata` as its follow-up because a
# listing on its own is an enumeration, not a collection: without it a watch
# list of channels records which videos exist and nothing about any of them.
DIRECTIVES: Mapping[str, tuple[str, str | None]] = {
    "video": ("video.metadata", None),
    "channel": ("channel.videos", "video.metadata"),
    "search": ("search.videos", "video.metadata"),
    "trending": ("trending.videos", "video.metadata"),
}

COMMENT = "#"


def read_watchlist(path: Path) -> list[Directive]:
    """Parse the whole file, or refuse the whole file.

    Nothing is returned until every line has parsed, so a list with a typo in
    it queues nothing at all rather than the half above the typo. A partial
    sweep is the harder failure to see: the jobs that did run make the pass
    look like it worked.

    A file that cannot be read is refused rather than treated as empty, the
    same rule and for the same reason as the bare-id reader it replaces.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValidationError(f"cannot read the watch list at {path}: {error}") from error

    directives: list[Directive] = []
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(COMMENT):
            continue
        directives.append(_directive(stripped, path=path, number=number))
    return directives


def _directive(line: str, *, path: Path, number: int) -> Directive:
    """One non-empty line, as a directive — or a refusal naming the line.

    `maxsplit=1` is the whole of the format's syntax: the first run of
    whitespace ends the directive and everything after it is the target.
    """
    parts = line.split(maxsplit=1)
    name = parts[0]
    rest = parts[1] if len(parts) > 1 else ""

    kind_and_follow_up = DIRECTIVES.get(name.lower())
    if kind_and_follow_up is None:
        known = ", ".join(DIRECTIVES)
        raise ValidationError(
            f"{path} line {number}: unknown directive {name!r} — known directives are {known}"
        )
    target = rest.strip()
    if not target:
        raise ValidationError(f"{path} line {number}: {name!r} names nothing to collect")
    kind, follow_up = kind_and_follow_up
    return Directive(kind=kind, target=target, follow_up=follow_up, line=number)
