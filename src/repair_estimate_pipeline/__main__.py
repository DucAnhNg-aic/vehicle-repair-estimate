"""Chạy cả luồng VRE OCR → mapping: python -m repair_estimate_pipeline <file>."""
from __future__ import annotations

import argparse
import sys
from contextlib import ExitStack
from pathlib import Path

from llm import PROVIDER_NAMES, LlmClients, default_models, default_provider, provider_name, redact
from mapping.mapper import CODEBOOK_DIR
from ocr.extractor import PROJECT_ROOT, load_env
from .runner import REPAIR_ESTIMATE_OUTPUT_DIR, run_repair_estimate_pipeline


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="Ảnh/PDF/Markdown báo giá.")
    parser.add_argument("--provider", choices=sorted(PROVIDER_NAMES),
                        help="Provider cho cả OCR và mapping; mặc định theo --model, rồi LLM_PROVIDER trong env/.env.")
    parser.add_argument("--model", help="Model OCR (claude-... hoặc gemini-...); mặc định <PROVIDER>_OCR_MODEL của provider.")
    parser.add_argument("--mapping-model", help="Model cho LLM fallback của mapping; mặc định <PROVIDER>_MAPPING_MODEL "
                                                "của provider. Truyền model hãng khác để thử trộn provider (chỉ CLI).")
    parser.add_argument("--exact-only", action="store_true", help="Mapping chỉ exact match, không gọi LLM fallback.")
    parser.add_argument("--save-mongodb", action="store_true", help="Lưu kết quả vào MongoDB sau khi ghi JSON.")
    parser.add_argument("--no-cache", action="store_true",
                        help="Bỏ qua cache Redis/MongoDB: luôn gọi LLM và không ghi cache.")
    parser.add_argument("--output-dir", type=Path, default=REPAIR_ESTIMATE_OUTPUT_DIR)
    parser.add_argument("--codebook-dir", type=Path, default=CODEBOOK_DIR)
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--timeout", type=float, default=180, help="Timeout mỗi HTTP request (giây).")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout phải lớn hơn 0")
    load_env(args.env_file)
    llm = LlmClients(args.timeout, backend_options={
        "gemini": {"context_cache_dir": args.output_dir / ".gemini_context_cache"}})
    try:
        default_ocr, default_mapping = default_models(
            args.provider or provider_name(args.model) or default_provider())
        model = args.model or default_ocr
        mapping_model = args.mapping_model or default_mapping
        llm.require(model, None if args.exact_only else mapping_model)
    except (RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    with ExitStack() as stack:
        stack.enter_context(llm)
        repository = None
        if args.save_mongodb:
            from storage import MongoSettings, RepairEstimateRepository
            repository = stack.enter_context(RepairEstimateRepository(MongoSettings.from_env(args.env_file)))
            # Check storage before spending tokens on OCR.
            repository.ping()
            repository.ensure_indexes()
        cache = None
        if not args.no_cache:
            from llm_cache import TwoTierCache
            cache = stack.enter_context(TwoTierCache.from_env(args.env_file))
        summary = run_repair_estimate_pipeline(
            args.file, model=model, llm=llm, mapping_model=mapping_model,
            exact_only=args.exact_only, output_dir=args.output_dir,
            codebook_dir=args.codebook_dir, timeout=args.timeout, cache=cache,
        )
        if repository is not None:
            try:
                saved = repository.save_run(summary["run_dir"])
            except Exception as exc:
                print(f"Lưu MongoDB thất bại. JSON đã lưu tại: {summary['run_dir']}", file=sys.stderr)
                print(f"Import lại bằng: python -m storage import '{summary['run_dir']}'", file=sys.stderr)
                raise RuntimeError("Không lưu được MongoDB; dùng output JSON để import lại.") from exc
            print(f"Đã lưu MongoDB: {saved['run_id']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Report already printed by the pipeline; never expose the API key.
        print(f"Lỗi: {redact(str(exc))}", file=sys.stderr)
        raise SystemExit(1)
