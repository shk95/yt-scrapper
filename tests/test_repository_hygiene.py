"""Checks about the repository itself, not about its behaviour.

These exist because each guards an invariant that is invisible in review and
expensive once it lands: a committed credential, or a transport constructed
somewhere the egress pool cannot govern.
"""

from __future__ import annotations

import gzip
import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = REPOSITORY_ROOT / "src" / "tubedepth"
# The sanctioned construction sites. The invariant is not "only the egress
# package may build a transport" — it is "each transport has exactly one place
# it is built", so the proxy an egress hands out cannot be forgotten by one
# caller and applied by another. ytdlp_runtime.py exists to be that place for
# yt-dlp, and its own docstring says so.
TRANSPORT_CONSTRUCTION_SITES = (
    SOURCE_ROOT / "egress",
    SOURCE_ROOT / "sources" / "ytdlp_runtime.py",
)

# yt-dlp emits ~37 signed googlevideo URLs per video. They expire within hours,
# they are dead weight in a fixture, and gitleaks reads them as credentials.
SIGNED_MEDIA_URL = re.compile(r"https?://[\w.-]*googlevideo\.com/")
WIREGUARD_KEY = re.compile(r"(?im)^\s*(Private|Preshared)Key\s*=\s*[A-Za-z0-9+/]{42,43}=")
TRANSPORT_CONSTRUCTION = re.compile(r"httpx\.AsyncClient\(|YoutubeDL\(")


IGNORED_DIRECTORIES = {".git", ".venv", "var", ".pytest_cache", ".ruff_cache", "__pycache__"}


def _committed_files(suffixes: tuple[str, ...]) -> list[Path]:
    return [
        path
        for path in REPOSITORY_ROOT.rglob("*")
        if path.suffix in suffixes and path.is_file() and IGNORED_DIRECTORIES.isdisjoint(path.parts)
    ]


def _readable_text(path: Path) -> str:
    """Read a file as text, decompressing it first when it is gzipped.

    Fixtures are stored gzipped, and reading one as raw bytes finds nothing —
    so a guard that skips decompression passes happily while a signed URL sits
    inside. That is worse than having no guard, because it is believed.
    """
    if path.suffix == ".gz":
        try:
            return gzip.decompress(path.read_bytes()).decode("utf-8", errors="ignore")
        except OSError:
            return ""
    return path.read_text(errors="ignore")


def test_no_committed_fixture_contains_a_signed_media_url() -> None:
    offenders = [
        path
        for path in _committed_files((".json", ".gz", ".txt"))
        if SIGNED_MEDIA_URL.search(_readable_text(path))
    ]
    assert offenders == [], (
        "signed googlevideo URLs must be stripped when a fixture is captured: "
        f"{[str(p.relative_to(REPOSITORY_ROOT)) for p in offenders]}"
    )


def test_no_committed_file_contains_wireguard_key_material() -> None:
    offenders = [
        path
        for path in _committed_files((".conf", ".json", ".toml", ".txt", ".md"))
        if WIREGUARD_KEY.search(_readable_text(path))
    ]
    assert offenders == [], (
        "WireGuard configs hold private keys and live outside this repository: "
        f"{[str(p.relative_to(REPOSITORY_ROOT)) for p in offenders]}"
    )


def test_no_module_outside_a_sanctioned_site_constructs_a_transport_directly() -> None:
    """Every request must leave through a transport the egress pool governs.

    If httpx and yt-dlp are built in more than one place they can disagree
    about which proxy they are using, and that disagreement is invisible: it
    leaks the origin address on whichever transport was forgotten. The rule is
    therefore one construction site per transport, not zero outside egress/.
    """
    sanctioned = set(TRANSPORT_CONSTRUCTION_SITES)
    offenders = [
        path
        for path in SOURCE_ROOT.rglob("*.py")
        if path not in sanctioned
        and sanctioned.isdisjoint(path.parents)
        and TRANSPORT_CONSTRUCTION.search(path.read_text())
    ]
    assert offenders == [], (
        "construct transports only at a sanctioned site "
        f"({sorted(str(p.relative_to(REPOSITORY_ROOT)) for p in sanctioned)}): "
        f"{[str(p.relative_to(REPOSITORY_ROOT)) for p in offenders]}"
    )
