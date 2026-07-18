from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProductCreate(BaseModel):
    kaspi_product_id: str = Field(min_length=1, max_length=64)
    merchant_sku: str | None = Field(default=None, max_length=128)
    name: str = Field(min_length=1, max_length=500)
    brand: str | None = Field(default=None, max_length=255)


class ProductRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kaspi_product_id: str
    merchant_sku: str | None
    name: str
    brand: str | None
    status: str
    created_at: datetime
    updated_at: datetime
