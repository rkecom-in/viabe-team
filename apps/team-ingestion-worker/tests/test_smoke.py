"""Smoke tests for the ingestion worker scaffold."""

import team_ingestion_worker


def test_package_version() -> None:
    assert team_ingestion_worker.__version__ == "0.1.0"
