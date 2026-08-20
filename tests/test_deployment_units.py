"""The systemd units, checked for the mistakes that only appear on a reboot.

A unit file is configuration nobody runs until the machine restarts, which is
the worst moment to discover it names a command that does not exist. These are
cheap assertions about the things that have actually gone wrong with units:
wrong command, missing lock pinning, a data directory the sandbox forbids
writing to, and a stop signal that abandons work.
"""

from __future__ import annotations

import configparser
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

UNITS = Path(__file__).parent.parent / "deploy"


def help_for(subcommand: str) -> str:
    """`tubedepth <subcommand> --help`, rendered wide, with a bare environment.

    `env=` replaces the whole environment rather than adding to it, and that is
    the load-bearing part: nearly every option in this CLI carries an `envvar=`
    default, and typer prints the effective value into the help text. A
    `TUBEDEPTH_*` variable sitting in the shell that ran pytest would otherwise
    be able to change what these tests read. `COLUMNS=200` stops rich from
    wrapping an option name across two lines, which would make it unfindable
    by substring.

    Shared with `tests/test_compose.py`, which asks the same question of
    `deploy/docker-compose.yml`'s `command:` lines. It was inline in the option
    test below until there was a second caller.
    """
    return subprocess.run(
        [sys.executable, "-m", "tubedepth.cli", subcommand, "--help"],
        capture_output=True,
        text=True,
        env={"COLUMNS": "200", "PATH": "/usr/bin:/bin"},
    ).stdout


def arguments_of(command: str | Sequence[str]) -> list[str]:
    """The words `tubedepth` itself is given, out of either shape a command takes.

    A unit's `ExecStart` is one string naming the launcher first
    (`/usr/bin/env uv run --frozen tubedepth work --poll 5`), so what the CLI
    receives is everything after the program name. A compose `command:` is
    already relative to the image's `ENTRYPOINT ["tubedepth"]` and may be a
    YAML list rather than a string, so all of it is the CLI's.

    Matched on the whole word rather than by splitting on the literal
    `"tubedepth "`, so a path that merely contains the name —
    `/etc/tubedepth/watchlist.txt`, which the compose `watch` service passes —
    is not mistaken for the program.
    """
    words = command.split() if isinstance(command, str) else [str(word) for word in command]
    if "tubedepth" in words:
        words = words[words.index("tubedepth") + 1 :]
    return words


