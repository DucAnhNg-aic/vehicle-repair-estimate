"""API cho đối tác (mount tại /partner): một lần gọi gửi báo giá và nhận kết quả, xác thực bằng X-API-Key.

Job vẫn chạy qua hàng đợi RQ như demo; endpoint chỉ giữ request chờ tối đa ``wait_seconds``.
Quá hạn thì trả 202 kèm ``job_id`` để khách hỏi lại, job không bị huỷ.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from typing import Callable

from fastapi import Depends, FastAPI, File, Form, Request, Response, Security, UploadFile, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from rq import Queue
from rq.job import Job

from .config import ApiSettings
from .jobs import SUPPORTED_SUFFIXES, api_error, enqueue_upload, public_status
from .schemas import Estimate, JobOptions, PartnerErrorResponse

POLL_INTERVAL_SECONDS = 1.0
FAILED_ERROR = {"code": "PROCESSING_FAILED", "message": "Không xử lý được báo giá"}

_api_key_header = APIKeyHeader(
    name="X-API-Key", auto_error=False, description="Key do AICycle cấp riêng cho từng khách hàng.")


def _description(settings: ApiSettings) -> str:
    max_mb = settings.max_upload_bytes // (1024 * 1024)
    return f"""
Trích xuất và chuẩn hoá **báo giá sửa chữa xe** (ảnh hoặc PDF): đọc thông tin chung, từng dòng hạng mục,
số tiền, và đối chiếu mỗi dòng với mã hạng mục chuẩn AICycle.

## Xác thực
Mọi request gửi header `X-API-Key: <key>` (bấm **Authorize** để thử trên trang này).

## Luồng gọi
1. `POST /v1/estimates` với file báo giá. Server chờ tối đa `wait_seconds`
   (mặc định và tối đa **{settings.partner_max_wait_seconds}s**):
   * **200**: xử lý xong, kết quả nằm trong `result`.
   * **202**: chưa xong trong thời gian chờ. Job **vẫn tiếp tục chạy**, hãy gọi
     `GET /v1/estimates/{{job_id}}` mỗi 3–5 giây tới khi `status` là `success` hoặc `failed`.
   * **500** với `status = failed`: không xử lý được tài liệu.
2. Một báo giá thường mất 30–90 giây; tài liệu dài có thể lâu hơn. Muốn luôn bất đồng bộ thì gửi `wait_seconds=0`.

