"""Where collected payloads actually live.

Rows hold the index; the payload itself is a gzipped file on disk. A comment
harvest runs to tens of megabytes, and a multi-megabyte TEXT column makes every
careless SELECT on that table drag it through the ORM — in the same file the
job queue depends on staying fast.
"""

from __future__ import annotations

import json
from pathlib import Path

from tubedepth.payload_store import PayloadStore


def test_a_stored_payload_reads_back_byte_for_byte(tmp_path: Path) -> None:
    store = PayloadStore(tmp_path)
    payload = {"video_id": "dQw4w9WgXcQ", "tags": ["rick astley"]}

    stored = store.put("video.metadata", json.dumps(payload).encode())

    assert json.loads(store.read(stored.digest)) == payload


def test_storing_the_same_payload_twice_reuses_one_file(tmp_path: Path) -> None:
    # Content addressing earns its keep on refetch: a video whose metadata has
    # not moved since the last collection costs no new bytes at all.
    store = PayloadStore(tmp_path)
    payload = b'{"video_id": "dQw4w9WgXcQ"}'

    first = store.put("video.metadata", payload)
    second = store.put("video.metadata", payload)

    assert first.digest == second.digest
    assert len(list(tmp_path.rglob("*.json.gz"))) == 1
