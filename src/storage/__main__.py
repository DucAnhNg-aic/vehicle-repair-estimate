"""MongoDB utility: ping, import a completed run, or show stored results."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from ocr.extractor import PROJECT_ROOT
from .mongodb import MongoSettings, RepairEstimateRepository


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("ping", help="Kiểm tra kết nối MongoDB.")
    import_parser = subparsers.add_parser("import", help="Lưu một run đã có vào MongoDB.")
    import_parser.add_argument("run_dir", type=Path)
    show_parser = subparsers.add_parser("show", help="Hiển thị kết quả mới nhất.")
    show_parser.add_argument("--limit", type=int, default=10)
    show_parser.add_argument("--vehicle-plate")
    show_parser.add_argument("--full", action="store_true", help="Hiển thị cả items; mặc định chỉ hiện tổng quan.")
    args = parser.parse_args()

    settings = MongoSettings.from_env(args.env_file)
    with RepairEstimateRepository(settings) as repository:
        repository.ping()
        repository.ensure_indexes()
        if args.command == "ping":
            print(f"MongoDB OK: {settings.database}.{settings.collection}")
        elif args.command == "import":
            print(json.dumps(repository.save_run(args.run_dir), ensure_ascii=False, indent=2, default=_json_default))
        else:
            results = repository.list_results(
                limit=args.limit, vehicle_plate=args.vehicle_plate, include_items=args.full
            )
            print(json.dumps(results, ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
