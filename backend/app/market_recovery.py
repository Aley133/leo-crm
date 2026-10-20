"""Daily Kaspi market recovery watch, executed only by Product Test Agent."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html import escape
import re
from uuid import uuid4
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from .workspace_models import Workspace

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, Index, Integer, Numeric, String, Text, UniqueConstraint, func, or_, select
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .auth import require_service_token
from .db import Base, get_db, get_unscoped_db
from .models import OutboxEvent, Product
from .product_test_models import ProductTestItem
from .product_test_api import ProductTestAgentIdentity, _product_test_agent_status
from .fast_dumping_agent_api import _validate_workspace_merchant
from .workspace_context import WorkspaceOwned, current_workspace_id, workspace_context

EVENT_TYPE = "kaspi.market_recovery"
DAY = timedelta(days=1)


def now():
    return datetime.now(UTC)


def aware(value):
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


class MarketRecoverySettings(WorkspaceOwned, Base):
    __tablename__ = "market_recovery_settings"
    __table_args__ = (UniqueConstraint("workspace_id", name="uq_market_recovery_settings_workspace"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    rise_percent: Mapped[int] = mapped_column(Integer, default=50, server_default="50")
    city_id: Mapped[str] = mapped_column(String(32), default="196220100", server_default="196220100")
    zone_id: Mapped[str] = mapped_column(String(64), default="Magnum_ZONE1", server_default="Magnum_ZONE1")
    catalog_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MarketRecoveryCard(WorkspaceOwned, Base):
    __tablename__ = "market_recovery_cards"
    __table_args__ = (
        UniqueConstraint("workspace_id", "kaspi_product_id", name="uq_market_recovery_card"),
        Index("ix_market_recovery_due", "workspace_id", "next_check_at", "lease_until"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kaspi_product_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(500))
    kaspi_url: Mapped[str] = mapped_column(Text)
    baseline_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    seller_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sellers_complete: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    notified_band: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, server_default=func.now())
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    completed_token: Mapped[str | None] = mapped_column(String(64), nullable=True)


router = APIRouter(prefix="/api/monitoring-center/market-recovery", tags=["market-recovery"], dependencies=[Depends(require_service_token)])
agent_router = APIRouter(prefix="/api/product-test-agent/market-recovery", tags=["product-test-agent"], dependencies=[Depends(require_service_token)])


def settings_for(db, workspace_id):
    settings = db.scalar(select(MarketRecoverySettings).where(MarketRecoverySettings.workspace_id == workspace_id).with_for_update())
    if settings is None:
        insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
        db.execute(insert(MarketRecoverySettings).values(workspace_id=workspace_id).on_conflict_do_nothing(index_elements=["workspace_id"]))
        settings = db.scalar(select(MarketRecoverySettings).where(MarketRecoverySettings.workspace_id == workspace_id).with_for_update())
    return settings


def sync_catalog(db, settings, *, force=False):
    timestamp = now()
    if not force and settings.catalog_synced_at and timestamp - aware(settings.catalog_synced_at) < DAY:
        return
    workspace = settings.workspace_id
    # No sale/status/stock filters: disabled, archived and sleeping cards belong here too.
    sources = list(db.execute(select(Product.kaspi_product_id, Product.name).where(Product.workspace_id == workspace)))
    sources += list(db.execute(select(ProductTestItem.kaspi_product_id, ProductTestItem.name).where(
        ProductTestItem.workspace_id == workspace,
        or_(ProductTestItem.product_id.is_not(None), ProductTestItem.added_at.is_not(None)))))
    cards = {row.kaspi_product_id: row for row in db.scalars(select(MarketRecoveryCard).where(MarketRecoveryCard.workspace_id == workspace))}
    for raw_id, name in sources:
        master_id = str(raw_id or "").split("_", 1)[0].strip()
        if not re.fullmatch(r"\d{6,18}", master_id):
            continue
        if master_id in cards:
            continue
        card = MarketRecoveryCard(workspace_id=workspace, kaspi_product_id=master_id,
            name=str(name or master_id)[:500], kaspi_url=f"https://kaspi.kz/shop/p/-{master_id}/", next_check_at=timestamp)
        cards[master_id] = card
        db.add(card)
    settings.catalog_synced_at = timestamp
    db.flush()


class SettingsUpdate(BaseModel):
    enabled: bool
    rise_percent: int = Field(default=50, ge=0, le=1000)


@router.patch("/settings")
def update_settings(payload: SettingsUpdate, db: Session = Depends(get_db)):
    settings = settings_for(db, current_workspace_id())
    # Keep the underlying price floor. Changing a threshold takes effect on future checks.
    settings.enabled = payload.enabled
    if settings.rise_percent != payload.rise_percent:
        for card in db.scalars(select(MarketRecoveryCard).where(MarketRecoveryCard.workspace_id == settings.workspace_id)):
            rise = max(Decimal(0), (card.current_price - card.baseline_price) * 100 / card.baseline_price) if card.current_price and card.baseline_price else Decimal(0)
            card.notified_band = int(rise // payload.rise_percent) if payload.rise_percent else 0
    settings.rise_percent = payload.rise_percent
    db.commit()
    return {"saved": True}


@router.post("/sync")
def sync_assortment(db: Session = Depends(get_db)):
    settings = settings_for(db, current_workspace_id())
    sync_catalog(db, settings, force=True)
    db.commit()
    return {"synced": True}


@router.get("")
def read_state(limit: int = 100, offset: int = 0, db: Session = Depends(get_db)):
    workspace = current_workspace_id()
    settings = settings_for(db, workspace)
    sync_catalog(db, settings)
    cards = select(MarketRecoveryCard).where(MarketRecoveryCard.workspace_id == workspace)
    totals = db.execute(select(func.count(), func.count(MarketRecoveryCard.last_checked_at),
        func.count().filter(MarketRecoveryCard.last_error.is_not(None))).where(MarketRecoveryCard.workspace_id == workspace)).one()
    rows = db.scalars(cards.order_by(MarketRecoveryCard.last_error.is_(None), MarketRecoveryCard.id).limit(max(1, min(200, limit))).offset(max(0, offset))).all()
    events = db.scalars(select(OutboxEvent).where(OutboxEvent.workspace_id == workspace,
        OutboxEvent.event_type == EVENT_TYPE).order_by(OutboxEvent.created_at.desc()).limit(30)).all()
    result = {"enabled": settings.enabled, "rise_percent": settings.rise_percent,
        "total": totals[0], "checked": totals[1], "errors": totals[2],
        "catalog_synced_at": settings.catalog_synced_at,
        "cards": [{"id": c.id, "name": c.name, "kaspi_product_id": c.kaspi_product_id, "kaspi_url": c.kaspi_url,
            "baseline_price": c.baseline_price, "current_price": c.current_price, "seller_count": c.seller_count,
            "sellers_complete": c.sellers_complete, "last_checked_at": c.last_checked_at,
            "next_check_at": c.next_check_at, "last_error": c.last_error} for c in rows],
        "signals": [{**e.payload_json, "sent": e.published_at is not None and not e.last_error,
            "delivery_error": e.last_error, "created_at": e.created_at} for e in events],
        "agent": _product_test_agent_status(workspace)}
    db.commit()
    return result


def validate_agent(payload, db):
    if payload.agent_kind != "product_test":
        raise HTTPException(409, "Нужен LEO Product Test Agent")
    try:
        version = tuple(int(x) for x in (payload.version or "").split("."))
    except ValueError:
        version = ()
    if version < (1, 1, 27):
        raise HTTPException(409, "Обновите Product Test Agent до 1.1.27")
    try:
        _validate_workspace_merchant(db, workspace_id=payload.workspace_id, merchant_uid=payload.merchant_uid)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@agent_router.post("/claim")
def claim_card(payload: ProductTestAgentIdentity, db: Session = Depends(get_unscoped_db)):
    validate_agent(payload, db)
    with workspace_context(payload.workspace_id):
        settings = settings_for(db, payload.workspace_id)
        sync_catalog(db, settings)
        timestamp = now()
        card = None
        if settings.enabled:
            card = db.scalar(select(MarketRecoveryCard).where(MarketRecoveryCard.workspace_id == payload.workspace_id,
                MarketRecoveryCard.next_check_at <= timestamp,
                or_(MarketRecoveryCard.lease_until.is_(None), MarketRecoveryCard.lease_until <= timestamp))
                .order_by(MarketRecoveryCard.next_check_at, MarketRecoveryCard.id).limit(1).with_for_update(skip_locked=True))
        if card is None:
            db.commit()
            return {"card": None, "retry_after_seconds": 60}
        card.lease_token, card.agent_id = uuid4().hex, payload.agent_id
        card.lease_until = timestamp + timedelta(minutes=10)
        result = {"id": card.id, "reference": card.kaspi_product_id, "lease_token": card.lease_token,
            "city_id": settings.city_id, "zone_id": settings.zone_id}
        db.commit()
        return {"card": result, "retry_after_seconds": 2}


class MarketResult(ProductTestAgentIdentity):
    lease_token: str = Field(min_length=16, max_length=64)
    status: str = Field(pattern="^(succeeded|failed)$")
    price: Decimal | None = Field(default=None, gt=0, le=1000000000, max_digits=18, decimal_places=2)
    seller_count: int | None = Field(default=None, ge=0, le=100000)
    sellers_complete: bool = True
    empty_confirmed: bool = False
    error: str | None = Field(default=None, max_length=1000)
    kaspi_url: str | None = Field(default=None, max_length=4000)


def apply_observation(db, card, settings, payload, timestamp):
    if payload.seller_count is None:
        raise ValueError("Kaspi не подтвердил число продавцов")
    if payload.seller_count == 0 and (not payload.empty_confirmed or not payload.sellers_complete):
        raise ValueError("Отсутствие продавцов не подтверждено двумя проверками")
    if payload.seller_count > 0 and payload.price is None:
        raise ValueError("Не подтверждена минимальная цена продавцов")
    previous_price, previous_count = card.current_price, card.seller_count
    had_baseline = card.baseline_price is not None
    if payload.kaspi_url:
        url = urlparse(payload.kaspi_url)
        if url.scheme == "https" and url.hostname == "kaspi.kz" and re.search(rf"-{re.escape(card.kaspi_product_id)}/?$", url.path):
            card.kaspi_url = payload.kaspi_url
        else:
            raise ValueError("Kaspi вернул ссылку на другую карточку")
    reasons = []
    rise = Decimal(0)
    if payload.seller_count == 0:
        if previous_count != 0:
            reasons.append("no_sellers")
    else:
        price = payload.price
        if card.baseline_price is None or price < card.baseline_price:
            card.baseline_price, card.notified_band = price, 0
        rise = (price - card.baseline_price) * 100 / card.baseline_price
        threshold = settings.rise_percent
        band = int(rise // threshold) if threshold else 0
        if had_baseline and price > (previous_price or card.baseline_price) and (
            (threshold == 0) or (band > card.notified_band and band > 0)):
            reasons.append("price_rise")
            card.notified_band = band
    card.current_price = payload.price if payload.seller_count else None
    card.seller_count, card.sellers_complete = payload.seller_count, payload.sellers_complete
    card.last_checked_at, card.last_error = timestamp, None
    if reasons:
        workspace_name = db.scalar(select(Workspace.name).where(Workspace.id == card.workspace_id))
        db.add(OutboxEvent(workspace_id=card.workspace_id, aggregate_type="market_recovery", aggregate_id=str(card.id),
            event_type=EVENT_TYPE, idempotency_key=f"market-recovery:{card.id}:{payload.lease_token}",
            payload_json={"card_id": card.id, "workspace_id": card.workspace_id, "workspace_name": workspace_name or str(card.workspace_id), "product_name": card.name,
                "kaspi_product_id": card.kaspi_product_id, "kaspi_url": card.kaspi_url,
                "reasons": reasons, "previous_price": str(previous_price) if previous_price is not None else None,
                "baseline_price": str(card.baseline_price) if card.baseline_price is not None else None,
                "current_price": str(card.current_price) if card.current_price is not None else None,
                "rise_percent": str(rise.quantize(Decimal("0.1"))), "previous_sellers": previous_count,
                "seller_count": card.seller_count, "sellers_complete": card.sellers_complete, "checked_at": timestamp.isoformat(),
                "city_id": settings.city_id}))


@agent_router.post("/{card_id}/complete")
def complete_card(card_id: int, payload: MarketResult, db: Session = Depends(get_unscoped_db)):
    validate_agent(payload, db)
    with workspace_context(payload.workspace_id):
        settings = settings_for(db, payload.workspace_id)
        card = db.scalar(select(MarketRecoveryCard).where(MarketRecoveryCard.workspace_id == payload.workspace_id,
            MarketRecoveryCard.id == card_id).with_for_update())
        if card is None:
            raise HTTPException(404, "Карточка не найдена")
        if card.completed_token == payload.lease_token and card.agent_id == payload.agent_id:
            db.commit()
            return {"accepted": True}
        timestamp = now()
        if card.lease_token != payload.lease_token or card.agent_id != payload.agent_id or card.lease_until is None or aware(card.lease_until) < timestamp:
            raise HTTPException(409, "Проверку уже получил другой агент")
        error = payload.error or "Проверка Kaspi не завершилась"
        if payload.status == "succeeded" and settings.enabled:
            try:
                apply_observation(db, card, settings, payload, timestamp)
                error = None
            except ValueError as exc:
                error = str(exc)
        if error:
            card.last_error = error[:1000]
        card.next_check_at = timestamp + (timedelta(hours=1) if error else DAY)
        card.completed_token = card.lease_token
        card.lease_token, card.lease_until = None, None
        db.commit()
        return {"accepted": True, "error": error}


def market_event_current(db, payload):
    card = db.scalar(select(MarketRecoveryCard).where(MarketRecoveryCard.id == payload.get("card_id"),
        MarketRecoveryCard.workspace_id == payload.get("workspace_id")))
    settings = db.scalar(select(MarketRecoverySettings).where(MarketRecoverySettings.workspace_id == payload.get("workspace_id")))
    if card is None or settings is None or not settings.enabled or not card.last_checked_at or card.last_error:
        return False
    # Do not deliver stale opportunities after a long Telegram outage or a newer observation.
    return (now() - aware(card.last_checked_at) < timedelta(hours=48)
        and card.seller_count == payload.get("seller_count")
        and (card.current_price == Decimal(payload["current_price"]) if payload.get("current_price") is not None else card.current_price is None))


def format_market_message(payload):
    title = "🟢 Карточка Kaspi без продавцов" if "no_sellers" in payload["reasons"] else "📈 Восстановление цены Kaspi"
    lines = [f"<b>{title}</b>", escape(payload["product_name"]),
        f"Магазин: {escape(str(payload.get('workspace_name') or payload['workspace_id']))} · Kaspi ID: {payload['kaspi_product_id']}"]
    if "price_rise" in payload["reasons"]:
        lines += [f"Минимум за наблюдение: {payload['baseline_price']} ₸",
            f"Предыдущая цена: {payload.get('previous_price') or payload['baseline_price']} ₸",
            f"Сейчас: <b>{payload['current_price']} ₸ (+{payload['rise_percent']}%)</b>"]
    checked_at = datetime.fromisoformat(payload["checked_at"]).astimezone(ZoneInfo("Asia/Qyzylorda"))
    lines += [f"Проверено: {checked_at:%d.%m.%Y %H:%M} (Казахстан)", f"Продавцов: {payload.get('previous_sellers') if payload.get('previous_sellers') is not None else '—'} → {payload['seller_count']}",
        f"Город Kaspi: {payload['city_id']}", "Можно проверить закупку и возможность вернуться в продажу.",
        f'<a href="{escape(payload["kaspi_url"], quote=True)}">Открыть карточку Kaspi</a>']
    return "\n".join(lines)
