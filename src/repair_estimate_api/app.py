"""FastAPI endpoints cho VRE pipeline."""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import ValidationError
from pymongo.errors import PyMongoError
from redis import Redis
from rq import Queue

from storage import MongoSettings, RepairEstimateRepository
from .config import ApiSettings
from .schemas import JobAccepted, JobOptions, JobResult, JobStatusResponse, RunDetail, RunList

SUPPORTED_SUFFIXES = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".md", ".markdown"}


def get_queue(settings: ApiSettings) -> Queue:
    return Queue(settings.queue_name, connection=Redis.from_url(settings.redis_url))


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _public_status(rq_status: str) -> str:
    return {
        "queued": "queued", "deferred": "queued", "scheduled": "queued",
        "started": "running", "finished": "success", "failed": "failed",
        "stopped": "failed", "canceled": "failed",
    }.get(rq_status, rq_status)


def _utc(value: datetime) -> datetime:
    """pymongo trả datetime naive (UTC); gắn tzinfo để JSON có hậu tố Z và UI đổi múi giờ đúng."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _run_summary(row: dict) -> dict:
    metrics = row.get("metrics") or {}
    mapping, ocr, total = metrics.get("mapping") or {}, metrics.get("ocr") or {}, metrics.get("total") or {}
    document = row.get("document") or {}
    return {
        "run_id": row["run_id"], "created_at": _utc(row["created_at"]), "status": row.get("status", "unknown"),
        "file_name": (row.get("input") or {}).get("file_name"),
        "vehicle_plate": document.get("vehicle_plate"), "estimate_number": document.get("estimate_number"),
        "models": row.get("models") or {},
        "item_count": mapping.get("item_count"), "exact_count": mapping.get("exact_count"),
        "gemini_count": mapping.get("gemini_count"), "unmatched_count": mapping.get("unmatched_count"),
        "duration_seconds": total.get("duration_seconds"),
        "cost_usd": (total.get("cost") or {}).get("total_usd"),
        "cache_hit": ocr.get("cache_hit"),
    }


def create_app(settings: ApiSettings | None = None) -> FastAPI:
    settings = settings or ApiSettings.from_env()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="Vehicle Repair Estimate API", version="1.0.0")
    app.state.settings = settings

    repository_lock = threading.Lock()

    def history_repository() -> RepairEstimateRepository:
        """MongoClient an toàn đa luồng nên dùng chung một cái, tạo lười khi cần."""
        with repository_lock:
            repository = getattr(app.state, "history_repository", None)
            if repository is None:
                repository = RepairEstimateRepository(MongoSettings.from_env(settings.env_file))
                app.state.history_repository = repository
            return repository

    @app.get("/demo", include_in_schema=False)
    async def demo():
        return FileResponse(Path(__file__).parent / "static" / "demo.html")

    @app.get("/demo/logo.png", include_in_schema=False)
    async def demo_logo():
        return FileResponse(Path(__file__).parent / "static" / "logo.png", media_type="image/png")

    @app.get("/api/v1/repair-estimate/demo-config")
    async def demo_config() -> dict:
        return {"models": sorted(settings.allowed_models), "default_model": settings.default_model,
                "default_mapping_model": settings.default_mapping_model or settings.default_model,
                "thinking_levels": ["low", "medium", "high"],
                "default_ocr_thinking_level": settings.default_ocr_thinking_level,
                "default_mapping_thinking_level": settings.default_mapping_thinking_level or "low",
                "show_ai_options": settings.show_ai_options,
                "max_upload_bytes": settings.max_upload_bytes}

    @app.get("/health/live")
    async def live() -> dict:
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready() -> dict:
        checks: dict[str, str] = {}
        try:
            get_queue(settings).connection.ping()
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "failed"
        try:
            with RepairEstimateRepository(MongoSettings.from_env(settings.env_file)) as repository:
                repository.ping()
            checks["mongodb"] = "ok"
        except Exception:
            checks["mongodb"] = "failed"
        checks["codebook"] = "ok" if (settings.codebook_dir / "codebook.xlsx").is_file() else "failed"
        if "failed" in checks.values():
            raise _error(status.HTTP_503_SERVICE_UNAVAILABLE, "NOT_READY", "Một hoặc nhiều dependency chưa sẵn sàng")
        return {"status": "ok", "checks": checks}

    @app.post(
        "/api/v1/repair-estimate/jobs",
        response_model=JobAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_job(file: UploadFile = File(...), options: str = Form("{}")) -> JobAccepted:
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise _error(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "UNSUPPORTED_FILE", "Định dạng file không hỗ trợ")
        try:
            parsed = JobOptions.model_validate_json(options)
        except ValidationError as exc:
            raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "INVALID_OPTIONS", str(exc)) from exc
        ocr_model = parsed.ocr_model or settings.default_model
        mapping_model = parsed.mapping_model or settings.default_mapping_model or ocr_model
        invalid_models = {ocr_model, mapping_model} - settings.allowed_models
        if invalid_models:
            raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "MODEL_NOT_ALLOWED", "Model không nằm trong allowlist")

        job_id = uuid.uuid4().hex
        upload_path = settings.upload_dir / f"{job_id}{suffix}"
        size = 0
        digest = hashlib.sha256()
        try:
            with upload_path.open("xb") as handle:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise _error(status.HTTP_413_CONTENT_TOO_LARGE, "FILE_TOO_LARGE", "File vượt giới hạn upload")
                    digest.update(chunk)
                    handle.write(chunk)
            if size == 0:
                raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "EMPTY_FILE", "File rỗng")
            payload = parsed.model_copy(update={
                "ocr_model": ocr_model, "mapping_model": mapping_model,
                "ocr_thinking_level": parsed.ocr_thinking_level or settings.default_ocr_thinking_level,
                "mapping_thinking_level": parsed.mapping_thinking_level or settings.default_mapping_thinking_level,
            }).model_dump()
            server = {
                "env_file": str(settings.env_file),
                "output_dir": str(settings.output_dir),
                "codebook_dir": str(settings.codebook_dir),
            }
            job = get_queue(settings).enqueue(
                "repair_estimate_api.tasks.process_repair_estimate_job",
                str(upload_path), payload, server,
                job_id=job_id,
                job_timeout=settings.job_timeout_seconds,
                result_ttl=settings.result_ttl_seconds,
                failure_ttl=settings.result_ttl_seconds,
                meta={"step": "queued", "file_name": file.filename, "sha256": digest.hexdigest()},
            )
        except HTTPException:
            upload_path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            upload_path.unlink(missing_ok=True)
            raise _error(status.HTTP_503_SERVICE_UNAVAILABLE, "QUEUE_UNAVAILABLE", "Không thể tạo job") from exc
        finally:
            await file.close()
        return JobAccepted(
            job_id=job.id,
            status="queued",
            status_url=f"/api/v1/repair-estimate/jobs/{job.id}",
            result_url=f"/api/v1/repair-estimate/jobs/{job.id}/result",
        )

    def fetch_job(job_id: str):
        job = get_queue(settings).fetch_job(job_id)
        if job is None:
            raise _error(status.HTTP_404_NOT_FOUND, "JOB_NOT_FOUND", "Không tìm thấy job")
        return job

    @app.get(
        "/api/v1/repair-estimate/jobs/{job_id}",
        response_model=JobStatusResponse,
    )
    async def get_job(job_id: str) -> dict:
        job = fetch_job(job_id)
        rq_status = job.get_status(refresh=True)
        response = {
            "job_id": job.id,
            "status": _public_status(rq_status),
            "step": job.meta.get("step"),
            "created_at": job.created_at,
            "started_at": job.started_at,
            "ended_at": job.ended_at,
        }
        if rq_status == "failed":
            response["error"] = {"code": "PROCESSING_FAILED", "message": "Không xử lý được báo giá"}
        return response

    @app.get(
        "/api/v1/repair-estimate/jobs/{job_id}/result",
        response_model=JobResult,
    )
    async def get_result(job_id: str) -> dict:
        job = fetch_job(job_id)
        rq_status = job.get_status(refresh=True)
        if rq_status == "failed":
            raise _error(status.HTTP_500_INTERNAL_SERVER_ERROR, "PROCESSING_FAILED", "Không xử lý được báo giá")
        if rq_status != "finished":
            raise _error(status.HTTP_409_CONFLICT, "JOB_NOT_COMPLETED", "Job chưa xử lý xong")
        if not isinstance(job.result, dict):
            raise _error(status.HTTP_500_INTERNAL_SERVER_ERROR, "RESULT_UNAVAILABLE", "Kết quả job không còn khả dụng")
        return job.result

    @app.get("/api/v1/repair-estimate/runs", response_model=RunList)
    def list_runs(limit: int = Query(20, ge=1, le=100), before: datetime | None = None,
                  q: str | None = Query(None, max_length=100)) -> dict:
        try:
            rows = history_repository().list_history(limit=limit, before=before, search=q)
        except PyMongoError as exc:
            raise _error(status.HTTP_503_SERVICE_UNAVAILABLE, "HISTORY_UNAVAILABLE", "Không đọc được lịch sử") from exc
        runs = [_run_summary(row) for row in rows]
        return {"runs": runs, "next_before": runs[-1]["created_at"] if len(runs) == limit else None}

    @app.get("/api/v1/repair-estimate/runs/{run_id}", response_model=RunDetail)
    def get_run(run_id: str) -> dict:
        try:
            row = history_repository().get_run(run_id)
        except PyMongoError as exc:
            raise _error(status.HTTP_503_SERVICE_UNAVAILABLE, "HISTORY_UNAVAILABLE", "Không đọc được lịch sử") from exc
        if row is None:
            raise _error(status.HTTP_404_NOT_FOUND, "RUN_NOT_FOUND", "Không tìm thấy lần chạy")
        return {**row, "created_at": _utc(row["created_at"])}

    return app


app = create_app()
