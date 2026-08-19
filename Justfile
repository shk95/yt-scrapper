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

# Record one yt-dlp fixture. There is no way to re-record them all, and no
# way to record an InnerTube one at all — see issue #10.

# Record a yt-dlp fixture for one video
[group('data')]
fixture-capture target name:
    uv run tubedepth capture-fixture {{target}} --name {{name}}

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
