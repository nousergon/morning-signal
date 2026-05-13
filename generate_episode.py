#!/usr/bin/env python3
"""Backward-compatibility shim.

The real implementation lives in the `morning_signal` package. This file
exists so existing systemd / launchd units that exec `python generate_episode.py`
keep working without unit-file changes during the PyPI rollout.

Prefer the `morning-signal` console script (or `python -m morning_signal.cli`)
for new deployments.
"""

from morning_signal.cli import main

if __name__ == "__main__":
    main()
