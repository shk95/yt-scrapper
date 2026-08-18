"""Collecting one kind of data, and not collecting it twice.

Dispatch goes through the registry rather than a method per kind, and the
cache check lives here rather than in each caller — the worker and the CLI go
through this one method, so there is no path that collects without consulting
what is already known.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from .database import Database
from .egress.transport import DirectEgress, Egress
from .fingerprints import fingerprint
from .identifiers import normalize_target
from .payload_store import PayloadStore, StoredPayload
from .repositories import ArtifactRepository
from .sources import SourceRegistry, default_registry
from .sources.ytdlp_runtime import LibraryYtdlpRuntime, YtdlpRuntime


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

        result = source.collect(normalized, self._egress, self._runtime)
        stored = self._payloads.put(kind, result.model_dump_json(indent=1).encode())
        self._record(question, kind, normalized, stored, source.default_freshness)
        return Collected(
            kind=kind, target=normalized, payload=stored, result=result, from_cache=False
        )

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
