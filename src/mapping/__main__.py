"""Map an existing OCR result: python -m mapping result.json."""
import argparse
from pathlib import Path

from llm import PROVIDER_NAMES, LlmClients, default_models, default_provider
from ocr.extractor import PROJECT_ROOT, load_env, write_json
from ocr.schema import RepairEstimate
from .mapper import CODEBOOK_DIR, CodebookMapper, LlmMatcher


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path)
    parser.add_argument("--output", type=Path, default=Path("outputs/mapping.json"))
    parser.add_argument("--codebook-dir", type=Path, default=CODEBOOK_DIR)
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--provider", choices=sorted(PROVIDER_NAMES), help="Mặc định LLM_PROVIDER trong env/.env.")
    parser.add_argument("--model", help="claude-... hoặc gemini-...; mặc định <PROVIDER>_MAPPING_MODEL của provider.")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--exact-only", action="store_true", help="Test exact match offline, không gọi LLM")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout phải lớn hơn 0")
    estimate = RepairEstimate.model_validate_json(args.result.read_text(encoding="utf-8"))
    load_env(args.env_file)
    try:
        model = args.model or default_models(args.provider or default_provider())[1]
    except ValueError as exc:
        parser.error(str(exc))
    with LlmClients(args.timeout, backend_options={"gemini": {"context_cache_dir": args.output.parent / ".gemini_context_cache"}}) as llm:
        if not args.exact_only:
            try:
                llm.require(model)
            except (RuntimeError, ValueError) as exc:
                parser.error(f"{exc}; dùng --exact-only để test offline")
        matcher = None if args.exact_only else LlmMatcher(llm.backend(model), model)
        mapper = CodebookMapper(matcher=matcher, codebook_dir=args.codebook_dir)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        try:
            results = mapper.map_repair_estimate(estimate)
            write_json(args.output, {"document": estimate.document.model_dump(mode="json"),
                                    "items": [dict(item.model_dump(mode="json"), mapping=result.model_dump(mode="json"))
                                              for item, result in zip(estimate.items, results)]})
        finally:
            write_json(args.output.with_suffix(".calls.json"), matcher.calls if matcher else [])
    print(f"Đã map {len(results)} dòng: {args.output}")
    if matcher:
        total = matcher.cost_summary()
        print(f"LLM fallback ({model}): {total['calls']} lời gọi; chi phí ước tính ${total['total_usd']:.6f} USD"
              + ("" if total["complete"] else f" (thiếu {total['unpriced_calls']} lời gọi chưa định giá)"))


if __name__ == "__main__":
    main()