Kết quả được giữ {settings.result_ttl_seconds // 86400} ngày kể từ khi tạo job.

## Giới hạn
* File: {", ".join(sorted(SUPPORTED_SUFFIXES))}; tối đa {max_mb} MB.
* Lỗi trả về dạng `{{"detail": {{"code": "...", "message": "..."}}}}`; `429` khi vượt giới hạn tần suất.

## Ví dụ
```bash
curl -X POST "$BASE/partner/v1/estimates" \\
  -H "X-API-Key: $API_KEY" \\
  -F "file=@bao_gia.jpg" -F "reference_id=CLAIM-2026-0001"
```
"""


def _error_responses(*codes: int) -> dict:
    return {code: {"model": PartnerErrorResponse} for code in codes}


def create_partner_app(settings: ApiSettings, queue_factory: Callable[[], Queue]) -> FastAPI:
    app = FastAPI(
        title="AICycle Repair Estimate API",
        version="1.0.0",
        description=_description(settings),
        redoc_url=None,
        swagger_ui_parameters={"persistAuthorization": True},
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        """Giữ một định dạng lỗi {code, message} cho khách thay vì danh sách lỗi mặc định của FastAPI."""
        message = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                            content={"detail": {"code": "INVALID_REQUEST", "message": message}})

    def client_name(api_key: str | None = Security(_api_key_header)) -> str:
        if not settings.partner_api_keys:
            raise api_error(status.HTTP_503_SERVICE_UNAVAILABLE, "PARTNER_API_DISABLED", "API đối tác chưa được bật")
        if api_key:
            for key, name in settings.partner_api_keys.items():
                if secrets.compare_digest(key.encode(), api_key.encode()):
                    return name
        raise api_error(status.HTTP_401_UNAUTHORIZED, "INVALID_API_KEY", "Thiếu hoặc sai X-API-Key")

    def fetch_owned(queue: Queue, job_id: str, client: str) -> Job:
        """Job của khách khác (hoặc của demo) coi như không tồn tại."""
        job = queue.fetch_job(job_id)
        if job is None or job.meta.get("client") != client:
            raise api_error(status.HTTP_404_NOT_FOUND, "JOB_NOT_FOUND", "Không tìm thấy job hoặc job đã hết hạn")
        return job

    def to_estimate(job: Job, request: Request) -> dict:
        state = public_status(job.get_status(refresh=True))
        body = {
            "job_id": job.id, "reference_id": job.meta.get("reference_id"), "status": state,
            "created_at": job.created_at, "finished_at": job.ended_at,
            "status_url": f"{request.scope.get('root_path', '')}/v1/estimates/{job.id}",
        }
        if state == "success" and isinstance(job.result, dict):
            # Kết quả worker không đánh số dòng (index chỉ được thêm khi lưu MongoDB).
            # response_model lọc phần còn lại về document + items theo hợp đồng.
            items = [{"index": i, **item} for i, item in enumerate(job.result.get("items") or [])]
            body["result"] = {**job.result, "items": items}
        elif state in {"success", "failed"}:
            body.update(status="failed", error=FAILED_ERROR)
        return body

    @app.post(
        "/v1/estimates",
        response_model=Estimate,
        summary="Gửi báo giá và nhận kết quả",
        tags=["Estimates"],
        responses={
            202: {"model": Estimate, "description": "Chưa xong trong thời gian chờ; hỏi lại qua `status_url`."},
            500: {"model": Estimate, "description": "`status = failed`: không xử lý được tài liệu."},
            **_error_responses(401, 413, 415, 422, 429, 503),
        },
    )
    async def create_estimate(
        request: Request,
        response: Response,
        file: UploadFile = File(description="Ảnh hoặc PDF báo giá sửa chữa."),
        reference_id: str | None = Form(
            None, max_length=100, description="Mã tham chiếu phía khách (mã hồ sơ, claim...); trả lại nguyên văn."),
        wait_seconds: int | None = Form(
            None, ge=0,
            description=f"Số giây server giữ request chờ kết quả, 0–{settings.partner_max_wait_seconds} "
                        f"(mặc định {settings.partner_max_wait_seconds}). Giá trị lớn hơn được hạ về mức tối đa."),
        client: str = Depends(client_name),
    ) -> dict:
        queue = queue_factory()
        job = await enqueue_upload(lambda: queue, settings, file, JobOptions(),
                                   {"channel": "partner", "client": client, "reference_id": reference_id})
        wait = settings.partner_max_wait_seconds if wait_seconds is None else min(
            wait_seconds, settings.partner_max_wait_seconds)
        deadline = time.monotonic() + wait
        while True:
            body = to_estimate(fetch_owned(queue, job.id, client), request)
            if (body["status"] in {"success", "failed"} or time.monotonic() >= deadline
                    or await request.is_disconnected()):
                break
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        response.status_code = {"success": status.HTTP_200_OK, "failed": status.HTTP_500_INTERNAL_SERVER_ERROR}.get(
            body["status"], status.HTTP_202_ACCEPTED)
        return body

    @app.get(
        "/v1/estimates/{job_id}",
        response_model=Estimate,
        summary="Xem trạng thái / kết quả của một job",
        tags=["Estimates"],
        responses=_error_responses(401, 404, 422, 429, 503),
    )
    def get_estimate(job_id: str, request: Request, client: str = Depends(client_name)) -> dict:
        return to_estimate(fetch_owned(queue_factory(), job_id, client), request)

    return app
