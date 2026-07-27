from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .db import get_db
from .dumping_models import KaspiXmlFeed
from .models import Product, ProductStatus
from .order_line_product_linking import link_all_matching_order_lines_for_products
from .product_xml_import_api import (
    _merchant_id,
    _read_products,
    _sample,
    _source_filename,
)
from .workspace_auth import WorkspacePrincipal, require_workspace_principal

router = APIRouter(
    prefix="/api/workspace/product-registry/imports/xml",
    tags=["workspace-product-registry"],
)


def _existing_products(db: Session, workspace_id: int, ids: list[str]) -> dict[str, Product]:
    rows = db.scalars(
        select(Product).where(
            Product.workspace_id == workspace_id,
            Product.kaspi_product_id.in_(ids),
        )
    ).all()
    return {item.kaspi_product_id: item for item in rows}


def _store_feed_source(
    db: Session,
    *,
    workspace_id: int,
    body: bytes,
    source_filename: str | None,
    activate: bool,
) -> KaspiXmlFeed:
    xml_text = body.decode("utf-8-sig")
    feed = db.scalar(
        select(KaspiXmlFeed)
        .where(KaspiXmlFeed.workspace_id == workspace_id)
        .order_by(KaspiXmlFeed.id.desc())
        .limit(1)
    )
    if feed is None:
        feed = KaspiXmlFeed(
            workspace_id=workspace_id,
            merchant_id=_merchant_id(body),
            source_filename=source_filename,
            source_xml=xml_text,
            generated_xml=xml_text,
            active=activate,
        )
        db.add(feed)
    else:
        feed.merchant_id = _merchant_id(body) or feed.merchant_id
        feed.source_filename = source_filename or feed.source_filename
        feed.source_xml = xml_text
        if activate:
            feed.generated_xml = xml_text
            feed.active = True
    return feed


@router.post("/retain-source")
async def retain_xml_source(
    request: Request,
    principal: WorkspacePrincipal = Depends(require_workspace_principal),
    db: Session = Depends(get_db),
) -> dict:
    body, products, warnings = await _read_products(request)
    feed = _store_feed_source(
        db,
        workspace_id=principal.workspace_id,
        body=body,
        source_filename=_source_filename(request),
        activate=False,
    )
    db.commit()
    db.refresh(feed)
    return {
        "retained": True,
        "total": len(products),
        "warning_count": len(warnings),
        "source_filename": feed.source_filename,
        "merchant_id": feed.merchant_id,
    }


@router.post("/preview")
async def preview_xml_import(
    request: Request,
    principal: WorkspacePrincipal = Depends(require_workspace_principal),
    db: Session = Depends(get_db),
) -> dict:
    _body, products, warnings = await _read_products(request)
    ids = list(dict.fromkeys(item.kaspi_product_id for item in products))
    existing = _existing_products(db, principal.workspace_id, ids)
    return {
        "total": len(products),
        "new_count": sum(1 for item in products if item.kaspi_product_id not in existing),
        "existing_count": sum(1 for item in products if item.kaspi_product_id in existing),
        "warning_count": len(warnings),
        "warnings": warnings,
        "sample": _sample(products),
    }


@router.post("/commit")
async def commit_xml_import(
    request: Request,
    principal: WorkspacePrincipal = Depends(require_workspace_principal),
    db: Session = Depends(get_db),
) -> dict:
    body, products, warnings = await _read_products(request)
    ids = list(dict.fromkeys(item.kaspi_product_id for item in products))
    existing = _existing_products(db, principal.workspace_id, ids)

    created = 0
    updated = 0
    unchanged = 0
    stored_products: list[Product] = []
    for item in products:
        product = existing.get(item.kaspi_product_id)
        if product is None:
            product = Product(
                workspace_id=principal.workspace_id,
                kaspi_product_id=item.kaspi_product_id,
                merchant_sku=item.merchant_sku,
                name=item.name,
                brand=item.brand,
                status=ProductStatus.ACTIVE.value,
            )
            db.add(product)
            existing[item.kaspi_product_id] = product
            created += 1
        else:
            changed = False
            for field, value in (
                ("merchant_sku", item.merchant_sku),
                ("name", item.name),
                ("brand", item.brand),
            ):
                if value is not None and getattr(product, field) != value:
                    setattr(product, field, value)
                    changed = True
            if changed:
                updated += 1
            else:
                unchanged += 1
        stored_products.append(product)

    db.flush()
    linked_order_lines = link_all_matching_order_lines_for_products(db, products=stored_products)
    db.execute(
        update(KaspiXmlFeed)
        .where(KaspiXmlFeed.workspace_id == principal.workspace_id)
        .values(active=False)
    )
    feed = _store_feed_source(
        db,
        workspace_id=principal.workspace_id,
        body=body,
        source_filename=_source_filename(request),
        activate=True,
    )
    feed.active = True
    db.commit()
    return {
        "total": len(products),
        "created_count": created,
        "updated_count": updated,
        "unchanged_count": unchanged,
        "linked_order_lines": linked_order_lines,
        "warning_count": len(warnings),
        "warnings": warnings,
    }
