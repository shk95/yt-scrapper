"""The command line. The HTTP API will sit on the same service layer."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .collection import CollectionService
from .errors import TubedepthError
from .payload_store import PayloadStore
from .sources.ytdlp_runtime import LibraryYtdlpRuntime

application = typer.Typer(
    name="tubedepth",
    help="Collect the YouTube data the official Data API does not expose.",
    no_args_is_help=True,
)


def _payload_store(data_directory: Path) -> PayloadStore:
    return PayloadStore(data_directory / "payloads")


@application.command()
def collect(
    target: Annotated[str, typer.Argument(help="A video URL or bare video id")],
    data_directory: Annotated[
        Path,
        typer.Option("--data-dir", envvar="TUBEDEPTH_DATA_DIR", help="Where payloads are stored"),
    ] = Path("var"),
    show: Annotated[bool, typer.Option("--show/--no-show", help="Print the payload")] = False,
) -> None:
    """Collect one video's metadata and store it."""
    payloads = _payload_store(data_directory)
    service = CollectionService(runtime=LibraryYtdlpRuntime(), payloads=payloads)

    typer.echo(f"→ collecting {target}")
    stored = service.collect_video_metadata(target)
    typer.echo(f"✓ stored {stored.byte_count} bytes at {stored.path}")
    if show:
        typer.echo(
            json.dumps(json.loads(payloads.read(stored.digest)), indent=2, ensure_ascii=False)
        )


@application.command()
def version() -> None:
    """Print the app version and the yt-dlp it is actually running.

    The first question when extraction breaks is which yt-dlp this is, and the
    answer is not whatever `yt-dlp --version` on PATH says: everything here
    runs the version uv.lock pins.
    """
    from yt_dlp import version as ytdlp_version

    typer.echo(f"tubedepth {__version__}")
    typer.echo(f"yt-dlp    {ytdlp_version.__version__}")


def main() -> None:
    try:
        application()
    except TubedepthError as error:
        # SystemExit, not typer.Exit: typer.Exit is only meaningful inside
        # typer's own invocation, and raising it here printed the traceback
        # this handler exists to replace.
        typer.echo(f"✗ {error}", err=True)
        raise SystemExit(1) from error


if __name__ == "__main__":
    os.environ.setdefault("COLUMNS", "100")
    main()
