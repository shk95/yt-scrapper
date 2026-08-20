# List all the just commands
default:
    @just --list

############################################################################
#
#  repository checks
#
#  The recipes here delegate to tool/checks/*, which are also what the git
#  hooks and CI run. Keeping the commands in the scripts rather than here is
#  what makes those three agree; a Justfile recipe that inlined the command
#  would be a fourth definition to keep in sync.
#
############################################################################

[group('repository')]
doctor:
    tool/doctor.sh

# Formatting, static analysis and the offline test suite.
[group('repository')]
check:
    tool/checks/format
    tool/checks/lint
    tool/checks/test

[group('repository')]
format-check:
    tool/checks/format

# Apply the formatting that `just format-check` only reports.
[group('repository')]
format:
    uv run ruff format .

[group('repository')]
lint:
    tool/checks/lint

[group('repository')]
test:
    tool/checks/test

# Never run by hooks or CI: they are red on any datacenter address for
# reasons unrelated to the change under test. Run deliberately, on a
# residential connection.

# Run the live tests that actually reach YouTube
[group('repository')]
contract:
    uv run pytest -m live

# The migration checks against a real PostgreSQL server, which this project is
# moving to (`docs/status.md`). Not part of `just check`: that suite must stay
# runnable with nothing installed, and a missing container is not a failing
# change. It brings the server up with `deploy/postgres-bootstrap.sql`, the
# same file a real deployment runs, so what is checked is the shape production
# has — including the `search_path` that decides where `alembic_version` lands.

# Start a throwaway PostgreSQL, run the migration checks against it, remove it
[group('repository')]
postgres:
    #!/usr/bin/env bash
    set -euo pipefail
    name=tubedepth-pg-check
    trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT
    docker rm -f "$name" >/dev/null 2>&1 || true
    docker run -d --name "$name" -e POSTGRES_PASSWORD=fleet -e POSTGRES_USER=fleet \
        -e POSTGRES_DB=fleet -p 55432:5432 postgres:18-alpine >/dev/null
    for _ in $(seq 1 60); do
        docker exec "$name" pg_isready -U fleet -q 2>/dev/null && break
        sleep 1
    done
    docker exec -i "$name" psql -U fleet -d fleet -v ON_ERROR_STOP=1 \
        -v password="'check'" -v runtime_password="'check-runtime'" -v database=fleet \
        -q < deploy/postgres-bootstrap.sql
    # Harness only, and deliberately not in the bootstrap file: the tests drop
    # and recreate the schema between cases so a half-applied migration cannot
    # decide what the next one starts from. In production the service role has
    # no business creating schemas.
    docker exec "$name" psql -U fleet -d fleet -q -c 'GRANT CREATE ON DATABASE fleet TO tubedepth_migrator'
    TUBEDEPTH_TEST_POSTGRES_URL='postgresql+psycopg://tubedepth_migrator:check@localhost:55432/fleet' \
        TUBEDEPTH_TEST_POSTGRES_RUNTIME_URL='postgresql+psycopg://tubedepth_runtime:check-runtime@localhost:55432/fleet' \
        tool/checks/postgres

############################################################################
#
#  running it
#
############################################################################

# The API and the worker are two processes here for the same reason they are
# two units in production: yt-dlp extraction blocks and holds memory, and a
# crash in it must not take the API with it. Run them in two terminals.
#
# There used to be a `dev` recipe here promising `serve --with-worker`. That
# option has never existed, so the recipe has never run.

[group('run')]
serve port="8080":
    uv run tubedepth serve --port {{port}}

[group('run')]
worker:
    uv run tubedepth work

############################################################################
#
#  fixtures
#
############################################################################

# Reaches the network on purpose. Review the diff before committing: because
# the fixtures are pretty-printed and stripped of tracking noise, that diff is
# a readable list of what YouTube changed.

# Record one fixture at a time. There is still no way to re-record them all;
# the InnerTube recipe below is what closed the other half of issue #10.

# Record a yt-dlp fixture for one video
[group('data')]
fixture-capture target name:
    uv run tubedepth capture-fixture {{target}} --name {{name}}

# Run after a deliberate schema_version bump. Refuses to rewrite a shape
# already recorded against the version that is still current — the only way to
# make that check pass is the bump it is asking for.

# Append the current payload shapes to the lock
[group('repository')]
record-payload-shapes:
    uv run pytest tests/test_payload_shapes.py --record-payload-shapes

# Record an InnerTube fixture: next-related, browse-channel-home, browse-community
[group('data')]
fixture-capture-innertube surface target name:
    uv run tubedepth capture-fixture {{target}} --name {{name}} --innertube {{surface}}

############################################################################
#
#  dependencies
#
############################################################################

# yt-dlp is pinned without an upper bound precisely so this is the whole fix
# when YouTube changes something. Try it before debugging this project.

# Upgrade yt-dlp to the latest release and re-lock
[group('deps')]
update-ytdlp:
    uv lock --upgrade-package yt-dlp
    uv sync --extra dev --frozen
    uv run python -c "import yt_dlp; print('yt-dlp', yt_dlp.version.__version__)"
