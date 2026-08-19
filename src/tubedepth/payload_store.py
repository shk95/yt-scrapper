"""Content-addressed, gzipped payloads on disk."""

from __future__ import annotations

import gzip
import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class StoredPayload:
    digest: str
    path: Path
    byte_count: int


class PayloadStore:
    """Addressed by the digest of the payload, so an unchanged refetch costs nothing.

    Two levels of directory fan-out because a flat directory of a hundred
    thousand files is slow to list on the filesystem this runs on.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path_for(self, kind: str, digest: str) -> Path:
        return self._root / kind / digest[:2] / f"{digest}.json.gz"

    def put(self, kind: str, payload: bytes) -> StoredPayload:
        digest = hashlib.sha256(payload).hexdigest()
        path = self._path_for(kind, digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(payload))
        return StoredPayload(digest=digest, path=path, byte_count=len(payload))

    def path_for(self, kind: str, digest: str) -> Path | None:
        """Where a payload lives, or None if the bytes are gone.

        Retention deletes files, so an index entry can outlive its payload. A
        cache that does not check would serve a FileNotFoundError rather than
        a miss.
        """
        path = self._path_for(kind, digest)
        return path if path.exists() else None

    def delete(self, kind: str, digest: str) -> bool:
        """Remove a payload. Returns whether there was one to remove."""
        path = self._path_for(kind, digest)
        if not path.exists():
            return False
        path.unlink()
        return True

    def stored_files(self) -> Iterator[tuple[str, str, Path]]:
        """Every payload on disk as (kind, digest, path).

        The store is the only thing that knows its own layout, so walking it
        belongs here rather than in a caller reconstructing the fan-out.
        """
        if not self._root.exists():
            return
        for path in self._root.glob("*/*/*.json.gz"):
            yield path.parent.parent.name, path.name.removesuffix(".json.gz"), path

    def read(self, digest: str) -> bytes:
        matches = list(self._root.glob(f"*/{digest[:2]}/{digest}.json.gz"))
        if not matches:
            raise FileNotFoundError(f"payload not stored: {digest}")
        return gzip.decompress(matches[0].read_bytes())
