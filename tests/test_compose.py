"""`deploy/docker-compose.yml`, checked for the mistakes that only appear on `up`.

The same argument as `tests/test_deployment_units.py`: a compose file is
configuration nobody runs until a deployment, which is the worst moment to
discover it passes an option the CLI dropped, or that the API and the worker
disagree about a cap that is part of the cache key. Everything here is file
parsing plus one `--help` subprocess — no Docker, because `tests/conftest.py`
refuses outbound sockets and a test that needed a daemon would be a test that
is skipped everywhere it matters.

`yaml` is available without touching `pyproject.toml`: pyyaml is a transitive
dependency of `uvicorn[standard]`, which is a main dependency, so `uv run
pytest --frozen` has it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from test_deployment_units import assert_every_option_exists, help_for

DEPLOY = Path(__file__).parent.parent / "deploy"
COMPOSE = DEPLOY / "docker-compose.yml"

# The services that stay up, as opposed to `migrate` (a one-shot) and
# `postgres` (not this project's process at all).
LONG_RUNNING = ("api", "worker", "watch")


def compose() -> dict[str, Any]:
    """The parsed file.

    `yaml.safe_load` rather than `docker compose config`: this must be a pure
    parse, and it also keeps the anchor identity below meaningful — an alias
    resolves to the *same* Python object here, while compose's own renderer
    flattens it into two equal-looking blocks.
    """
    return yaml.safe_load(COMPOSE.read_text())


def services() -> dict[str, Any]:
    return compose()["services"]


def mount_parts(entry: str) -> list[str]:
    """Split `source:target[:mode]`, ignoring the colons inside `${...}`.

    `${TUBEDEPTH_WATCHLIST_FILE:-./watchlist.example.txt}` carries one, and a
    naive `split(":")` turns that mount into five meaningless fragments.
    """
    parts: list[str] = []
    current = ""
    depth = 0
    for character in entry:
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
        if character == ":" and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += character
    parts.append(current)
    return parts


def mounts(service: dict[str, Any]) -> dict[str, str]:
    """target -> source, for every volume a service declares."""
    found: dict[str, str] = {}
    for entry in service.get("volumes", []):
        parts = mount_parts(entry)
        found[parts[1]] = parts[0]
    return found


def test_the_api_and_the_worker_share_one_env_block_by_anchor() -> None:
    """Not "they happen to hold the same values" — the same object.

    `TUBEDEPTH_LISTING_LIMIT`, `TUBEDEPTH_COMMENT_LIMIT` and
    `TUBEDEPTH_TRENDING_LIMIT` are read once per process inside
    `@cache def default_registry()` and flow into `cache_parameters_of(source)`
    → `fingerprint(parameters=...)`. Two processes with different values
    compute different SHA-256 keys, so the API serves a question the worker
    never collected — silently, with data collected at the old cap.

    The identity check is the point. A test that only compared the resolved
    mappings would pass on two hand-copied blocks that agree today and stop
    agreeing on the first edit that touches one of them, which is exactly the
    failure this is written against. PyYAML resolves an alias to the same
    object, so `is` is true only when the file actually shares one block.
    """
    parsed = services()
    api = parsed["api"]["environment"]
    worker = parsed["worker"]["environment"]

    assert api == worker, "api and worker must run with the same environment"
    assert api is worker, (
        "api and worker have equal environments but not a shared one — use the "
        "YAML anchor, or the next edit to one of them is a silent cache-key split"
    )
    assert "*runtime-environment" in COMPOSE.read_text(), (
        "the shared block should be referenced by its alias, so the file reads as one block"
    )


def test_the_shared_env_block_carries_every_cap_that_is_part_of_the_cache_key() -> None:
    """A cap left out of the block is a cap that can differ per service again."""
    shared = services()["api"]["environment"]

    for variable in (
        "TUBEDEPTH_LISTING_LIMIT",
        "TUBEDEPTH_COMMENT_LIMIT",
        "TUBEDEPTH_TRENDING_LIMIT",
    ):
        assert variable in shared, f"{variable} decides a cache key; it belongs in the shared block"


@pytest.mark.parametrize("name", sorted(services()))
def test_every_option_a_service_passes_actually_exists(name: str) -> None:
    """The classic failure, in a compose file rather than a unit: an option the
    subcommand no longer takes, found on a deploy instead of on the commit that
    removed it.

    Checked against the CLI's own help, by the same helper the unit test uses.
    """
    service = services()[name]
    command = service.get("command")
    if command is None or "tubedepth" not in str(service.get("image", "")):
        pytest.skip(f"{name} does not run this project's CLI")

    assert_every_option_exists(command, source=f"docker-compose.yml's {name}")


def test_every_service_runs_a_subcommand_this_project_has() -> None:
    from tubedepth.cli import application

    registered = {
        info.name or (info.callback.__name__ if info.callback else "")
        for info in application.registered_commands
    }

    for name, service in services().items():
        command = service.get("command")
        if command is None or "tubedepth" not in str(service.get("image", "")):
            continue
        subcommand = (command.split() if isinstance(command, str) else list(command))[0]
        assert subcommand in registered, (
            f"{name} runs `tubedepth {subcommand}`, which does not exist"
        )


def test_no_secret_is_a_literal_in_the_file() -> None:
    """A compose file is committed; a database URL embeds a password.

    Everything that could carry one has to arrive by `${...}` interpolation
    from `deploy/.env` or the environment. Two checks, because they catch
    different mistakes: a URL pasted into a value, and a value that is only
    *partly* interpolated (`postgresql://user:${PASSWORD}@host`, which still
    commits the username and the host).
    """
    body = COMPOSE.read_text()
    interpolated = re.compile(r"^\$\{[^{}]+\}$")
    sensitive = re.compile(r"PASSWORD|SECRET|_KEY$|DATABASE_URL")

    assert "ytd_" not in body, "an API key literal is in the compose file"
    assert not re.search(r"://[^\s/${]+:[^\s/@]+@", body), (
        "a URL with an inline credential is in the compose file"
    )

    for name, service in services().items():
        for variable, value in (service.get("environment") or {}).items():
            if sensitive.search(variable):
                assert interpolated.match(str(value)), (
                    f"{name}'s {variable} is a literal: it must be `${{...}}` interpolation"
                )


def test_the_payload_directory_is_one_volume_the_api_and_the_worker_both_see() -> None:
    """Payload bytes are gzipped files under
    `$TUBEDEPTH_DATA_DIR/payloads/<kind>/<xx>/<sha256>.json.gz`
    (`payload_store.py`), not database rows. The worker writes them and the API
    serves them, so a `GET /v1/artifacts/{digest}` against an index that says
    the artifact exists is a 404 the moment the two see different directories.
    """
    parsed = services()
    data_directory = parsed["api"]["environment"]["TUBEDEPTH_DATA_DIR"]

    sources = set()
    for name in ("api", "worker"):
        service = parsed[name]
        assert service["environment"]["TUBEDEPTH_DATA_DIR"] == data_directory
        mounted = mounts(service)
        assert data_directory in mounted, f"{name} has no volume at {data_directory}"
        sources.add(mounted[data_directory])

    assert len(sources) == 1, (
        f"api and worker mount different sources at {data_directory}: {sources}"
    )
    volume = sources.pop()
    assert not volume.startswith("."), "a named volume, not a bind mount — see the compose comment"
    assert volume in compose()["volumes"], f"{volume} is mounted but never declared"


def test_the_long_running_services_wait_for_a_successful_migration() -> None:
    """`service_completed_successfully`, not `service_started`.

    The boot path issues no DDL (#14, docs/shared-postgres.md §6), so a
    process that starts before the migration finishes does not create what it
    is missing — it refuses to start, naming the fix. Waiting for *success* is
    also what stops a failed migration from being followed by a
    running-but-wrong stack.
    """
    parsed = services()

    for name in LONG_RUNNING:
        dependency = parsed[name]["depends_on"]["migrate"]
        assert dependency["condition"] == "service_completed_successfully", (
            f"{name} must wait for migrate to finish successfully, not merely to start"
        )


def test_migrate_is_a_one_shot_that_is_not_restarted() -> None:
    """A restarting `migrate` is a migration running again on every boot."""
    migrate = services()["migrate"]

    assert migrate["command"] == "migrate"
    assert str(migrate.get("restart", "no")) == "no"


def test_the_local_postgres_is_behind_a_profile_and_uses_the_real_bootstrap() -> None:
    """The external fleet database is the default; the local one is opt-in.

    And it is set up by `deploy/postgres-bootstrap.sql` itself — the file a
    real deployment runs — rather than a containerised copy of it, so what
    this stack is verified against is the shape production has.

    The SQL is mounted *outside* `/docker-entrypoint-initdb.d` on purpose:
    initdb runs `.sql` files there with a plain `psql -f` and no `-v`
    assignments, and this one needs three. The `.sh` wrapper in the initdb
    directory supplies them.
    """
    postgres = services()["postgres"]

    assert postgres["profiles"] == ["local"], "the local database must not come up by default"

    mounted = mounts(postgres)
    initdb = [target for target in mounted if target.startswith("/docker-entrypoint-initdb.d")]
    assert initdb, "nothing is mounted into initdb, so the bootstrap never runs"

    bootstrap = [
        target for target, source in mounted.items() if source.endswith("postgres-bootstrap.sql")
    ]
    assert bootstrap, "deploy/postgres-bootstrap.sql is not mounted"
    assert not any(target.startswith("/docker-entrypoint-initdb.d") for target in bootstrap), (
        "the bootstrap SQL must not sit in initdb directly — it needs psql -v variables"
    )

    wrapper_directory = DEPLOY / Path(mounted[initdb[0]]).name
    wrappers = sorted(wrapper_directory.glob("*.sh"))
    assert wrappers, f"{wrapper_directory} holds no wrapper to run the bootstrap"

    script = wrappers[0].read_text()
    assert bootstrap[0] in script, f"{wrappers[0].name} does not run the file the compose mounts"
    for variable in ("-v password=", "-v runtime_password=", "-v database="):
        assert variable in script, f"{wrappers[0].name} does not pass {variable}"

    # Statements only. The wrapper explains at length why it does *not* carry
    # the harness grant, and a substring search over the whole file would read
    # that explanation as the thing it forbids.
    statements = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )
    assert "GRANT CREATE ON DATABASE" not in statements, (
        "that grant is a test affordance for the per-test schema isolation, not a deployment one"
    )


def test_the_postgres_healthcheck_probes_tcp_rather_than_the_socket() -> None:
    """`pg_isready` run inside the container reaches the Unix socket by
    default, and the official image's entrypoint runs a temporary, local-only
    server during initdb that answers the socket while the bootstrap in
    `postgres-initdb/` may still be creating the roles. A pass at that moment
    releases `migrate` against a database that does not have them yet, and
    under `restart: "no"` its failure takes the whole stack with it.

    The temporary server never binds a TCP interface at all
    (`listen_addresses=''`), so `-h 127.0.0.1` is what makes "healthy" mean
    the real server, started after initdb — and so after the bootstrap — has
    finished. `tool/checks/test` documents the same trap and makes the same
    choice, probing the mapped port from the host.
    """
    check = " ".join(services()["postgres"]["healthcheck"]["test"])

    assert "pg_isready" in check
    assert "-h 127.0.0.1" in check, (
        "the probe must go over TCP, or initdb's temporary server answers it early — "
        "see the healthcheck comment and tool/checks/test"
    )


def test_the_api_binds_beyond_loopback_and_publishes_a_port() -> None:
    """The opposite of what `tests/test_deployment_units.py` asserts for the
    units, and deliberately so.

    `serve` defaults to 127.0.0.1, which in a container is the container's own
    loopback and reachable by nothing. What makes `0.0.0.0` safe here is that a
    container's network is private until a port is published, so exposure is
    one explicit `ports:` line rather than the default. On a host there is no
    such gate, which is why the units bind loopback and say so.
    """
    api = services()["api"]

    assert "--host 0.0.0.0" in api["command"] or "TUBEDEPTH_HOST" in api["environment"]
    assert api["ports"], "the API binds publicly inside the container but publishes nothing"


def test_the_api_healthcheck_is_in_compose_and_not_in_the_image() -> None:
    """`/healthz` is unauthenticated on purpose (`api/application.py`), so it
    can be checked before anyone has a key.

    The image carries no HEALTHCHECK, and that is the decision being asserted:
    it would be wrong for the worker and the watcher (no endpoint) and for
    `migrate`, a one-shot that is supposed to exit — Docker would mark a
    successful migration unhealthy.
    """
    parsed = services()

    assert "healthz" in str(parsed["api"]["healthcheck"]["test"])
    for name in ("worker", "watch", "migrate"):
        assert "healthcheck" not in parsed[name], f"{name} answers no endpoint"

    # Instructions only. The Dockerfile explains at length why it has no
    # HEALTHCHECK, and a substring search would read its own explanation as
    # the thing it forbids.
    dockerfile = (DEPLOY.parent / "Dockerfile").read_text()
    declared = [
        line for line in dockerfile.splitlines() if line.strip().upper().startswith("HEALTHCHECK")
    ]
    assert not declared, f"the image must carry no HEALTHCHECK, found: {declared}"


def test_the_worker_polls_rather_than_draining_once_and_restarting() -> None:
    """`--poll` defaults to 0.0, which drains the queue once and exits. With a
    restart policy that turns the worker into a poll loop made of container
    starts: a full interpreter launch and a fresh set of database connections
    every few seconds, almost always to find nothing.
    """
    worker = services()["worker"]
    words = worker["command"].split()

    assert "--poll" in words, "without --poll the worker exits after one drain"
    assert float(words[words.index("--poll") + 1]) > 0
    assert worker.get("restart") == "unless-stopped"


def test_the_worker_is_given_time_to_release_its_lease() -> None:
    """`_stopping_on_signals()` handles SIGTERM, so a drain in flight finishes
    and the lease is released instead of being abandoned to wait out its full
    lease before another worker may take it. Compose sends SIGTERM and then
    SIGKILL after `stop_grace_period`, so a short window throws away exactly
    the shutdown that handler exists to provide.
    """
    grace = services()["worker"]["stop_grace_period"]

    assert int(str(grace).rstrip("s")) >= 60


def test_the_watcher_stays_resident_because_compose_has_no_timers() -> None:
    """`--every` exists for environments with no scheduler, compose among them.
    Without it `watch` queues one pass and exits, and a restart policy makes
    that a container-launch loop.

    The list itself is a required positional argument (envvar
    `TUBEDEPTH_WATCHLIST`), because the failure worth designing against is a
    watcher that quietly watches nothing.
    """
    watch = services()["watch"]
    words = watch["command"].split()

    assert "--every" in words, "without --every the watcher queues one pass and exits"
    assert float(words[words.index("--every") + 1]) > 0

    positional = [word for word in words[1:] if not word.startswith("--")]
    assert positional, "watch takes the list as a required argument; nothing was passed"
    assert positional[0] in mounts(watch), (
        f"{positional[0]} is passed to watch but nothing is mounted there"
    )


def test_the_help_helper_reads_a_clean_environment() -> None:
    """The behaviour the shared helper must not lose.

    Every option here carries an `envvar=` default that typer prints into the
    help text, so a `TUBEDEPTH_*` variable in the shell running pytest could
    otherwise change what the option checks read.
    """
    text = help_for("serve")

    assert "--host" in text and "127.0.0.1" in text
