#!/usr/bin/env bash
# Chạy toàn bộ OCR → mapping codebook (exact match + LLM fallback; provider Gemini hoặc Claude).
#
# Cách dùng (đường dẫn tương đối tính từ thư mục đang gọi lệnh):
#   ./run_repair_estimate_pipeline.sh 'data/img/bao gia.pdf'
#   ./run_repair_estimate_pipeline.sh 'data/img/' --timeout 300
# Folder: chạy tuần tự file được hỗ trợ ngay trong folder, không duyệt folder con.
# Tiếp tục khi một file lỗi; trả exit code 1 nếu có bất kỳ file nào lỗi.
#   ./run_repair_estimate_pipeline.sh 'data/img/bao gia.jpg' --provider claude --timeout 300
#   ./run_repair_estimate_pipeline.sh 'data/img/bao gia.pdf' --exact-only
#   ./run_repair_estimate_pipeline.sh --help
#
# Các option CLI có thể truyền sau đường dẫn input:
#   --provider NAME        gemini | claude cho cả OCR và mapping (mặc định theo --model, rồi LLM_PROVIDER).
#   --model MODEL          Model OCR gemini-... hoặc claude-... (mặc định <PROVIDER>_OCR_MODEL từ env/.env).
#   --mapping-model MODEL  Model mapping (mặc định <PROVIDER>_MAPPING_MODEL); model hãng khác = thử trộn provider.
#   --exact-only           Mapping chỉ exact; bước OCR vẫn gọi LLM.
#   --save-mongodb         Lưu MongoDB sau OCR và mapping (đọc MONGO_* từ .env).
#   --no-cache             Bỏ qua cache Redis/MongoDB (REDIS_URL, MONGO_URI trong .env).
#   --output-dir DIR       Thư mục lưu kết quả từng lần chạy.
#   --codebook-dir DIR     Chứa service_codebook.xlsx và part_codebook.xlsx.
#   --env-file FILE        File chứa API key, LLM_PROVIDER và model mặc định từng provider.
#   --timeout SECONDS      Timeout mỗi HTTP request.
#
# Có thể chỉnh các giá trị mặc định bên dưới hoặc override bằng biến môi trường:
#   PYTHON_BIN=/path/to/python OUTPUT_DIR=/tmp/vre ./run_repair_estimate_pipeline.sh '/path/input.pdf'
# API key đọc từ environment hoặc .env; không đặt trực tiếp trong file này.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python}"
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/.env}"
OUTPUT_DIR="${OUTPUT_DIR:-$SCRIPT_DIR/outputs/repair_estimate_pipeline}"
CODEBOOK_DIR="${CODEBOOK_DIR:-$SCRIPT_DIR/data/codebook}"
TIMEOUT="${TIMEOUT:-180}"

if (( $# == 0 )); then
    printf 'Cách dùng: %s <file hoặc folder> [options]\n' "$0" >&2
    printf 'Xem options: %s --help\n' "$0" >&2
    exit 2
fi

# Cho phép import VRE pipeline khi gọi script từ thư mục khác; giữ nguyên cwd
# để các đường dẫn input/option tương đối vẫn đúng với nơi gọi lệnh.
export PYTHONPATH="$SCRIPT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
command=("$PYTHON_BIN" -m repair_estimate_pipeline
    --env-file "$ENV_FILE"
    --output-dir "$OUTPUT_DIR"
    --codebook-dir "$CODEBOOK_DIR"
    --timeout "$TIMEOUT")

if [[ "$1" == "--help" || "$1" == "-h" ]]; then
    printf 'Cách dùng: %s <file hoặc folder> [options]\n' "$0"
    printf 'Folder: đọc ảnh/PDF/Markdown trực tiếp bên trong, không duyệt folder con.\n\n'
    exec "${command[@]}" --help
fi

input="$1"
shift
if [[ -f "$input" ]]; then
    exec "${command[@]}" "$@" -- "$input"
elif [[ ! -d "$input" ]]; then
    printf 'Input không phải file hoặc folder tồn tại: %s\n' "$input" >&2
    exit 2
fi

# Bash array giữ nguyên đường dẫn có dấu cách; dotglob bao gồm file ẩn.
shopt -s nullglob dotglob
files=()
for file in "$input"/*; do
    [[ -f "$file" ]] || continue
    case "${file,,}" in
        *.jpg|*.jpeg|*.png|*.webp|*.heic|*.heif|*.pdf|*.md|*.markdown)
            files+=("$file") ;;
    esac
done
if (( ${#files[@]} == 0 )); then
    printf 'Không có ảnh/PDF/Markdown được hỗ trợ trong folder: %s\n' "$input" >&2
    exit 2
fi

succeeded=0
failed=0
for file in "${files[@]}"; do
    printf '\n[%d/%d] %s\n' "$((succeeded + failed + 1))" "${#files[@]}" "$file"
    if "${command[@]}" "$@" -- "$file"; then
        succeeded=$((succeeded + 1))
    else
        failed=$((failed + 1))
        printf 'LỖI: %s; tiếp tục file kế tiếp.\n' "$file" >&2
    fi
done
printf '\nHoàn tất: %d thành công, %d lỗi, tổng %d file.\n' "$succeeded" "$failed" "${#files[@]}"
(( failed == 0 ))
