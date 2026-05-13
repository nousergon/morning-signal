"""CLI entry point.

This is a thin shim for PR 1 — just delegates to episode.main(). PR 2 will
replace this with a typer-based multi-subcommand CLI (init, generate, preview,
subscribe, version).
"""

from __future__ import annotations

import logging

from morning_signal.episode import main as _main


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    _main()


if __name__ == "__main__":
    main()
