"""Checks about the repository itself, not about its behaviour.

These exist because each guards an invariant that is invisible in review and
expensive once it lands: a committed credential, or a transport constructed
somewhere the egress pool cannot govern.
"""

from __future__ import annotations

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = REPOSITORY_ROOT / "src" / "tubedepth"
EGRESS_PACKAGE = SOURCE_ROOT / "egress"

# yt-dlp emits ~37 signed googlevideo URLs per video. They expire within hours,
# they are dead weight in a fixture, and gitleaks reads them as credentials.
SIGNED_MEDIA_URL = re.compile(r"https?://[\w.-]*googlevideo\.com/")
WIREGUARD_KEY = re.compile(r"(?im)^\s*(Private|Preshared)Key\s*=\s*[A-Za-z0-9+/]{42,43}=")
TRANSPORT_CONSTRUCTION = re.compile(r"httpx\.AsyncClient\(|YoutubeDL\(")


def _committed_files(suffixes: tuple[str, ...]) -> list[Path]:
    return [
        path
        for path in REPOSITORY_ROOT.rglob("*")
        if path.suffix in suffixes
        and path.is_file()
        and ".git" not in path.parts
        and ".venv" not in path.parts
        and "var" not in path.parts
    ]


def test_no_committed_fixture_contains_a_signed_media_url() -> None:
    offenders = [
        path
        for path in _committed_files((".json", ".gz", ".txt"))
        if SIGNED_MEDIA_URL.search(path.read_text(errors="ignore"))
    ]
    assert offenders == [], (
        "signed googlevideo URLs must be stripped when a fixture is captured: "
        f"{[str(p.relative_to(REPOSITORY_ROOT)) for p in offenders]}"
    )


def test_no_committed_file_contains_wireguard_key_material() -> None:
    offenders = [
        path
        for path in _committed_files((".conf", ".json", ".toml", ".txt", ".md"))
        if WIREGUARD_KEY.search(path.read_text(errors="ignore"))
    ]
    assert offenders == [], (
        "WireGuard configs hold private keys and live outside this repository: "
        f"{[str(p.relative_to(REPOSITORY_ROOT)) for p in offenders]}"
    )


def test_no_module_outside_the_egress_package_constructs_a_transport_directly() -> None:
    """Every request must leave through an egress the pool can govern.

    If httpx and yt-dlp are built anywhere other than one call site, they can
    disagree about which proxy they are using — and that disagreement is
    invisible: it leaks the origin address on whichever transport was forgotten.
    """
    offenders = [
        path
        for path in SOURCE_ROOT.rglob("*.py")
        if EGRESS_PACKAGE not in path.parents and TRANSPORT_CONSTRUCTION.search(path.read_text())
    ]
    assert offenders == [], (
        "construct transports only in src/tubedepth/egress/: "
        f"{[str(p.relative_to(REPOSITORY_ROOT)) for p in offenders]}"
    )
