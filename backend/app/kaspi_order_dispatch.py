from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from .browser_agent_job_contract import encode_kaspi_seller_order_job
from .browser_agent_models import BrowserAgentJob, BrowserAgentJobStatus
from .kaspi_seller.snapshot_models import KaspiSellerOrderSnapshotRecord
from .lease_engine import utc_now
from .models import MarketplaceAccount, MarketplaceOrder


@dataclass(frozen=True, slots=True)
class KaspiOrderDispatchResult:
    queued_job_ids: tuple[int, ...]

    @property
    def queued_count(self) -> int:
        return len(self.queued_job_ids)


_TERMINAL_ORDER_STATUSES = {"delivered", "cancelled", "canceled", "returned"}
_ACTIVE_JOB_STATUSES = {
    BrowserAgentJobStatus.QUEUED.value,
    BrowserAgentJobStatus.LEASED.value,
}


def dispatch_stale_kaspi_orders(
    session: Session,
    *,
    limit: int = 100,
    refresh_seconds: int = 180,
) -> KaspiOrderDispatchResult:
    """Queue Browser Agent jobs for active Kaspi orders with stale snapshots.

    Marketplace Orders API is the discovery source. Kaspi Seller Snapshot is the
    operational-state source. The dispatcher joins those two streams without
    making the UI responsible for refreshing data.
    """

    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    if refresh_seconds < 30 or refresh_seconds > 86400:
        raise ValueError("refresh_seconds must be between 30 and 86400")

    stale_before = utc_now() - timedelta(seconds=refresh_seconds)

    latest_snapshot_at = (
        select(func.max(KaspiSellerOrderSnapshotRecord.observed_at))
        .where(
            KaspiSellerOrderSnapshotRecord.merchant_id == MarketplaceAccount.external_account_id,
            KaspiSellerOrderSnapshotRecord.order_code == MarketplaceOrder.external_code,
        )
        .correlate(MarketplaceOrder, MarketplaceAccount)
        .scalar_subquery()
    )

    rows = session.execute(
        select(
            MarketplaceOrder.id,
            MarketplaceOrder.external_code,
            MarketplaceAccount.external_account_id,
            latest_snapshot_at.label("latest_snapshot_at"),
        )
        .join(MarketplaceAccount, MarketplaceAccount.id == MarketplaceOrder.marketplace_account_id)
        .where(
            MarketplaceAccount.provider == "kaspi",
            MarketplaceOrder.external_code.is_not(None),
            func.lower(MarketplaceOrder.status).not_in(_TERMINAL_ORDER_STATUSES),
            or_(latest_snapshot_at.is_(None), latest_snapshot_at <= stale_before),
        )
        .order_by(latest_snapshot_at.asc().nullsfirst(), MarketplaceOrder.ordered_at.desc().nullslast())
        .limit(limit)
    ).all()

    queued: list[int] = []
    for _order_id, order_code, merchant_id, _latest_at in rows:
        if not order_code or not merchant_id:
            continue

        encoded_url = encode_kaspi_seller_order_job(
            merchant_id=str(merchant_id),
            order_code=str(order_code),
        )
        pending = session.scalar(
            select(BrowserAgentJob.id)
            .where(
                BrowserAgentJob.url == encoded_url,
                BrowserAgentJob.status.in_(_ACTIVE_JOB_STATUSES),
            )
            .order_by(BrowserAgentJob.id)
            .limit(1)
        )
        if pending is not None:
            continue

        job = BrowserAgentJob(
            monitor_target_id=None,
            supplier_product_id=0,
            url=encoded_url,
            status=BrowserAgentJobStatus.QUEUED.value,
        )
        session.add(job)
        session.flush()
        queued.append(job.id)

    return KaspiOrderDispatchResult(tuple(queued))
