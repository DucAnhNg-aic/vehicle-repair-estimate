"""RQ task chạy OCR, mapping, cache và lưu MongoDB."""
from __future__ import annotations

import json
import logging
import os
from contextlib import ExitStack
from pathlib import Path

from google import genai
from google.genai import types
from rq import get_current_job

from llm_cache import TwoTierCache
from ocr.extractor import load_env
from repair_estimate_pipeline import new_run_dir, run_repair_estimate_pipeline
from repair_estimate_pipeline.metrics import build_metrics
from storage import InputFileStore, MongoSettings, RepairEstimateRepository

logger = logging.getLogger(__name__)


def _set_progress(step: str) -> None:
    job = get_current_job()
    if job is not None:
        job.meta["step"] = step
        job.save_meta()


def _read_mapping(run_dir: str | Path) -> dict:
    return json.loads((Path(run_dir) / "mapping.json").read_text(encoding="utf-8"))


def _save_failed_run(repository: RepairEstimateRepository, run_dir: Path, origin: dict,
                     file_name: str | None) -> None:
    """Lưu run lỗi vào MongoDB; lỗi khi lưu chỉ ghi log để không che mất lỗi gốc của pipeline."""
    if not (run_dir / "summary.json").is_file():
        return
    try:
        repository.save_run(run_dir, source=origin, file_name=file_name)
    except Exception:
        logger.exception("Không lưu được run lỗi %s vào MongoDB", run_dir.name)


def process_repair_estimate_job(file_path: str, options: dict, server: dict) -> dict:
    """RQ entrypoint. Mọi secret và đường dẫn hệ thống lấy từ cấu hình server."""
    source = Path(file_path)
    env_file = Path(server["env_file"])
    job = get_current_job()
    meta = job.meta if job else {}
    origin = {"channel": meta.get("channel"), "client": meta.get("client"),
              "reference_id": meta.get("reference_id"), "job_id": job.id if job else None}
    try:
        load_env(env_file)
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("Server chưa cấu hình GEMINI_API_KEY")
        with ExitStack() as stack:
            repository = None
            if options["save_mongodb"]:
                repository = stack.enter_context(RepairEstimateRepository(MongoSettings.from_env(env_file)))
                repository.ping()
                repository.ensure_indexes()
                # Lưu file gốc ngay đầu job để job lỗi/bị kill giữa chừng vẫn còn file mà điều tra.
                # Job enqueue trước khi có tính năng này không có input_store_dir trong server.
                if server.get("input_store_dir"):
                    stored = InputFileStore(server["input_store_dir"]).put(source)
                    repository.record_input_file(stored, file_name=meta.get("file_name"))
            cache = None
            if options["use_cache"]:
                cache = stack.enter_context(TwoTierCache.from_env(env_file))
            client = stack.enter_context(genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(timeout=int(options["timeout_seconds"] * 1000)),
            ))
            _set_progress("ocr_mapping")
            run_dir = new_run_dir(server["output_dir"])
            try:
                summary = run_repair_estimate_pipeline(
                    source,
                    model=options["ocr_model"],
                    mapping_model=options["mapping_model"],
                    exact_only=False,
                    client=client,
                    output_dir=server["output_dir"],
                    codebook_dir=server["codebook_dir"],
                    timeout=options["timeout_seconds"],
                    verbose=False,
                    cache=cache,
                    ocr_thinking_level=options.get("ocr_thinking_level"),
                    mapping_thinking_level=options.get("mapping_thinking_level"),
                    run_dir=run_dir,
                )
            except Exception:
                if repository is not None:
                    _save_failed_run(repository, run_dir, origin, meta.get("file_name"))
                raise
            saved = None
            if repository is not None:
                _set_progress("storage")
                saved = repository.save_run(summary["run_dir"], source=origin, file_name=meta.get("file_name"))
            mapping = _read_mapping(summary["run_dir"])
            _set_progress("completed")
            return {
                "job_id": origin["job_id"],
                "status": "success",
                "document": mapping["document"],
                "items": mapping["items"],
                "metrics": build_metrics(
                    summary, item_count=len(mapping["items"]),
                    methods=summary["steps"]["mapping"].get("methods", {}),
                ),
                "storage": {
                    "saved": saved is not None,
                    "run_id": saved["run_id"] if saved else Path(summary["run_dir"]).name,
                },
            }
    finally:
        source.unlink(missing_ok=True)
