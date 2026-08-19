"""Collecting one kind of data, and not collecting it twice.

Dispatch goes through the registry rather than a method per kind, and the
cache check lives here rather than in each caller — the worker and the CLI go
through this one method, so there is no path that collects without consulting
what is already known.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel

from .database import Database
from .egress.transport import DirectEgress, Egress
from .errors import NotFoundError, TubedepthError
from .fingerprints import fingerprint
from .identifiers import normalize_target
from .payload_store import PayloadStore, StoredPayload
from .repositories import ArtifactRepository
from .schemas import Degradation, VideoBundle
from .sources import SourceRegistry, default_registry
from .sources.ytdlp_runtime import LibraryYtdlpRuntime, YtdlpRuntime

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Collected:
    kind: str
    target: str
    payload: StoredPayload
    # The parsed model, when this collection actually parsed one. None for a
    # cache hit: nothing was parsed, and a caller that fans out from a listing
    # must not fan out again from bytes it did not re-read.
    result: BaseModel | None = None
    # Whether this cost a request. The caller usually does not care; the
    # operator counting what a sweep actually spent very much does.
    from_cache: bool = False


class CollectionService:
    def __init__(
        self,
        *,
        payloads: PayloadStore,
        database: Database | None = None,
        runtime: YtdlpRuntime | None = None,
        egress: Egress | None = None,
        registry: SourceRegistry | None = None,
    ) -> None:
        self._payloads = payloads
        self._database = database
        self._runtime = runtime or LibraryYtdlpRuntime()
        self._egress = egress or DirectEgress()
        # Injected rather than imported, so a test can hand over a registry
        # holding one fake source and exercise the whole pipeline offline.
        self._registry = registry or default_registry()

    def kinds(self) -> list[str]:
        return self._registry.kinds()

    def collect(self, kind: str, target: str, *, refresh: bool = False) -> Collected:
        source = self._registry.get(kind)
        normalized = normalize_target(source.target_type, target)
        question = fingerprint(kind=kind, target=normalized, schema_version=source.schema_version)

        if not refresh:
            cached = self._cached(question, kind, normalized)
            if cached is not None:
                return cached

        parts = getattr(source, "parts", None)
        if parts is not None:
            result = self._assemble(normalized, parts)
        else:
            result = source.collect(normalized, self._egress, self._runtime)
        stored = self._payloads.put(kind, result.model_dump_json(indent=1).encode())
        self._record(question, kind, normalized, stored, source.default_freshness)
        return Collected(
            kind=kind, target=normalized, payload=stored, result=result, from_cache=False
        )

    def _assemble(self, target: str, parts: Sequence[str]) -> VideoBundle:
        """Collect each part through this same service, keeping what arrives.

        Through `self.collect` rather than the sources directly, so every part
        consults the cache and records its own artifact. A bundle asked for
        seconds after a metadata collect must not fetch the metadata again —
        on the one budget that actually caps this system, that is the expensive
        kind of convenience.

        A part that fails becomes a degradation instead of failing the bundle,
        which is the whole reason the composite exists. All parts failing is
        still a failure: an empty success would make "collected" and "collected
        nothing" the same answer.
        """
        collected: dict[str, object] = {}
        degradations: list[Degradation] = []
        for part in parts:
            try:
                result = self.collect(part, target)
            except TubedepthError as error:
                degradations.append(
                    Degradation(source=part, code=type(error).__name__, detail=str(error))
                )
                logger.info("bundle for %s lost %s: %s", target, part, error)
                continue
            collected[part] = result.result.model_dump() if result.result else None

        if not collected:
            raise NotFoundError(
                f"nothing could be collected for: {target} "
                f"({', '.join(f'{d.source} {d.code}' for d in degradations)})"
            )
        return VideoBundle(video_id=target, parts=collected, degradations=degradations)

    def cached(self, kind: str, target: str) -> Collected | None:
        """A fresh answer if one is held, without collecting. Never fetches."""
        source = self._registry.get(kind)
        question = fingerprint(kind=kind, target=target, schema_version=source.schema_version)
        return self._cached(question, kind, target)

    # -- the cache -------------------------------------------------------

    def _cached(self, question: str, kind: str, target: str) -> Collected | None:
        """A still-good answer, if there is one and the bytes are still there.

        The payload file is checked as well as the row: retention deletes
        files, and an index entry pointing at a missing file would serve a
        FileNotFoundError instead of a cache miss.
        """
        if self._database is None:
            return None
        with self._database.session() as session:
            artifact = ArtifactRepository(session).fresh(question)
            if artifact is None:
                return None
            found = (artifact.digest, artifact.byte_count)
        digest, byte_count = found
        path = self._payloads.path_for(kind, digest)
        if path is None:
            return None
        # Parsed back rather than returned as bytes. A listing served from
        # cache still has to produce the videos it holds, and a cache that
        # cannot reproduce the parsed value makes every consumer refetch —
        # which turns a repeat sweep from cheap into a silent no-op.
        model = self._registry.get(kind).payload_model
        result = model.model_validate_json(self._payloads.read(digest))
        return Collected(
            kind=kind,
            target=target,
            payload=StoredPayload(digest=digest, path=path, byte_count=byte_count),
            result=result,
            from_cache=True,
        )

    def _record(
        self,
        question: str,
        kind: str,
        target: str,
        stored: StoredPayload,
        freshness: object,
    ) -> None:
        if self._database is None:
            return
        with self._database.session() as session:
            ArtifactRepository(session).record(
                kind=kind,
                target=target,
                fingerprint=question,
                digest=stored.digest,
                byte_count=stored.byte_count,
                freshness=freshness,  # type: ignore[arg-type]
            )
