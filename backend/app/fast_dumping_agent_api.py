from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Lock
from time import monotonic
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .db import get_unscoped_db
from .fast_dumping_service import (
    claim_job,
    complete_apply,
    complete_scan,
    complete_verification,
    prepare_apply,
    serialize_claimed_job,
)
from .models import Product
from .product_images import normalize_product_image_url
from .workspace_context import LEGACY_WORKSPACE_ID, current_workspace_id, workspace_context
from .workspace_models import KaspiAccountCredential


class FastAgentIdentity(BaseModel):
    agent_id: str = Field(min_length=1, max_length=255)
    workspace_id: int = Field(default=LEGACY_WORKSPACE_ID, ge=1)
    hostname: str | None = Field(default=None, max_length=255)
    platform: str | None = Field(default=None, max_length=500)
    version: str | None = Field(default=None, max_length=64)
    concurrency: int = Field(default=1, ge=1, le=8)
    merchant_uid: str = Field(min_length=1, max_length=128)


class FastAgentHeartbeat(FastAgentIdentity):
    status: str = Field(default="online", max_length=32)


class FastScanComplete(BaseModel):
    agent_id: str = Field(min_length=1, max_length=255)
    workspace_id: int = Field(ge=1)
    lease_token: str = Field(min_length=16, max_length=64)
    status: Literal["succeeded", "failed"]
    market: dict = Field(default_factory=dict)
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=4000)


class FastPrepareApply(BaseModel):
    agent_id: str = Field(min_length=1, max_length=255)
    workspace_id: int = Field(ge=1)
    lease_token: str = Field(min_length=16, max_length=64)


class FastApplyComplete(FastPrepareApply):
    accepted: bool = False
    verified: bool = False
    status_code: int | None = Field(default=None, ge=100, le=599)
    operation_id: str | None = Field(default=None, max_length=255)
    latency_seconds: float = Field(default=0, ge=0, le=3600)
    observed_own_price_kzt: str | None = Field(default=None, max_length=64)
    session_refreshed: bool = False
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=2000)


class FastVerifyComplete(FastPrepareApply):
    status: Literal["succeeded", "failed"] = "succeeded"
    observed_own_price_kzt: str | None = Field(default=None, max_length=64)
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=2000)


class FastPhotoComplete(BaseModel):
    agent_id: str = Field(min_length=1, max_length=255)
    workspace_id: int = Field(ge=1)
    lease_token: str = Field(min_length=16, max_length=64)
    status: Literal["succeeded", "failed"]
    image_url: str | None = Field(default=None, max_length=2048)
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=1000)
    retry_after_seconds: int = Field(default=21600, ge=60, le=86400)


router = APIRouter(
    prefix="/api/fast-dumping-agent",
    tags=["fast-dumping-agent"],
    dependencies=[Depends(require_service_token)],
)

_HEARTBEATS: dict[tuple[int, str], dict] = {}
_HEARTBEATS_LOCK = Lock()
_ONLINE_FOR = timedelta(seconds=45)
_MAX_HEARTBEATS = 32
_AGENT_GUARD_LOCK = Lock()
_MIN_CLAIM_INTERVAL_SECONDS = 2.0
_IDLE_CLAIM_INTERVAL_SECONDS = 60.0
_CLAIM_NOT_BEFORE: dict[int, float] = {}
_PHOTO_LEASE_SECONDS = 30 * 60


def _now() -> datetime:
    return datetime.now(UTC)


def _touch_agent(payload: FastAgentIdentity, *, status: str = "online") -> dict:
    record = {
        "agent_id": payload.agent_id,
        "workspace_id": payload.workspace_id,
        "hostname": payload.hostname,
        "platform": payload.platform,
        "version": payload.version,
        "concurrency": payload.concurrency,
        "merchant_uid": payload.merchant_uid,
        "status": status,
        "last_seen_at": _now(),
    }
    with _HEARTBEATS_LOCK:
        _HEARTBEATS[(payload.workspace_id, payload.agent_id)] = record
        if len(_HEARTBEATS) > _MAX_HEARTBEATS:
            oldest = sorted(
                _HEARTBEATS,
                key=lambda key: _HEARTBEATS[key]["last_seen_at"],
            )[: len(_HEARTBEATS) - _MAX_HEARTBEATS]
            for key in oldest:
                _HEARTBEATS.pop(key, None)
    return record


def _status_payload(workspace_id: int) -> dict:
    checked_at = _now()
    with _HEARTBEATS_LOCK:
        agents = [
            dict(record)
            for (record_workspace, _agent_id), record in _HEARTBEATS.items()
            if record_workspace == workspace_id
        ]
    agents.sort(key=lambda item: item["last_seen_at"], reverse=True)
    for item in agents:
        item["online"] = item["last_seen_at"] >= checked_at - _ONLINE_FOR
    return {
        "workspace_id": workspace_id,
        "online": any(item["online"] for item in agents),
        "agents": agents,
        "checked_at": checked_at,
    }


