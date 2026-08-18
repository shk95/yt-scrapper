"""Collecting one kind of data and putting it where it can be read again.

Dispatch goes through the registry rather than a method per kind. That is the
difference between adding a source costing one module and it costing an edit
here, an edit in the CLI, and an edit in the worker — and it is what the
job queue will read when a claimed job names its kind.
"""

from __future__ import annotations

from dataclasses import dataclass

from .egress.transport import DirectEgress, Egress
from .identifiers import normalize_target
from .payload_store import PayloadStore, StoredPayload
from .sources import SourceRegistry, default_registry
from .sources.ytdlp_runtime import LibraryYtdlpRuntime, YtdlpRuntime


@dataclass(frozen=True, slots=True)
class Collected:
    kind: str
    target: str
    payload: StoredPayload


class CollectionService:
    def __init__(
        self,
        *,
        payloads: PayloadStore,
        runtime: YtdlpRuntime | None = None,
        egress: Egress | None = None,
        registry: SourceRegistry | None = None,
    ) -> None:
        self._payloads = payloads
        self._runtime = runtime or LibraryYtdlpRuntime()
        self._egress = egress or DirectEgress()
        # Injected rather than imported, so a test can hand over a registry
        # holding one fake source and exercise the whole pipeline offline.
        self._registry = registry or default_registry()

    def kinds(self) -> list[str]:
        return self._registry.kinds()

    def collect(self, kind: str, target: str) -> Collected:
        source = self._registry.get(kind)
        normalized = normalize_target(source.target_type, target)
        result = source.collect(normalized, self._egress, self._runtime)
        stored = self._payloads.put(kind, result.model_dump_json(indent=1).encode())
        return Collected(kind=kind, target=normalized, payload=stored)
