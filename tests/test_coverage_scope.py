"""The coverage gate's *scope* is asserted here, not only its number.

repository-baseline-policy.md §4.2 C5: the way a coverage gate stops being
honest is by narrowing what it measures rather than by lowering the number —
which reads as an improvement in every report. Measured on symposion, removing
one flag moved the reported figure from 34.76% to 92.36% with no new test code.

So these tests assert three things a passing suite cannot otherwise notice:

* the measured source is the WHOLE ``morning_signal`` package (C1), never a
  subset of it;
* the floor is enforced by a non-zero exit (C2) and is a ratchet that may be
  raised and never lowered (C3);
* nothing is omitted from measurement by a coverage config, which would shrink
  the denominator without touching either flag.
"""

from __future__ import annotations

import configparser
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTEST_INI = REPO_ROOT / "pytest.ini"
PACKAGE_ROOT = REPO_ROOT / "src" / "morning_signal"

#: The floor may be RAISED here as coverage improves. Lowering it is a policy
#: amendment (repository-baseline-policy.md §4.2 C3), not a code change.
MINIMUM_FLOOR = 92


def _addopts() -> str:
    parser = configparser.ConfigParser()
    parser.read(PYTEST_INI, encoding="utf-8")
    return parser["pytest"]["addopts"]


def test_coverage_source_is_the_whole_package() -> None:
    """C1 — ``--cov`` names the package, so unimported modules still count."""
    sources = re.findall(r"--cov=(\S+)", _addopts())
    assert sources == ["morning_signal"], (
        f"coverage source must be exactly the morning_signal package, got {sources!r}. "
        "Narrowing it to a submodule or a path measures the tested subset and "
        "reports it as the repository."
    )


def test_coverage_floor_is_enforced_and_never_lowered() -> None:
    """C2 + C3 — the gate exits non-zero below a floor that only ratchets up."""
    found = re.findall(r"--cov-fail-under=(\d+)", _addopts())
    assert len(found) == 1, f"expected exactly one --cov-fail-under, got {found!r}"
    assert int(found[0]) >= MINIMUM_FLOOR, (
        f"coverage floor {found[0]} is below the ratchet {MINIMUM_FLOOR}. "
        "A floor is raised as coverage improves and never lowered to make a "
        "change pass (repository-baseline-policy.md §4.2 C3)."
    )


def test_no_coverage_config_omits_source_files() -> None:
    """A shrunk denominator is a narrowing that neither flag would reveal."""
    for name in (".coveragerc", "setup.cfg", "tox.ini", "pyproject.toml"):
        path = REPO_ROOT / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*omit\s*=", text, re.MULTILINE), (
            f"{name} sets a coverage `omit` — removing files from the "
            "denominator raises the reported figure without adding a test."
        )


def test_every_source_module_is_inside_the_measured_package() -> None:
    """No source file lives outside what ``--cov=morning_signal`` measures."""
    src = REPO_ROOT / "src"
    stray = sorted(
        p.relative_to(REPO_ROOT).as_posix()
        for p in src.rglob("*.py")
        if PACKAGE_ROOT not in p.parents and p != PACKAGE_ROOT
    )
    assert not stray, (
        f"source modules outside the measured package are invisible to the "
        f"coverage gate: {stray}"
    )
