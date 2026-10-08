"""Public request/response schemas của VRE API."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


ThinkingLevel = Literal["minimal", "low", "medium", "high"]


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
