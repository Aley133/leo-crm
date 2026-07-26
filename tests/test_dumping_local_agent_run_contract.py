from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dumping_run_now_route_is_registered_for_current_frontend() -> None:
    compat = (ROOT / "backend" / "app" / "dumping_run_compat_api.py").read_text(encoding="utf-8")
    main = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    frontend = (ROOT / "backend" / "app" / "static" / "dumping.js").read_text(encoding="utf-8")

    assert '@router.post("/products/{product_id}/run-now"' in compat
    assert "app.include_router(dumping_run_compat_router)" in main
    assert "/run-now" in frontend


def test_local_queue_states_are_normalized_for_live_dumping_ui() -> None:
    source = (ROOT / "backend" / "app" / "dumping_competitor_worker.py").read_text(encoding="utf-8")

    assert '"queued_local": "queued"' in source
    assert '"leased_local": "scanning"' in source
    assert '"succeeded_local": "completed"' in source
    assert '"failed_local": "failed"' in source
