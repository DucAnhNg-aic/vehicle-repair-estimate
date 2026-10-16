"""Public request/response schemas của VRE API."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


ThinkingLevel = Literal["low", "medium", "high"]


class JobOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ocr_model: str | None = None
    mapping_model: str | None = None
    ocr_thinking_level: ThinkingLevel | None = None  # None = auto: theo cấu hình server
    mapping_thinking_level: ThinkingLevel | None = None
    use_cache: bool = True
    save_mongodb: bool = True
    timeout_seconds: float = Field(default=180, ge=10, le=600)


class JobAccepted(BaseModel):
    job_id: str
    status: str
    status_url: str
    result_url: str


class ApiError(BaseModel):
    code: str
    message: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: Literal["queued", "running", "success", "failed"]
    step: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    error: ApiError | None = None


class StepMetrics(BaseModel):
    duration_seconds: float | None
    cost: dict[str, Any] | None


class OcrMetrics(StepMetrics):
    cache_hit: bool


class MappingMetrics(StepMetrics):
    item_count: int
    exact_count: int
    gemini_count: int
    unmatched_count: int
    cache_hits: int
    cache_misses: int
    gemini_calls: int
    context_cache: list[dict] = Field(default_factory=list)


class JobMetrics(BaseModel):
    total: StepMetrics
    ocr: OcrMetrics
    mapping: MappingMetrics


class StorageResult(BaseModel):
    saved: bool
    run_id: str


class JobResult(BaseModel):
    job_id: str
    status: Literal["success"]
    document: dict[str, Any]
    items: list[dict[str, Any]]
    metrics: JobMetrics
    storage: StorageResult


class RunSummary(BaseModel):
    run_id: str
    created_at: datetime  # luôn UTC (có tzinfo); UI tự đổi sang giờ Việt Nam
    status: str
    file_name: str | None = None
    vehicle_plate: str | None = None
    estimate_number: str | None = None
    models: dict[str, Any] = Field(default_factory=dict)
    item_count: int | None = None
    exact_count: int | None = None
    gemini_count: int | None = None
    unmatched_count: int | None = None
    duration_seconds: float | None = None
    cost_usd: float | None = None
    cache_hit: bool | None = None
    channel: str | None = None  # demo | partner; None = chạy CLI hoặc bản ghi cũ
    client: str | None = None
    reference_id: str | None = None


class RunList(BaseModel):
    runs: list[RunSummary]
    next_before: datetime | None = None  # truyền lại làm ``before`` để lấy trang kế


class RunDetail(BaseModel):
    run_id: str
    created_at: datetime
    status: str
    models: dict[str, Any] = Field(default_factory=dict)
    input: dict[str, Any] = Field(default_factory=dict)  # file_name gốc + sha256 nội dung file
    source: dict[str, Any] | None = None
    document: dict[str, Any]
    items: list[dict[str, Any]]
    metrics: dict[str, Any]
    error: Any = None


# --- API đối tác (/partner/v1) -------------------------------------------------------------------
# Hợp đồng ổn định, tách khỏi schema OCR nội bộ: field thừa (chi phí, model, row codebook...) bị
# response_model lọc bỏ, nên đổi pipeline không làm lộ dữ liệu nội bộ hay vỡ tích hợp phía khách.

class PartnerError(BaseModel):
    code: str = Field(examples=["PROCESSING_FAILED"])
    message: str = Field(examples=["Không xử lý được báo giá"])


class PartnerErrorResponse(BaseModel):
    detail: PartnerError


class CodebookMatch(BaseModel):
    code: str = Field(description="Mã hạng mục chuẩn AICycle.", examples=["AIC.000079"])
    name: str = Field(description="Tên hạng mục chuẩn.", examples=["Cánh cửa mộc trước bên trái"])
    item_type: str | None = Field(None, description="Nhóm hạng mục trong codebook, ví dụ `part`.")


class ItemMapping(BaseModel):
    method: Literal["exact", "ai", "unmatched"] = Field(
        description="`exact`: khớp chính xác mã/tên; `ai`: AI đối chiếu; `unmatched`: chưa tìm được mã chuẩn.")
    matches: list[CodebookMatch] = Field(description="Các hạng mục chuẩn khớp, rỗng nếu `unmatched`.")

    @field_validator("method", mode="before")
    @classmethod
    def hide_provider(cls, value: str) -> str:
        return "ai" if value == "gemini" else value


class EstimateDocument(BaseModel):
    estimate_number: str | None = Field(None, description="Số báo giá, nguyên văn.", examples=["1889"])
    date: str | None = Field(None, description="Ngày báo giá, giữ nguyên văn như trên tài liệu.",
                             examples=["28 tháng 09 năm 2026"])
    vehicle_plate: str | None = Field(None, description="Biển số xe, nguyên văn.", examples=["50E26280"])
    vehicle_model: str | None = Field(None, description="Hiệu/loại xe, nguyên văn.")
    vin: str | None = Field(None, description="Số khung (VIN), nguyên văn.")


class EstimateItem(BaseModel):
    """Một dòng hạng mục trên báo giá. Số tiền là VND; tỷ lệ là phần trăm (8% → 8). Null = không có trên tài liệu."""

    index: int = Field(description="Thứ tự dòng trên báo giá, bắt đầu từ 0.")
    raw_section: str | None = Field(None, description="Tên nhóm/section nguyên văn.")
    section_type: str = Field(description="`service` | `paint_service` | `material` | `unknown`.")
    raw_code: str | None = Field(None, description="Mã dòng nguyên văn (nếu có).")
    raw_name: str = Field(description="Tên hạng mục nguyên văn.", examples=["Sơn cửa trước Trái"])
    operation: str | None = Field(None, description=(
        "Thao tác chính: `paint` | `repair` | `bodywork` | `remove_install` | `replace` | `weld` | "
        "`reshape` | `polish` | `clean` | `other`."))
    vehicle_part: str | None = Field(None, description="Bộ phận xe nhắc tới trong tên.", examples=["cửa trước trái"])
    item_name: str = Field(description="Tên hạng mục đã chuẩn hoá nhẹ.")
    category: str = Field(description="`material` (phụ tùng/vật tư) | `labor` (công) | `paint` (sơn).")
    unit: str | None = None
    quantity: float | None = None
    unit_price: float | None = None
    discount_rate: float | None = None
    discount_amount: float | None = None
    tax_rate: float | None = None
    tax_amount: float | None = None
    amount: float | None = Field(None, description="Thành tiền như ghi trên tài liệu (không tự tính).")
    material_amount: float | None = None
    labor_amount: float | None = None
    paint_amount: float | None = None
    material_discount_rate: float | None = None
    labor_discount_rate: float | None = None
    paint_discount_rate: float | None = None
    material_tax_rate: float | None = None
    labor_tax_rate: float | None = None
    paint_tax_rate: float | None = None
    source_evidence: str | None = Field(None, description="Đoạn trích nguyên văn làm căn cứ cho dòng.")
    mapping: ItemMapping


class EstimateResult(BaseModel):
    document: EstimateDocument
    items: list[EstimateItem]


class Estimate(BaseModel):
    job_id: str = Field(description="Mã job; dùng để hỏi lại trạng thái qua `GET /v1/estimates/{job_id}`.")
    reference_id: str | None = Field(None, description="Mã tham chiếu phía khách gửi lên, trả lại nguyên văn.")
    status: Literal["queued", "running", "success", "failed"]
    created_at: datetime | None = None
    finished_at: datetime | None = None
    status_url: str = Field(description="Đường dẫn để hỏi lại trạng thái/kết quả.")
    result: EstimateResult | None = Field(None, description="Chỉ có khi `status = success`.")
    error: PartnerError | None = Field(None, description="Chỉ có khi `status = failed`.")
