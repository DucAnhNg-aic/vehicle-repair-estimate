"""RQ task chạy OCR, mapping, cache và lưu MongoDB."""
from __future__ import annotations

import json
import os
from contextlib import ExitStack
from pathlib import Path

from google import genai
from google.genai import types
from rq import get_current_job

from llm_cache import TwoTierCache
from ocr.extractor import load_env
from repair_estimate_pipeline import run_repair_estimate_pipeline
from repair_estimate_pipeline.metrics import build_metrics
from storage import MongoSettings, RepairEstimateRepository


def _set_progress(step: str) -> None:
    job = get_current_job()
    if job is not None:
        job.meta["step"] = step
        job.save_meta()


def _read_mapping(run_dir: str | Path) -> dict:
    return json.loads((Path(run_dir) / "mapping.json").read_text(encoding="utf-8"))


def process_repair_estimate_job(file_path: str, options: dict, server: dict) -> dict:
    """RQ entrypoint. Mọi secret và đường dẫn hệ thống lấy từ cấu hình server."""
    source = Path(file_path)
    env_file = Path(server["env_file"])
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
            cache = None
            if options["use_cache"]:
                cache = stack.enter_context(TwoTierCache.from_env(env_file))
            client = stack.enter_context(genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(timeout=int(options["timeout_seconds"] * 1000)),
            ))
            _set_progress("ocr_mapping")
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
            )
            saved = None
            if repository is not None:
                _set_progress("storage")
                saved = repository.save_run(summary["run_dir"])
            mapping = _read_mapping(summary["run_dir"])
            _set_progress("completed")
            return {
                "job_id": get_current_job().id if get_current_job() else None,
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
