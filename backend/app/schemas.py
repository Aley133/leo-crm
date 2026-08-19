from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import ProductStatus
from .product_images import normalize_product_image_url


class ProductCreate(BaseModel):
    kaspi_product_id: str = Field(min_length=1, max_length=64)
    merchant_sku: str | None = Field(default=None, max_length=128)
    name: str = Field(min_length=1, max_length=500)
    brand: str | None = Field(default=None, max_length=255)
    image_url: str | None = Field(default=None, max_length=2048)
    status: ProductStatus = ProductStatus.DRAFT

    @field_validator("image_url")
    @classmethod
    def validate_image_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_product_image_url(value)
        if normalized is None:
            raise ValueError("Разрешена только HTTPS-ссылка на изображение Kaspi")
        return normalized


class ProductRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kaspi_product_id: str
    merchant_sku: str | None
    name: str
    brand: str | None
    image_url: str | None
    status: ProductStatus
    created_at: datetime
    updated_at: datetime