def _conflict(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


def _validate_workspace_merchant(
    db: Session,
    *,
    workspace_id: int,
    merchant_uid: str,
) -> None:
    credential = db.scalar(
        select(KaspiAccountCredential).where(
            KaspiAccountCredential.workspace_id == workspace_id
        )
    )
    if credential is None:
        raise ValueError("В выбранном workspace не настроен аккаунт Kaspi")
    if credential.partner_id.strip() != merchant_uid.strip():
        raise ValueError(
            "Merchant UID Fast Agent не совпадает с Kaspi Partner ID выбранного workspace"
        )


def _reserve_claim_slot(workspace_id: int, *, now: float | None = None) -> int:
    """Allow one bounded claim stream per workspace, including old agents."""

    checked_at = monotonic() if now is None else now
    with _AGENT_GUARD_LOCK:
        not_before = _CLAIM_NOT_BEFORE.get(workspace_id, 0.0)
        if not_before > checked_at:
            return max(1, int(not_before - checked_at + 0.999))
        _CLAIM_NOT_BEFORE[workspace_id] = checked_at + _MIN_CLAIM_INTERVAL_SECONDS
    return 0


def _defer_claims(
    workspace_id: int,
    *,
    seconds: float,
    now: float | None = None,
) -> None:
    checked_at = monotonic() if now is None else now
    with _AGENT_GUARD_LOCK:
        _CLAIM_NOT_BEFORE[workspace_id] = max(
            _CLAIM_NOT_BEFORE.get(workspace_id, 0.0),
            checked_at + max(_MIN_CLAIM_INTERVAL_SECONDS, seconds),
        )


def _claim_photo_job(
    db: Session,
    *,
    workspace_id: int,
    agent_id: str,
) -> dict | None:
    now = _now()
    product = db.scalar(
        select(Product)
        .where(
            Product.workspace_id == workspace_id,
            or_(Product.image_url.is_(None), Product.image_url == ""),
            or_(
                Product.image_backfill_after.is_(None),
                Product.image_backfill_after <= now,
            ),
        )
        # A visible CRM placeholder sets image_backfill_after, so non-null due
        # rows are served before the one-time legacy backlog.
        .order_by(
            Product.image_backfill_after.is_(None),
            Product.image_backfill_after.desc(),
            Product.id,
        )
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if product is None:
        return None
    lease_token = uuid4().hex
    product.image_backfill_after = now + timedelta(seconds=_PHOTO_LEASE_SECONDS)
    product.image_backfill_lease_token = lease_token
    product.image_backfill_agent_id = agent_id
    return {
        "product_id": product.id,
        "kaspi_product_id": product.kaspi_product_id,
        "name": product.name,
        "city_id": "196220100",
        "lease_token": lease_token,
    }


@router.post("/heartbeat")
def heartbeat(
    payload: FastAgentHeartbeat,
    db: Session = Depends(get_unscoped_db),
) -> dict:
    try:
        _validate_workspace_merchant(
            db,
            workspace_id=payload.workspace_id,
            merchant_uid=payload.merchant_uid,
        )
    except ValueError as exc:
        raise _conflict(exc) from exc
    return _touch_agent(payload, status=payload.status)


@router.get("/agents/status")
async def read_agent_status() -> dict:
    return _status_payload(current_workspace_id())


@router.post("/claim")
def claim(
    payload: FastAgentIdentity,
    db: Session = Depends(get_unscoped_db),
) -> dict:
    retry_after = _reserve_claim_slot(payload.workspace_id)
    if retry_after:
        return {
            "job": None,
            "retry_after_seconds": retry_after,
            "throttled": True,
        }
    try:
        _validate_workspace_merchant(
            db,
            workspace_id=payload.workspace_id,
            merchant_uid=payload.merchant_uid,
        )
    except ValueError as exc:
        raise _conflict(exc) from exc
    _touch_agent(payload)
    try:
        with workspace_context(payload.workspace_id):
            job = claim_job(
                db,
                workspace_id=payload.workspace_id,
                agent_id=payload.agent_id,
            )
            result = (
                None
                if job is None
                else serialize_claimed_job(
                    db,
                    job=job,
                    workspace_id=payload.workspace_id,
                )
            )
            db.commit()
    except ValueError as exc:
        db.rollback()
        raise _conflict(exc) from exc
    if result is None:
        _defer_claims(
            payload.workspace_id,
            seconds=_IDLE_CLAIM_INTERVAL_SECONDS,
        )
    return {
        "job": result,
        "retry_after_seconds": (
            int(_IDLE_CLAIM_INTERVAL_SECONDS) if result is None else 0
        ),
    }


@router.post("/photo-claim")
def claim_photo(
    payload: FastAgentIdentity,
    db: Session = Depends(get_unscoped_db),
) -> dict:
    try:
        _validate_workspace_merchant(
            db,
            workspace_id=payload.workspace_id,
            merchant_uid=payload.merchant_uid,
        )
        _touch_agent(payload)
        with workspace_context(payload.workspace_id):
            job = _claim_photo_job(
                db,
                workspace_id=payload.workspace_id,
                agent_id=payload.agent_id,
            )
            db.commit()
    except ValueError as exc:
        db.rollback()
        raise _conflict(exc) from exc
    return {
        "job": job,
        "retry_after_seconds": 60 if job is None else 0,
    }


@router.post("/photo-jobs/{product_id}/complete")
def complete_photo(
    product_id: int,
    payload: FastPhotoComplete,
    db: Session = Depends(get_unscoped_db),
) -> dict:
    try:
        with workspace_context(payload.workspace_id):
            product = db.scalar(
                select(Product)
                .where(
                    Product.id == product_id,
                    Product.workspace_id == payload.workspace_id,
                )
                .with_for_update()
            )
            if product is None:
                raise ValueError("Товар фото-задания не найден")
            if product.image_backfill_lease_token != payload.lease_token:
                raise ValueError("Lease фото-задания устарел")
            if product.image_backfill_agent_id != payload.agent_id:
                raise ValueError("Фото-задание принадлежит другому Agent")

            if payload.status == "succeeded":
                image_url = normalize_product_image_url(payload.image_url)
                if not image_url:
                    raise ValueError("Agent вернул недопустимый URL фотографии")
                product.image_url = image_url
                product.image_backfill_after = None
                product.image_backfill_error = None
                result = {"status": "saved", "image_url": image_url}
            else:
                product.image_backfill_after = _now() + timedelta(
                    seconds=payload.retry_after_seconds
                )
                product.image_backfill_error = (
                    payload.error_message or payload.error_code or "photo_failed"
                )[:1000]
                result = {
                    "status": "deferred",
                    "retry_after_seconds": payload.retry_after_seconds,
                }
            product.image_backfill_lease_token = None
            product.image_backfill_agent_id = None
            db.commit()
            return result
    except ValueError as exc:
        db.rollback()
        raise _conflict(exc) from exc


@router.post("/jobs/{job_id}/scan-complete")
def scan_complete(
    job_id: int,
    payload: FastScanComplete,
    db: Session = Depends(get_unscoped_db),
) -> dict:
    try:
        with workspace_context(payload.workspace_id):
            result = complete_scan(
                db,
                workspace_id=payload.workspace_id,
                job_id=job_id,
                agent_id=payload.agent_id,
                lease_token=payload.lease_token,
                succeeded=payload.status == "succeeded",
                market_payload=payload.market,
                error_code=payload.error_code,
                error_message=payload.error_message,
            )
            db.commit()
            return result
    except ValueError as exc:
        db.rollback()
        raise _conflict(exc) from exc


@router.post("/jobs/{job_id}/prepare-apply")
def prepare_job_apply(
    job_id: int,
    payload: FastPrepareApply,
    db: Session = Depends(get_unscoped_db),
) -> dict:
    try:
        with workspace_context(payload.workspace_id):
            result = prepare_apply(
                db,
                workspace_id=payload.workspace_id,
                job_id=job_id,
                agent_id=payload.agent_id,
                lease_token=payload.lease_token,
            )
            db.commit()
            return result
    except ValueError as exc:
        db.rollback()
        raise _conflict(exc) from exc


@router.post("/jobs/{job_id}/apply-complete")
def apply_complete(
    job_id: int,
    payload: FastApplyComplete,
    db: Session = Depends(get_unscoped_db),
) -> dict:
    try:
        with workspace_context(payload.workspace_id):
            result = complete_apply(
                db,
                workspace_id=payload.workspace_id,
                job_id=job_id,
                agent_id=payload.agent_id,
                lease_token=payload.lease_token,
                write_payload=payload.model_dump(
                    exclude={"agent_id", "workspace_id", "lease_token"}
                ),
            )
            db.commit()
            return result
    except ValueError as exc:
        db.rollback()
        raise _conflict(exc) from exc


@router.post("/jobs/{job_id}/verify-complete")
def verify_complete(
    job_id: int,
    payload: FastVerifyComplete,
    db: Session = Depends(get_unscoped_db),
) -> dict:
    try:
        with workspace_context(payload.workspace_id):
            result = complete_verification(
                db,
                workspace_id=payload.workspace_id,
                job_id=job_id,
                agent_id=payload.agent_id,
                lease_token=payload.lease_token,
                observed_own_price_kzt=payload.observed_own_price_kzt,
                verification_succeeded=payload.status == "succeeded",
                error_code=payload.error_code,
                error_message=payload.error_message,
            )
            db.commit()
            return result
    except ValueError as exc:
        db.rollback()
        raise _conflict(exc) from exc
