"""Smoke tests for the orchestrator scaffold."""

import team_orchestrator


def test_package_version() -> None:
    assert team_orchestrator.__version__ == "0.1.0"
