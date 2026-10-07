"""FastAPI endpoints cho VRE pipeline."""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import ValidationError
from redis import Redis
from rq import Queue

from storage import MongoSettings, RepairEstimateRepository
from .config import ApiSettings
from .schemas import JobAccepted, JobOptions, JobResult, JobStatusResponse

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


def create_app(settings: ApiSettings | None = None) -> FastAPI:
    settings = settings or ApiSettings.from_env()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="Vehicle Repair Estimate API", version="1.0.0")
    app.state.settings = settings

    @app.get("/demo", include_in_schema=False)
    async def demo():
        return FileResponse(Path(__file__).parent / "static" / "demo.html")

    @app.get("/demo/logo.png", include_in_schema=False)
    async def demo_logo():
        return FileResponse(Path(__file__).resolve().parents[1] / "aic.png", media_type="image/png")

    async def authorize(x_api_key: str | None = Header(default=None)) -> None:
        if settings.api_key and (x_api_key is None or not secrets.compare_digest(x_api_key, settings.api_key)):
            raise _error(status.HTTP_401_UNAUTHORIZED, "UNAUTHORIZED", "API key không hợp lệ")

    @app.get("/api/v1/repair-estimate/demo-config", dependencies=[Depends(authorize)])
    async def demo_config() -> dict:
        return {"models": sorted(settings.allowed_models), "default_model": settings.default_model,
                "max_upload_bytes": settings.max_upload_bytes}

    @app.get("/health/live")
    async def live() -> dict:
        return {"status": "ok"}

    @app.get("/health/ready", dependencies=[Depends(authorize)])
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
        checks["codebook"] = "ok" if all(
            (settings.codebook_dir / name).is_file()
            for name in ("service_codebook.xlsx", "part_codebook.xlsx")
        ) else "failed"
        if "failed" in checks.values():
            raise _error(status.HTTP_503_SERVICE_UNAVAILABLE, "NOT_READY", "Một hoặc nhiều dependency chưa sẵn sàng")
        return {"status": "ok", "checks": checks}

    @app.post(
        "/api/v1/repair-estimate/jobs",
        response_model=JobAccepted,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(authorize)],
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
        mapping_model = parsed.mapping_model or ocr_model
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
            payload = parsed.model_copy(update={"ocr_model": ocr_model, "mapping_model": mapping_model}).model_dump()
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
        dependencies=[Depends(authorize)],
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
        dependencies=[Depends(authorize)],
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

    return app


app = create_app()
