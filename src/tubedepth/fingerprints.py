"""What makes two collections the same question.

The cache is only as trustworthy as this. Two spellings of one request that
fingerprint differently produce two artifacts and halve the hit rate; two
genuinely different requests that fingerprint alike serve one the other's
answer, which is worse because nothing looks wrong.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def fingerprint(
    *,
    kind: str,
    target: str,
    schema_version: str,
    parameters: Mapping[str, Any] | None = None,
) -> str:
    """A stable identifier for one question.

    `schema_version` is part of it deliberately. Without it, adding a field to
    a normalized model leaves every cached artifact looking fresh while missing
    the field, and the only symptom is data that is quietly a version behind.
    Changing a normalizer therefore invalidates its own cache, with nobody
    having to remember to.
    """
    canonical = json.dumps(
        {
            "kind": kind,
            "target": target,
            "schema_version": schema_version,
            # sort_keys so two callers writing the same options in a different
            # order are recognised as asking the same thing.
            "parameters": dict(parameters or {}),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
