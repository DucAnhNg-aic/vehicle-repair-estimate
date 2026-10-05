"""Output contract from prompt.txt; null values remain null."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class Document(StrictModel):
    quotation_number: str | None
    date: str | None
    vehicle_plate: str | None
    vehicle_model: str | None
    vin: str | None


class QuotationItem(StrictModel):
    raw_section: str | None
    section_type: Literal["service", "paint_service", "material", "unknown"]
    raw_code: str | None
    raw_name: str
    operation: Literal[
        "paint", "repair", "bodywork", "remove_install", "replace",
        "weld", "reshape", "polish", "clean", "other",
    ] | None
    vehicle_part: str | None
    item_name: str
    category: Literal["material", "labor", "paint"]
    unit: str | None
    quantity: float | None
    unit_price: float | None
    discount_rate: float | None
    discount_amount: float | None
    tax_rate: float | None
    tax_amount: float | None
    amount: float | None
    material_amount: float | None
    labor_amount: float | None
    paint_amount: float | None
    material_discount_rate: float | None
    labor_discount_rate: float | None
    paint_discount_rate: float | None
    material_tax_rate: float | None
    labor_tax_rate: float | None
    paint_tax_rate: float | None
    source_evidence: str
    confidence: Literal["high", "medium", "low"]


class Quotation(StrictModel):
    document: Document
    items: list[QuotationItem]
