"""Who may ask, and how often.

The key is stored hashed. A stolen database should not hand over working
credentials, and the plaintext is shown once at creation because there is no
way to recover it afterwards — which is the property that makes the hash worth
having.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tubedepth.database import Database
from tubedepth.errors import RateLimitedError, UnauthenticatedError
from tubedepth.services.keys import ApiKeyService


def service(tmp_path: Path) -> tuple[ApiKeyService, Database]:
    database = Database(tmp_path / "tubedepth.db")
    database.create_schema()
    return ApiKeyService(database), database


def test_a_minted_key_verifies(tmp_path: Path) -> None:
    keys, _ = service(tmp_path)

    minted = keys.mint(label="laptop")

    assert keys.verify(minted.secret).label == "laptop"


def test_the_plaintext_key_is_not_stored(tmp_path: Path) -> None:
    # The whole point of hashing it. A database that leaks should not leak
    # working credentials with it.
    keys, database = service(tmp_path)
    minted = keys.mint(label="laptop")

    with database.session() as session:
        rows = session.execute(__import__("sqlalchemy").text("select * from api_keys")).all()
    assert minted.secret not in str(rows)


def test_an_unknown_key_is_refused(tmp_path: Path) -> None:
    keys, _ = service(tmp_path)

    with pytest.raises(UnauthenticatedError):
        keys.verify("ytd_nosuch_key")


def test_a_revoked_key_stops_working_immediately(tmp_path: Path) -> None:
    # Immediately, not at the next restart. That is why keys live in the
    # database rather than in a config file read at startup.
    keys, _ = service(tmp_path)
    minted = keys.mint(label="laptop")
    keys.revoke(minted.identifier)

    with pytest.raises(UnauthenticatedError):
        keys.verify(minted.secret)


def test_a_malformed_key_is_refused_without_touching_the_database(tmp_path: Path) -> None:
    keys, _ = service(tmp_path)

    with pytest.raises(UnauthenticatedError):
        keys.verify("not-even-the-right-shape")


def test_a_key_over_its_rate_limit_is_refused(tmp_path: Path) -> None:
    keys, _ = service(tmp_path)
    minted = keys.mint(label="noisy", requests_per_minute=3)

    for _ in range(3):
        keys.verify(minted.secret)

    with pytest.raises(RateLimitedError):
        keys.verify(minted.secret)


def test_one_key_hitting_its_limit_does_not_affect_another(tmp_path: Path) -> None:
    keys, _ = service(tmp_path)
    noisy = keys.mint(label="noisy", requests_per_minute=1)
    quiet = keys.mint(label="quiet", requests_per_minute=10)
    keys.verify(noisy.secret)

    with pytest.raises(RateLimitedError):
        keys.verify(noisy.secret)
    assert keys.verify(quiet.secret).label == "quiet"


def test_two_keys_are_not_the_same(tmp_path: Path) -> None:
    keys, _ = service(tmp_path)

    assert keys.mint(label="a").secret != keys.mint(label="b").secret


def test_keys_can_be_listed_before_anyone_decides_to_revoke_one(tmp_path: Path) -> None:
    """`last_used_at` was written on every request and readable from nowhere.

    So "is this key still in use" could not be answered before revoking it,
    which is the one question anyone asks first — and the secret is shown once,
    so there is no way to work it out afterwards either.
    """
    database = Database(tmp_path / "tubedepth.db")
    database.create_schema()
    service = ApiKeyService(database)
    minted = service.mint(label="ingest")
    service.verify(minted.secret)

    listed = service.listed()

    assert [entry.label for entry in listed] == ["ingest"]
    assert listed[0].identifier == minted.identifier
    assert listed[0].last_used_at is not None
    assert listed[0].revoked is False


def test_a_revoked_key_is_still_listed_and_says_so(tmp_path: Path) -> None:
    """Revocation is not deletion — the jobs it submitted still name it, and a
    row that vanished would make those unattributable."""
    database = Database(tmp_path / "tubedepth.db")
    database.create_schema()
    service = ApiKeyService(database)
    minted = service.mint(label="gone")
    service.revoke(minted.identifier)

    assert [entry.revoked for entry in service.listed()] == [True]
