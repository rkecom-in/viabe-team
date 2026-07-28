"""VT-711 static custody/inertness checks for checkpoint C."""

from __future__ import annotations

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[3]
_SRC = _ROOT / "src" / "orchestrator"


def test_checkpoint_c_modules_have_no_live_importer() -> None:
    forbidden = (
        "orchestrator.knowledge.learning_loop",
        "orchestrator.knowledge.admission",
        "orchestrator.knowledge.rollout",
    )
    owners = {
        _SRC / "knowledge" / "learning_loop.py",
        _SRC / "knowledge" / "admission.py",
        _SRC / "knowledge" / "rollout.py",
    }
    importers: list[str] = []
    for path in _SRC.rglob("*.py"):
        if path in owners:
            continue
        text = path.read_text(encoding="utf-8")
        if any(module in text for module in forbidden):
            importers.append(str(path.relative_to(_ROOT)))
    assert importers == [], f"checkpoint C unexpectedly wired live: {importers}"


def test_no_environment_driven_rollout_activation() -> None:
    rollout = (_SRC / "knowledge" / "rollout.py").read_text(encoding="utf-8")
    assert "os.environ" not in rollout
    assert "getenv(" not in rollout
    assert "RolloutMode.OFF" in rollout


def test_only_clau_allocated_vt711_addendum_migration_was_added() -> None:
    migration_root = _ROOT.parents[1] / "migrations"
    assert [path.name for path in migration_root.glob("*vt711*")] == [
        "186_vt711_specialist_memory_and_assignment.sql"
    ]
