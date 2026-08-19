"""Minting, verifying and revoking API keys."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select

from ..database import Database
from ..errors import RateLimitedError, UnauthenticatedError
from ..models import ApiKey, utcnow

PREFIX = "ytd"
PREFIX_LENGTH = 8
# Hex rather than url-safe base64, because the separator has to not appear in
# the alphabet: token_urlsafe emits "_" and "-", so a key could split into more
# than three parts and never verify. Sixteen hex characters is 64 bits for the
# lookup prefix and 48 hex is 192 bits of secret, which is plenty.
SECRET_BYTES = 24


@dataclass(frozen=True, slots=True)
class MintedKey:
    identifier: str
    label: str
    # Shown once. Nothing keeps a copy, which is the property that makes
    # hashing the stored form worth anything.
    secret: str


@dataclass(frozen=True, slots=True)
class VerifiedKey:
    identifier: str
    label: str


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class ApiKeyService:
    def __init__(self, database: Database, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._database = database
        self._clock = clock
        # In-process, which is honest for one API process. Two would each get
        # their own allowance; the README says so rather than implying
        # otherwise. Moving it to a table is the upgrade, and it is not built.
        self._recent: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def mint(self, *, label: str, requests_per_minute: int = 60) -> MintedKey:
        prefix = secrets.token_hex(PREFIX_LENGTH // 2)
        secret = f"{PREFIX}_{prefix}_{secrets.token_hex(SECRET_BYTES)}"
        with self._database.session() as session:
            key = ApiKey(
                label=label,
                key_prefix=prefix,
                key_hash=_hash(secret),
                requests_per_minute=requests_per_minute,
            )
            session.add(key)
            session.flush()
            identifier = key.identifier
        return MintedKey(identifier=identifier, label=label, secret=secret)

    def revoke(self, identifier: str) -> None:
        with self._database.session() as session:
            key = session.get(ApiKey, identifier)
            if key is not None:
                key.revoked_at = utcnow()

    def verify(self, presented: str) -> VerifiedKey:
        parts = presented.split("_")
        if len(parts) != 3 or parts[0] != PREFIX:
            # Refused on shape, before any query. A malformed key is not worth
            # a database round trip, and the response is the same either way.
            raise UnauthenticatedError("api key missing or not recognised")

        with self._database.session() as session:
            candidates = session.scalars(select(ApiKey).where(ApiKey.key_prefix == parts[1])).all()
            digest = _hash(presented)
            matched = next(
                (
                    key
                    for key in candidates
                    if hmac.compare_digest(key.key_hash, digest) and key.revoked_at is None
                ),
                None,
            )
            if matched is None:
                # The same message for unknown, malformed and revoked, so this
                # endpoint is not an oracle for which of the three it was.
                raise UnauthenticatedError("api key missing or not recognised")
            identifier, label, allowance = (
                matched.identifier,
                matched.label,
                matched.requests_per_minute,
            )
            matched.last_used_at = utcnow()

        self._charge(identifier, allowance)
        return VerifiedKey(identifier=identifier, label=label)

    def _charge(self, identifier: str, allowance: int) -> None:
        now = self._clock()
        with self._lock:
            recent = [stamp for stamp in self._recent.get(identifier, []) if now - stamp < 60.0]
            if len(recent) >= allowance:
                raise RateLimitedError(
                    f"api key is over its allowance of {allowance} requests per minute"
                )
            recent.append(now)
            self._recent[identifier] = recent
