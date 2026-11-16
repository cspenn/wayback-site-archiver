"""Typer CLI entry point for the Wayback site archiver."""

import os
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Annotated

import typer

from civil_service.config import configure_logging, load_settings
from civil_service.exceptions import CivilServiceError
from civil_service.runner import run_archiver

_SIGINT_EXIT_CODE = 130


@contextmanager
def hard_stop_on_second_interrupt() -> Iterator[None]:
    """Turn the first Ctrl-C into KeyboardInterrupt. Quit at once on the second one."""
    presses = 0

    def handler(_signum: int, _frame: FrameType | None) -> None:
        nonlocal presses
        presses += 1
        if presses > 1:
            os._exit(_SIGINT_EXIT_CODE)
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGINT, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


app = typer.Typer(add_completion=False, help="Crawl a site and archive missing URLs on Wayback.")


@app.command()
def archive(
    start_url: Annotated[str, typer.Argument(help="Starting URL to crawl and archive.")],
    config: Annotated[
        Path, typer.Option("--config", help="Path to YAML configuration file.")
    ] = Path("config.yml"),
) -> None:
    """Crawl start_url and submit unarchived public pages and links to archive.org."""
    try:
        settings = load_settings(config)
        configure_logging(settings.log_level)
        with hard_stop_on_second_interrupt():
            summary = run_archiver(start_url, settings)
    except CivilServiceError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except KeyboardInterrupt as exc:
        typer.echo("Interrupted. Run the same command to resume.", err=True)
        raise typer.Exit(code=_SIGINT_EXIT_CODE) from exc

    typer.echo(
        f"Done: crawled={summary.crawled} robots_blocked={summary.robots_blocked} "
        f"crawl_failed={summary.crawl_failed} archived={summary.archived} "
        f"submitted={summary.submitted} archive_skipped={summary.archive_skipped} "
        f"archive_failed={summary.archive_failed}\n"
        f"Coverage: {summary.covered}/{summary.eligible} ({summary.coverage_percent:.1f}%) "
        f"confirmed, {summary.unconfirmed} submitted but unconfirmed"
    )


if __name__ == "__main__":
    app()
