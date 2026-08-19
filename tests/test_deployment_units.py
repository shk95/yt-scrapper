"""The systemd units, checked for the mistakes that only appear on a reboot.

A unit file is configuration nobody runs until the machine restarts, which is
the worst moment to discover it names a command that does not exist. These are
cheap assertions about the things that have actually gone wrong with units:
wrong command, missing lock pinning, a data directory the sandbox forbids
writing to, and a stop signal that abandons work.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

UNITS = Path(__file__).parent.parent / "deploy"

# Read from the directory rather than listed here. A copied list is one a new
# unit is added without, and a unit nobody checks is exactly the file these
# tests exist for — the same argument the option check below makes for reading
# the CLI's own help instead of a list that would rot beside it.
SERVICES = sorted(path.name for path in UNITS.glob("*.service"))
TIMERS = sorted(path.name for path in UNITS.glob("*.timer"))


def unit(name: str) -> configparser.ConfigParser:
    # No interpolation: systemd's `%h` specifiers are not configparser's `%`
    # syntax, and the default parser raises on the first one it meets.
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    # systemd allows repeated keys and bare directives; the default parser is
    # stricter than systemd is.
    parser.optionxform = str  # type: ignore[method-assign,assignment]
    parser.read(UNITS / name)
    return parser


@pytest.mark.parametrize("name", SERVICES)
def test_the_unit_runs_a_command_this_project_actually_has(name: str) -> None:
    from tubedepth.cli import application

    command = unit(name)["Service"]["ExecStart"]
    subcommand = command.split("tubedepth ")[1].split()[0]
    registered = {
        info.name or (info.callback.__name__ if info.callback else "")
        for info in application.registered_commands
    }

    assert subcommand in registered, f"{name} runs `tubedepth {subcommand}`, which does not exist"


@pytest.mark.parametrize("name", SERVICES)
def test_the_unit_can_find_the_command_it_runs(name: str) -> None:
    """A user unit does not inherit the shell's PATH, and `uv` is not on the one it gets.

    systemd hands a user service `/usr/bin:/bin` and a little more, while this
    host installs uv into a nix profile under $HOME — see AGENTS.md, where nix
    is the install route because there is no Docker and no passwordless sudo.
    So `/usr/bin/env uv` resolves to nothing and the unit dies with status 127.

    Found by enabling these for the first time. All three had that defect,
    which is to say none of them had ever run: the failure appears only on the
    machine, only at enable time, and every other check here reads the file.
    """
    body = (UNITS / name).read_text()
    if "/usr/bin/env uv " not in body:
        pytest.skip(f"{name} does not run uv")

    declared = [line for line in body.splitlines() if line.startswith("Environment=PATH=")]

    assert len(declared) == 1, f"{name} runs uv without a PATH of its own, so it cannot find it"


@pytest.mark.parametrize("name", SERVICES)
def test_the_unit_pins_the_lock_file(name: str) -> None:
    """Without --frozen a restart may resolve a newer dependency than the one
    committed, so the running version becomes whatever the day supplies."""
    assert "--frozen" in unit(name)["Service"]["ExecStart"]


@pytest.mark.parametrize("name", SERVICES)
def test_the_sandbox_still_lets_the_data_directory_be_written(name: str) -> None:
    """`ProtectSystem=strict` makes the whole filesystem read-only, so a unit
    that sets it without a matching ReadWritePaths starts and then fails on the
    first write — which looks like a database problem, not a unit problem.

    Read from the raw text rather than the parsed unit: systemd accumulates
    repeated `Environment=` directives and configparser keeps only the last, so
    the parsed view of a unit with two of them is not the unit.
    """
    body = (UNITS / name).read_text()
    directives = [
        line for line in body.splitlines() if line.startswith("Environment=TUBEDEPTH_DATA_DIR=")
    ]
    assert len(directives) == 1, "the data directory is set once or not at all"
    data = directives[0].split("=", 2)[2]

    service = unit(name)["Service"]
    assert service["ProtectSystem"] == "strict"
    assert data in service["ReadWritePaths"]


def test_the_worker_is_stopped_in_a_way_that_releases_its_lease() -> None:
    """SIGTERM abandons a running job to wait out its whole lease before
    another worker may take it. SIGINT lets the loop finish and release."""
    service = unit("tubedepth-worker.service")["Service"]

    assert service["KillSignal"] == "SIGINT"
    assert int(service["TimeoutStopSec"].rstrip("s")) >= 60


def test_the_api_is_not_exposed_beyond_loopback_by_the_unit() -> None:
    """The API's auth is a header, which is not a substitute for TLS. Binding
    publicly by default would make a private tool a public one on install."""
    assert "--host 127.0.0.1" in unit("tubedepth-api.service")["Service"]["ExecStart"]


def test_no_unit_carries_a_secret() -> None:
    """Unit files are world-readable where they live."""
    for name in SERVICES:
        body = (UNITS / name).read_text()
        assert "ytd_" not in body
        for line in body.splitlines():
            if line.startswith("Environment=") and "SECRET" in line.upper():
                raise AssertionError(f"{name} puts a secret in the unit: {line}")


@pytest.mark.parametrize("name", TIMERS)
def test_every_timer_has_the_service_it_starts(name: str) -> None:
    """systemd pairs `foo.timer` with `foo.service` by name and says nothing
    when the second one is absent: `systemctl --user enable` accepts the timer,
    the schedule fires, and every firing fails to find its unit. Shipping the
    pair is the only moment anyone would notice.
    """
    expected = name.removesuffix(".timer") + ".service"

    assert expected in SERVICES, f"{name} fires {expected}, which is not in deploy/"


@pytest.mark.parametrize("name", SERVICES)
def test_every_option_the_unit_passes_actually_exists(name: str) -> None:
    """The classic unit failure: a command that no longer takes an option it
    is given, discovered on a reboot rather than on the commit that removed it.

    Checked against the CLI's own help so it stays true as options change,
    rather than against a list copied here that would rot beside them.
    """
    import subprocess
    import sys

    command = unit(name)["Service"]["ExecStart"]
    subcommand = command.split("tubedepth ")[1].split()[0]
    options = [word for word in command.split() if word.startswith("--") and word != "--frozen"]

    help_text = subprocess.run(
        [sys.executable, "-m", "tubedepth.cli", subcommand, "--help"],
        capture_output=True,
        text=True,
        env={"COLUMNS": "200", "PATH": "/usr/bin:/bin"},
    ).stdout

    for option in options:
        assert option in help_text, f"{name} passes {option}, which `tubedepth {subcommand}` lacks"
