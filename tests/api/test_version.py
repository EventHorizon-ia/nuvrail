"""
The API reports the package version from pyproject.toml, not a hardcoded copy.

Before #177 api/main.py carried its own "0.1.1"; the release workflow checks
pyproject.toml and web/package.json against the tag but never saw this third
copy, so it could silently drift on the next bump.
"""
from __future__ import annotations

import importlib.metadata
from pathlib import Path

import pytest

from api import main
from api.main import app

tomllib = pytest.importorskip("tomllib")  # stdlib on 3.11+, which CI runs

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _pyproject_version() -> str:
    with _PYPROJECT.open("rb") as f:
        return tomllib.load(f)["project"]["version"]


def test_app_version_matches_pyproject() -> None:
    # Editable-install metadata is frozen at install time: after a version
    # bump, re-run `pip install -e ".[dev]"` (CI and Docker always do).
    assert app.version == _pyproject_version(), (
        "api version comes from installed package metadata; reinstall after bumping pyproject.toml"
    )


def test_openapi_reports_pyproject_version() -> None:
    assert app.openapi()["info"]["version"] == _pyproject_version()


def test_falls_back_when_package_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _not_installed(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(main.importlib.metadata, "version", _not_installed)
    assert main._package_version() == "0+unknown"
