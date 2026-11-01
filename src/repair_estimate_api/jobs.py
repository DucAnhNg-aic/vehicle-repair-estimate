"""Phần dùng chung giữa API demo và API đối tác: nhận file, enqueue job RQ, lỗi chuẩn."""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Callable

from fastapi import HTTPException, UploadFile, status
from redis import Redis
from rq import Queue
from rq.job import Job

from .config import ApiSettings
from .schemas import JobOptions

SUPPORTED_SUFFIXES = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".md", ".markdown"}


def get_queue(settings: ApiSettings) -> Queue:
    return Queue(settings.queue_name, connection=Redis.from_url(settings.redis_url))


def api_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def public_status(rq_status: str) -> str:
    return {
        "queued": "queued", "deferred": "queued", "scheduled": "queued",
        "started": "running", "finished": "success", "failed": "failed",
        "stopped": "failed", "canceled": "failed",
    }.get(rq_status, rq_status)


async def enqueue_upload(
    queue_factory: Callable[[], Queue], settings: ApiSettings, file: UploadFile,
    options: JobOptions, meta: dict | None = None,
) -> Job:
    """Kiểm tra file + model, ghi file vào upload_dir rồi đưa job vào RQ. Lỗi nào cũng xoá file đã ghi."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise api_error(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "UNSUPPORTED_FILE", "Định dạng file không hỗ trợ")
    ocr_model = options.ocr_model or settings.default_model
    mapping_model = options.mapping_model or settings.default_mapping_model or ocr_model
    if {ocr_model, mapping_model} - settings.allowed_models:
        raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "MODEL_NOT_ALLOWED", "Model không nằm trong allowlist")

    job_id = uuid.uuid4().hex
    upload_path = settings.upload_dir / f"{job_id}{suffix}"
    size = 0
    digest = hashlib.sha256()
    try:
        with upload_path.open("xb") as handle:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise api_error(status.HTTP_413_CONTENT_TOO_LARGE, "FILE_TOO_LARGE", "File vượt giới hạn upload")
                digest.update(chunk)
                handle.write(chunk)
        if size == 0:
            raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "EMPTY_FILE", "File rỗng")
        payload = options.model_copy(update={
            "ocr_model": ocr_model, "mapping_model": mapping_model,
            "ocr_thinking_level": options.ocr_thinking_level or settings.default_ocr_thinking_level,
            "mapping_thinking_level": options.mapping_thinking_level or settings.default_mapping_thinking_level,
        }).model_dump()
        server = {
            "env_file": str(settings.env_file),
            "output_dir": str(settings.output_dir),
            "codebook_dir": str(settings.codebook_dir),
            "input_store_dir": str(settings.input_store_dir),
        }
        return queue_factory().enqueue(
            "repair_estimate_api.tasks.process_repair_estimate_job",
            str(upload_path), payload, server,
            job_id=job_id,
            job_timeout=settings.job_timeout_seconds,
            result_ttl=settings.result_ttl_seconds,
            failure_ttl=settings.result_ttl_seconds,
            meta={"step": "queued", "file_name": file.filename, "sha256": digest.hexdigest(), **(meta or {})},
        )
    except HTTPException:
        upload_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        upload_path.unlink(missing_ok=True)
        raise api_error(status.HTTP_503_SERVICE_UNAVAILABLE, "QUEUE_UNAVAILABLE", "Không thể tạo job") from exc
    finally:
        await file.close()
