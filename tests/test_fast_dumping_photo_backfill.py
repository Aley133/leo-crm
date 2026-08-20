from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError

import pytest

from backend.app.fast_dumping_agent_api import (
    FastPhotoComplete,
    _claim_photo_job,
    complete_photo,
)
from backend.app.models import Product
from tools import kaspi_fast_dumping_agent as agent


def _product(
    *,
    kaspi_id: str,
    name: str,
    image_url: str | None = None,
    workspace_id: int = 1,
) -> Product:
    return Product(
        workspace_id=workspace_id,
        kaspi_product_id=kaspi_id,
        merchant_sku=f"{kaspi_id}_SKU",
        name=name,
        image_url=image_url,
    )


def test_photo_claim_prioritizes_visible_missing_product_and_leases_it(db_session) -> None:
    cached = _product(
        kaspi_id="110000001",
        name="Cached",
        image_url="https://resources.cdn-kaspi.kz/img/cached.jpg",
    )
    backlog = _product(kaspi_id="110000002", name="Backlog")
    visible = _product(kaspi_id="110000003", name="Visible")
    visible.image_backfill_after = datetime.now(UTC) - timedelta(seconds=1)
    db_session.add_all([cached, backlog, visible])
    db_session.commit()

    job = _claim_photo_job(db_session, agent_id="agent-w1")
    db_session.commit()

    assert job is not None
    assert job["product_id"] == visible.id
    assert job["catalog_workspace_id"] == 1
    assert job["kaspi_product_id"] == "110000003"
    assert job["city_id"] == "196220100"
    assert len(job["lease_token"]) == 32
    db_session.refresh(visible)
    assert visible.image_backfill_agent_id == "agent-w1"
    assert visible.image_backfill_lease_token == job["lease_token"]
    assert visible.image_backfill_after > datetime.now(UTC)


def test_photo_claim_covers_the_full_catalog_across_workspaces(db_session) -> None:
    db_session.info["include_all_workspaces"] = True
    leoxpress_product = _product(
        kaspi_id="110000004",
        name="LeoXpress product without dumping",
        workspace_id=3,
    )
    db_session.add(leoxpress_product)
    db_session.commit()

    job = _claim_photo_job(db_session, agent_id="agent-w1")

    assert job is not None
    assert job["product_id"] == leoxpress_product.id
    assert job["catalog_workspace_id"] == 3


def test_photo_claim_reuses_a_cached_image_across_workspaces_without_http(db_session) -> None:
    db_session.info["include_all_workspaces"] = True
    cached = _product(
        kaspi_id="110000005_111",
        name="BARWORK cached product",
        image_url="https://resources.cdn-kaspi.kz/img/shared.jpg",
        workspace_id=1,
    )
    leoxpress_product = _product(
        kaspi_id="110000005_222",
        name="LeoXpress duplicate",
        workspace_id=3,
    )
    next_product = _product(
        kaspi_id="110000006",
        name="Next global product",
        workspace_id=3,
    )
    db_session.add_all([cached, leoxpress_product, next_product])
    db_session.commit()

    job = _claim_photo_job(db_session, agent_id="agent-w1")
    db_session.commit()

    assert leoxpress_product.image_url == cached.image_url
    assert leoxpress_product.image_backfill_after is None
    assert job is not None and job["product_id"] == next_product.id


def test_photo_completion_saves_url_atomically_and_never_reclaims_product(db_session) -> None:
    product = _product(kaspi_id="110000010", name="Needs photo")
    db_session.add(product)
    db_session.commit()
    job = _claim_photo_job(db_session, agent_id="agent-w1")
    db_session.commit()
    assert job is not None

    result = complete_photo(
        product.id,
        FastPhotoComplete(
            agent_id="agent-w1",
            workspace_id=1,
            lease_token=job["lease_token"],
            status="succeeded",
            image_url="https://resources.cdn-kaspi.kz/img/m/p/photo.jpg",
        ),
        db_session,
    )

    assert result["status"] == "saved"
    db_session.refresh(product)
    assert product.image_url == "https://resources.cdn-kaspi.kz/img/m/p/photo.jpg"
    assert product.image_backfill_after is None
    assert product.image_backfill_lease_token is None
    assert product.image_backfill_agent_id is None
    assert product.image_backfill_error is None
    assert _claim_photo_job(db_session, agent_id="agent-w1") is None


def test_photo_completion_shares_the_image_with_leoxpress(db_session) -> None:
    db_session.info["include_all_workspaces"] = True
    barwork_product = _product(
        kaspi_id="110000011",
        name="BARWORK product",
        workspace_id=1,
    )
    leoxpress_product = _product(
        kaspi_id="110000011_999",
        name="LeoXpress product",
        workspace_id=3,
    )
    db_session.add_all([barwork_product, leoxpress_product])
    db_session.commit()
    job = _claim_photo_job(db_session, agent_id="agent-w1")
    db_session.commit()
    assert job is not None

    result = complete_photo(
        barwork_product.id,
        FastPhotoComplete(
            agent_id="agent-w1",
            workspace_id=1,
            lease_token=job["lease_token"],
            status="succeeded",
            image_url="https://resources.cdn-kaspi.kz/img/m/p/shared-photo.jpg",
        ),
        db_session,
    )

    assert result["status"] == "saved"
    assert result["updated_products"] == 2
    assert barwork_product.image_url == result["image_url"]
    assert leoxpress_product.image_url == result["image_url"]


