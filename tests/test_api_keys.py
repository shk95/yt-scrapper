"""Who may ask, and how often.

The key is stored hashed. A stolen database should not hand over working
credentials, and the plaintext is shown once at creation because there is no
way to recover it afterwards — which is the property that makes the hash worth
having.
"""

from __future__ import annotations

import pytest

from tubedepth.database import Database
from tubedepth.errors import RateLimitedError, UnauthenticatedError
from tubedepth.services.keys import ApiKeyService


def service(database: Database) -> tuple[ApiKeyService, Database]:
    return ApiKeyService(database), database


def test_a_minted_key_verifies(database: Database) -> None:
    keys, _ = service(database)

    minted = keys.mint(label="laptop")

    assert keys.verify(minted.secret).label == "laptop"


def test_the_plaintext_key_is_not_stored(database: Database) -> None:
    # The whole point of hashing it. A database that leaks should not leak
    # working credentials with it.
    keys, database = service(database)
    minted = keys.mint(label="laptop")

    with database.session() as session:
        rows = session.execute(__import__("sqlalchemy").text("select * from api_keys")).all()
    assert minted.secret not in str(rows)


def test_an_unknown_key_is_refused(database: Database) -> None:
    keys, _ = service(database)

    with pytest.raises(UnauthenticatedError):
        keys.verify("ytd_nosuch_key")


def test_a_revoked_key_stops_working_immediately(database: Database) -> None:
    # Immediately, not at the next restart. That is why keys live in the
    # database rather than in a config file read at startup.
    keys, _ = service(database)
    minted = keys.mint(label="laptop")
    keys.revoke(minted.identifier)

    with pytest.raises(UnauthenticatedError):
        keys.verify(minted.secret)


def test_a_malformed_key_is_refused_without_touching_the_database(database: Database) -> None:
    keys, _ = service(database)

    with pytest.raises(UnauthenticatedError):
        keys.verify("not-even-the-right-shape")


def test_a_key_over_its_rate_limit_is_refused(database: Database) -> None:
    keys, _ = service(database)
    minted = keys.mint(label="noisy", requests_per_minute=3)

    for _ in range(3):
        keys.verify(minted.secret)

    with pytest.raises(RateLimitedError):
        keys.verify(minted.secret)


def test_one_key_hitting_its_limit_does_not_affect_another(database: Database) -> None:
    keys, _ = service(database)
    noisy = keys.mint(label="noisy", requests_per_minute=1)
    quiet = keys.mint(label="quiet", requests_per_minute=10)
    keys.verify(noisy.secret)

    with pytest.raises(RateLimitedError):
        keys.verify(noisy.secret)
    assert keys.verify(quiet.secret).label == "quiet"


def test_two_keys_are_not_the_same(database: Database) -> None:
    keys, _ = service(database)

    assert keys.mint(label="a").secret != keys.mint(label="b").secret


def test_keys_can_be_listed_before_anyone_decides_to_revoke_one(database: Database) -> None:
    """`last_used_at` was written on every request and readable from nowhere.

    So "is this key still in use" could not be answered before revoking it,
    which is the one question anyone asks first — and the secret is shown once,
    so there is no way to work it out afterwards either.
    """
    service = ApiKeyService(database)
    minted = service.mint(label="ingest")
    service.verify(minted.secret)

    listed = service.listed()

    assert [entry.label for entry in listed] == ["ingest"]
    assert listed[0].identifier == minted.identifier
    assert listed[0].last_used_at is not None
    assert listed[0].revoked is False


def test_a_revoked_key_is_still_listed_and_says_so(database: Database) -> None:
    """Revocation is not deletion — the jobs it submitted still name it, and a
    row that vanished would make those unattributable."""
    service = ApiKeyService(database)
    minted = service.mint(label="gone")
    service.revoke(minted.identifier)

    assert [entry.revoked for entry in service.listed()] == [True]
