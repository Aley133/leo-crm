from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "0017_repair_browser_agent_jobs.py"


def test_browser_agent_queue_repair_migration_is_production_safe() -> None:
    script = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "0017"' in script
    assert 'down_revision = "0016"' in script
    assert '"browser_agent_jobs" not in tables' in script
    assert 'op.create_table(' in script
    assert 'sa.ForeignKey("monitor_targets.id", ondelete="CASCADE")' in script
    assert 'sa.UniqueConstraint("lease_token"' in script
    assert 'ix_browser_agent_jobs_monitor_target_id' in script
    assert 'ix_browser_agent_jobs_supplier_product_id' in script
    assert 'ix_browser_agent_jobs_status' in script
    assert 'ix_browser_agent_jobs_lease_until' in script
    assert "Downgrade is intentionally a no-op" in script