def test_photo_failure_is_deferred_without_blocking_other_products(db_session) -> None:
    failed = _product(kaspi_id="110000020", name="Failed")
    next_product = _product(kaspi_id="110000021", name="Next")
    db_session.add_all([failed, next_product])
    db_session.commit()
    job = _claim_photo_job(db_session, agent_id="agent-w1")
    db_session.commit()
    assert job is not None and job["product_id"] == failed.id

    result = complete_photo(
        failed.id,
        FastPhotoComplete(
            agent_id="agent-w1",
            workspace_id=1,
            lease_token=job["lease_token"],
            status="failed",
            error_code="HTTPError",
            error_message="Kaspi returned 429",
            retry_after_seconds=1800,
        ),
        db_session,
    )

    assert result == {
        "status": "deferred",
        "retry_after_seconds": 1800,
        "attempts_remaining": 1,
    }
    db_session.refresh(failed)
    assert failed.image_url is None
    assert failed.image_backfill_error == "Kaspi returned 429"
    assert failed.image_backfill_after > datetime.now(UTC)
    next_job = _claim_photo_job(db_session, agent_id="agent-w1")
    assert next_job is not None and next_job["product_id"] == next_product.id


def test_photo_backfill_stops_after_two_failed_requests(db_session) -> None:
    product = _product(kaspi_id="110000022", name="Bounded failure")
    db_session.add(product)
    db_session.commit()

    first = _claim_photo_job(db_session, agent_id="agent-w1")
    db_session.commit()
    assert first is not None
    complete_photo(
        product.id,
        FastPhotoComplete(
            agent_id="agent-w1",
            workspace_id=1,
            lease_token=first["lease_token"],
            status="failed",
            error_code="HTTPError",
            retry_after_seconds=60,
        ),
        db_session,
    )
    product.image_backfill_after = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()

    second = _claim_photo_job(db_session, agent_id="agent-w1")
    db_session.commit()
    assert second is not None
    stopped = complete_photo(
        product.id,
        FastPhotoComplete(
            agent_id="agent-w1",
            workspace_id=1,
            lease_token=second["lease_token"],
            status="failed",
            error_code="HTTPError",
            retry_after_seconds=60,
        ),
        db_session,
    )

    assert stopped == {
        "status": "stopped",
        "retry_after_seconds": None,
        "attempts_remaining": 0,
    }
    assert _claim_photo_job(db_session, agent_id="agent-w1") is None


def test_local_agent_reads_large_photo_from_one_json_request(monkeypatch) -> None:
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({
                "data": {
                    "card": {"id": "115247718"},
                    "galleryImages": [{
                        "large": "https://resources.cdn-kaspi.kz/img/m/p/photo-large.jpg",
                    }],
                },
            }).encode("utf-8")

    def open_once(request, *, timeout):
        calls.append((request, timeout))
        return Response()

    monkeypatch.setattr(agent, "urlopen", open_once)

    image_url = agent._fetch_kaspi_photo("115247718_699622601", "196220100")

    assert image_url.endswith("photo-large.jpg")
    assert len(calls) == 1
    assert "productCode=115247718" in calls[0][0].full_url
    assert "cityId=196220100" in calls[0][0].full_url


def test_local_agent_does_not_retry_kaspi_429(monkeypatch) -> None:
    calls = 0

    def rate_limited(_request, *, timeout):
        nonlocal calls
        calls += 1
        raise HTTPError(
            agent.KASPI_PHOTO_ENDPOINT,
            429,
            "Too Many Requests",
            {},
            io.BytesIO(b"rate limited"),
        )

    monkeypatch.setattr(agent, "urlopen", rate_limited)

    with pytest.raises(agent.KaspiPhotoRequestError) as raised:
        agent._fetch_kaspi_photo("115247718", "196220100")

    assert calls == 1
    assert raised.value.retry_after_seconds == agent.PHOTO_TRANSIENT_RETRY_SECONDS


def test_photo_backfill_runs_only_inside_confirmed_price_idle_window() -> None:
    source = open(agent.__file__, encoding="utf-8").read()

    assert "/api/fast-dumping-agent/photo-claim" in source
    assert "number == 1" in source
    assert "now < price_idle_confirmed_until" in source
    assert source.index('/api/fast-dumping-agent/claim"') < source.index(
        '/api/fast-dumping-agent/photo-claim"'
    )
    assert "PHOTO_REQUEST_SPACING_SECONDS = 1.5" in source