def assert_every_option_exists(command: str | Sequence[str], *, source: str) -> None:
    """Fail if `command` passes an option the subcommand it names does not have.

    The classic deployment failure: a command that no longer takes an option it
    is given, discovered on a reboot — or on a `docker compose up` — rather
    than on the commit that removed it. Checked against the CLI's own help so
    it stays true as options change, rather than against a list copied here
    that would rot beside them.

    `--frozen` is `uv`'s option, not this CLI's. It sits before the program
    name and is therefore already gone by here, but it is skipped explicitly as
    well: that exclusion is what the check relied on before, and a launcher
    that put its own flags after the program name would need it again.
    """
    words = arguments_of(command)
    subcommand = words[0]
    options = [word for word in words if word.startswith("--") and word != "--frozen"]

    help_text = help_for(subcommand)
    for option in options:
        assert option in help_text, (
            f"{source} passes {option}, which `tubedepth {subcommand}` lacks"
        )


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

    The check itself is `assert_every_option_exists` above, shared with
    `tests/test_compose.py` — a compose `command:` is the same mistake waiting
    in a different file.
    """
    assert_every_option_exists(unit(name)["Service"]["ExecStart"], source=name)


def test_the_connection_budget_agrees_everywhere_it_is_declared() -> None:
    """`service-db.json`, `deploy/postgres-bootstrap.sql`'s `CONNECTION
    LIMIT` and session-default `ALTER ROLE ... SET` statements,
    `deploy/tubedepth-worker.service`'s `TUBEDEPTH_CONCURRENCY`,
    `deploy/docker-compose.yml`'s `--concurrency`, and the pool-sizing comment
    in `database.py` all have to agree, and nothing enforced that before this
    test — the previous survivor was `database.py` still asserting numbers a
    budget-raise had already changed everywhere else.

    The compose file is the newest site and was added here rather than to
    `tests/test_compose.py` deliberately: a budget that is cross-checked in two
    files is a budget with two partial answers, and the next person to raise it
    would find one of them. Everything that declares the number is compared in
    one place.

    `service-db.json` is actual JSON, so it is loaded with `json.loads`. The
    other four files are still parsed with regex, not a SQL/systemd/YAML
    parser, to avoid a dependency this repository does not declare directly. A
    future change to any one of them fails here rather than being caught by a
    human rereading six files.
    """
    import json
    import re

    def find(pattern: str, text: str) -> int:
        match = re.search(pattern, text, re.MULTILINE)
        assert match, f"pattern not found: {pattern!r}"
        return int(match[1])

    root = Path(__file__).parent.parent
    deploy = root / "deploy"
    database_py = root / "src" / "tubedepth" / "database.py"

    manifest = json.loads((root / "service-db.json").read_text())
    budget = manifest["connection_budget"]
    manifest_budget = budget["total"]

    bootstrap_text = (deploy / "postgres-bootstrap.sql").read_text()
    bootstrap_limit = find(r"ALTER ROLE tubedepth_runtime CONNECTION LIMIT (\d+);", bootstrap_text)

    worker_text = (deploy / "tubedepth-worker.service").read_text()
    concurrency = find(r"^Environment=TUBEDEPTH_CONCURRENCY=(\d+)", worker_text)

    # The compose worker passes the same number as a flag rather than an
    # environment variable, so that `api` and `worker` can go on sharing one
    # env anchor — see deploy/docker-compose.yml. Anchored to the `command:`
    # line so the prose around it cannot satisfy this.
    compose_text = (deploy / "docker-compose.yml").read_text()
    compose_concurrency = find(r"^\s*command: work .*--concurrency (\d+)", compose_text)

    database_text = database_py.read_text()
    comment_budget = find(r"declares (\d+) as the ceiling", database_text)
    comment_concurrency = find(r"deployed\n# default is (\d+), the AIMD controller", database_text)

    assert manifest_budget == bootstrap_limit == comment_budget, (
        "the connection budget disagrees between "
        f"manifest ({manifest_budget}), bootstrap.sql ({bootstrap_limit}), "
        f"and database.py's comment ({comment_budget})"
    )
    assert concurrency == comment_concurrency, (
        "TUBEDEPTH_CONCURRENCY disagrees between the worker unit "
        f"({concurrency}) and database.py's comment ({comment_concurrency})"
    )
    assert concurrency == compose_concurrency, (
        "worker concurrency disagrees between the worker unit "
        f"({concurrency}) and docker-compose.yml's `--concurrency` "
        f"({compose_concurrency}); both draw on the same 32-connection budget"
    )
    assert (
        concurrency == budget["worker"]["write_pool_size"] == budget["worker"]["write_max_overflow"]
    ), (
        "manifest's connection_budget.worker.write_pool_size/write_max_overflow "
        f"must equal TUBEDEPTH_CONCURRENCY ({concurrency}); got {budget['worker']}"
    )

    # The manifest's own breakdown must add up to the total it declares — the
    # whole point of adopting trend-radar's per-role shape (#1) was to make
    # this checkable instead of merely asserted in a comment.
    api, worker = budget["api"], budget["worker"]
    api_total = (
        api["write_pool_size"]
        + api["write_max_overflow"]
        + api["read_pool_size"]
        + api["read_max_overflow"]
    )
    worker_total = (
        worker["write_pool_size"]
        + worker["write_max_overflow"]
        + worker["read_pool_size"]
        + worker["read_max_overflow"]
    )
    breakdown_total = (
        api_total
        + worker_total
        + budget["workers_and_schedulers"]
        + budget["migration"]
        + budget["rolling_deploy_overlap"]
        + budget["service_spare"]
    )
    assert breakdown_total == manifest_budget, (
        f"connection_budget's breakdown sums to {breakdown_total}, "
        f"not the declared total {manifest_budget}"
    )

    # docs/status.md's formula: total = 2C + 13, must fit inside the budget
    # with the manifest's claimed margin.
    total = 2 * concurrency + 13
    assert total <= manifest_budget, (
        f"2*{concurrency}+13={total} exceeds the declared budget {manifest_budget}"
    )

    # session_defaults must agree with the ALTER ROLE ... SET statements
    # deploy/postgres-bootstrap.sql actually runs for tubedepth_runtime —
    # otherwise these are two sources of truth for the same timeouts with
    # nothing checking they agree.
    session_defaults = manifest["session_defaults"]

    def find_setting(role: str, setting: str) -> str:
        match = re.search(
            rf"ALTER ROLE {role}\s+IN DATABASE :database SET {setting} = '([^']+)';",
            bootstrap_text,
        )
        assert match, f"{setting} not found for {role} in postgres-bootstrap.sql"
        return match[1]

    assert (
        find_setting("tubedepth_runtime", "statement_timeout")
        == session_defaults["statement_timeout"]
    )
    assert find_setting("tubedepth_runtime", "lock_timeout") == session_defaults["lock_timeout"]
    assert (
        find_setting("tubedepth_runtime", "idle_in_transaction_session_timeout")
        == session_defaults["idle_in_transaction_session_timeout"]
    )
    assert (
        find_setting("tubedepth_runtime", "transaction_timeout")
        == session_defaults["transaction_timeout"]
    )
    assert find_setting("tubedepth_runtime", "TimeZone") == session_defaults["timezone"]
