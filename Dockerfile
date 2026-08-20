# One image for every process this project runs.
#
# `ENTRYPOINT ["tubedepth"]` and no `CMD`, so a compose service — or a
# `docker run` — differs from the next only by its arguments. Four things run
# out of this image (`migrate`, `serve`, `work`, `watch`) and building four
# images for them would be four things to keep in step for no gain: they are
# the same package, the same lock file, and the same code path into the
# database.
#
# NO `HEALTHCHECK` HERE, ON PURPOSE. An image-level healthcheck applies to
# every container started from the image, and it would be wrong for three of
# the four: `work` drains a queue and answers no port, `watch` queues rows and
# answers no port, and `migrate` is a one-shot that is *supposed* to exit —
# Docker would mark a successful migration unhealthy. Only `serve` has
# `/healthz`, so the healthcheck belongs to the one service that serves it,
# which is `deploy/docker-compose.yml`.

# uv, taken from its own published image rather than curl'd in: this pins the
# installer the same way uv.lock pins everything it installs.
FROM ghcr.io/astral-sh/uv:0.12.1 AS uv

# ---------------------------------------------------------------------------
# Build: resolve nothing, install exactly what uv.lock says.
# ---------------------------------------------------------------------------
FROM python:3.13-slim-trixie AS build

COPY --from=uv /uv /usr/local/bin/uv

# UV_COMPILE_BYTECODE trades build time for start-up time, which is the right
# way round for a container that is started far more often than it is built —
# `migrate` and `watch` are short-lived processes.
# UV_PYTHON_DOWNLOADS=never keeps uv on the interpreter this image already
# has instead of quietly fetching a second one.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Two steps, not one, and the split is the whole point of this stage.
#
# The first installs *only* the dependencies (`--no-install-project`), from a
# layer whose inputs are pyproject.toml and uv.lock alone — so editing a
# source file does not invalidate it and does not re-download yt-dlp,
# psycopg's bundled libpq and the rest.
#
# `--frozen` refuses to re-resolve: the image is built from the lock file that
# was committed, not from whatever PyPI has today. yt-dlp is deliberately
# uncapped in pyproject.toml (AGENTS.md says why), so without this the image
# would ship a version nobody chose.
#
# No `--extra dev`: ruff, basedpyright and pytest are for a checkout, not for
# a container.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# The project's version is `dynamic` and hatchling reads it out of
# `src/tubedepth/__init__.py` (`[tool.hatch.version]`), and `readme`/`license`
# name two more files — so all of them must exist before the wheel can be
# built. A Dockerfile that copied only pyproject.toml and uv.lock and then
# tried to install the project here would fail at metadata time.
COPY README.md LICENSE ./
COPY src/ ./src/
# `tubedepth migrate` resolves alembic.ini and migrations/ relative to the
# package's own location (`cli.py`: `Path(__file__).parent.parent.parent`), and
# `uv sync` installs this project in editable mode — the .pth file points at
# /app/src, so that expression is /app inside the image. Both must therefore be
# here, at this exact path, or `migrate` starts and cannot find its script
# directory.
COPY alembic.ini ./
COPY migrations/ ./migrations/

RUN uv sync --frozen

# ---------------------------------------------------------------------------
# Runtime: the venv and the source, as a user that is not root.
# ---------------------------------------------------------------------------
FROM python:3.13-slim-trixie AS runtime

# Not root. Nothing this image runs needs a privileged port or a system path,
# and a scraper that executes yt-dlp against attacker-supplied input is exactly
# the process you want unable to write outside its own directory.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin tubedepth

# The payload store: gzipped files under $TUBEDEPTH_DATA_DIR/payloads, written
# by `work` and read by `serve`. It is created here and owned by the runtime
# user, because a directory Docker creates for a volume mount is owned by root
# and the first write then fails with EACCES — which reads like a database
# problem and is not. In compose this path is a named volume shared by api and
# worker; see deploy/docker-compose.yml.
ENV TUBEDEPTH_DATA_DIR=/var/lib/tubedepth
RUN mkdir -p "$TUBEDEPTH_DATA_DIR" && chown tubedepth:tubedepth "$TUBEDEPTH_DATA_DIR"

# No uv in this stage: the venv is complete, and an image that can resolve
# dependencies at run time is an image whose contents are not what was built.
COPY --from=build --chown=tubedepth:tubedepth /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app
USER tubedepth

ENTRYPOINT ["tubedepth"]
