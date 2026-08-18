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

############################################################################
#
#  running it
#
############################################################################

# API plus an in-process worker. For demos and development only; production
# runs the two as separate units so a yt-dlp crash cannot take the API down.

# Run the API with an in-process worker (development only)
[group('run')]
dev port="8080":
    uv run tubedepth serve --with-worker --port {{port}}

[group('run')]
serve port="8080":
    uv run tubedepth serve --port {{port}}

[group('run')]
worker:
    uv run tubedepth worker

############################################################################
#
#  egress pool
#
############################################################################

# The check that matters: an egress reporting the SAME public address as
# `direct` has no tunnel and is silently leaking the origin IP.

# Show each egress's public address, country and health
[group('egress')]
egress-probe:
    uv run tubedepth egress probe

[group('egress')]
egress-status:
    uv run tubedepth egress status

# Reads egress_attempt. This is the answer to "what does one IP actually
# sustain against YouTube", which nobody can tell you in advance.

# Report measured per-egress throughput and block rates
[group('egress')]
egress-report since="24h":
    uv run tubedepth egress report --since {{since}}

############################################################################
#
#  fixtures
#
############################################################################

# Reaches the network on purpose. Review the diff before committing: because
# the fixtures are pretty-printed and stripped of tracking noise, that diff is
# a readable list of what YouTube changed.

# Re-record the InnerTube and yt-dlp fixtures
[group('data')]
fixtures-refresh:
    uv run tubedepth capture-fixture --all

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
