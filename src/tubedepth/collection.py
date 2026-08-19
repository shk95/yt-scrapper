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
from pydantic import ValidationError as PydanticValidationError

from .database import Database
from .egress.transport import DirectEgress, Egress
from .errors import NotFoundError, TubedepthError
from .fingerprints import fingerprint
from .identifiers import normalize_target
from .payload_store import PayloadStore, StoredPayload
from .repositories import ArtifactRepository
from .schemas import Degradation, VideoBundle
from .sources import SourceRegistry, default_registry
from .sources.registry import DataSource, cache_parameters_of
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

    def _question(self, source: DataSource, target: str) -> str:
        """The cache key for one question, computed in exactly one place.

        `collect` and `cached` are the same lookup asked from two processes —
        the worker and the API — and a key built twice is a key that can be
        built two ways. When they disagree the API stops matching anything the
        worker writes, *and* keeps matching every row from before the change
        and serving it as a 200: both failures the fingerprints docstring
        names, at once.

        Note `default_registry()` is @cache'd, so what a source declares is
        frozen per process, and `tubedepth serve` and `tubedepth work` are
        separate processes. Three of these caps are environment variables now
        (`TUBEDEPTH_LISTING_LIMIT`, `_COMMENT_LIMIT`, `_TRENDING_LIMIT`), so two
        units with different environments compute different keys and the API
        answers for a question the worker did not collect. That is why
        `GET /v1/sources` reports the values actually in effect and both unit
        files carry the variables together — see `default_registry`.
        """
        return fingerprint(
            kind=source.kind,
            target=target,
            schema_version=source.schema_version,
            parameters=cache_parameters_of(source),
        )

    def collect(self, kind: str, target: str, *, refresh: bool = False) -> Collected:
        source = self._registry.get(kind)
        normalized = normalize_target(source.target_type, target)
        question = self._question(source, normalized)

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
        self._record(
            question, kind, normalized, stored, source.default_freshness, source.schema_version
        )
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
        return self._cached(self._question(source, target), kind, target)

    # -- the cache -------------------------------------------------------

    def _cached(self, question: str, kind: str, target: str) -> Collected | None:
        """A still-good answer, if there is one and the bytes are still there.

        The payload file is checked as well as the row: retention deletes
        files, and an index entry pointing at a missing file would serve a
        FileNotFoundError instead of a cache miss.
        """
        if self._database is None:
            return None
        # A reader, and it has to say so. `decisions/002` is about exactly this:
        # every session that is not `readonly=True` opens BEGIN IMMEDIATE and
        # takes SQLite's write lock, so a pure lookup was serialising against
        # the worker for no reason — and once `POST /v1/jobs/batch` called this
        # from inside its own write transaction, the second target deadlocked
        # the request against a lock it was already holding. That decision file
        # records the same shape happening once before, in the schema repair.
        with self._database.session(readonly=True) as session:
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
        source = self._registry.get(kind)
        try:
            result = source.payload_model.model_validate_json(self._payloads.read(digest))
        except PydanticValidationError:
            # A model changed and its `schema_version` did not, so the cache
            # holds bytes the current shape rejects. This is the only place in
            # the codebase that parses a stored payload with a model, and
            # letting it raise reaches FastAPI's default handler: `POST
            # /v1/jobs` answers 500 for every target that has a cached
            # artifact, which is most of them.
            #
            # A stored payload the current model cannot read is not an answer
            # to the current question, so it is a miss. The cost becomes
            # requests until someone bumps, rather than an API that is down —
            # and the warning plus the payload-shape check in CI are what make
            # the cause loud instead of the symptom.
            logger.warning(
                "stored payload for %s %s (%s) does not fit schema version %s; "
                "treating it as a cache miss — a bump was probably missed",
                kind,
                target,
                digest[:12],
                source.schema_version,
            )
            return None
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
        schema_version: str,
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
                schema_version=schema_version,
            )
