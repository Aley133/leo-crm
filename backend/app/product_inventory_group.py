from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .models import Product


def inventory_owner_product(
    db: Session,
    product_or_id: Product | int,
) -> Product:
    """Resolve the single product that owns physical batches for a listing.

    New writes keep ownership one level deep, but the bounded walk makes reads
    fail safely if a future manual database edit introduces a chain or cycle.
    """

    product = (
        product_or_id
        if isinstance(product_or_id, Product)
        else db.get(Product, int(product_or_id))
    )
    if product is None:
        raise ValueError("Product not found")

    visited: set[int] = set()
    current = product
    while current.inventory_owner_product_id is not None:
        current_id = int(current.id)
        if current_id in visited:
            raise ValueError("Inventory ownership cycle detected")
        visited.add(current_id)
        owner = db.get(Product, int(current.inventory_owner_product_id))
        if owner is None or int(owner.workspace_id) != int(product.workspace_id):
            raise ValueError("Inventory owner belongs to another workspace")
        current = owner
    return current


def inventory_owner_product_id(
    db: Session,
    product_or_id: Product | int,
) -> int:
    return int(inventory_owner_product(db, product_or_id).id)


def inventory_group_products(
    db: Session,
    product_or_id: Product | int,
) -> tuple[Product, ...]:
    owner = inventory_owner_product(db, product_or_id)
    members = db.scalars(
        select(Product)
        .where(
            Product.workspace_id == owner.workspace_id,
            or_(
                Product.id == owner.id,
                Product.inventory_owner_product_id == owner.id,
            ),
        )
        .order_by(Product.id)
    ).all()
    return tuple(members)


def inventory_group_product_ids(
    db: Session,
    product_or_id: Product | int,
) -> tuple[int, ...]:
    return tuple(int(product.id) for product in inventory_group_products(db, product_or_id))


def inventory_owner_ids_for_products(
    db: Session,
    product_ids: set[int],
) -> dict[int, int]:
    if not product_ids:
        return {}
    products = db.scalars(select(Product).where(Product.id.in_(product_ids))).all()
    return {
        int(product.id): inventory_owner_product_id(db, product)
        for product in products
    }
