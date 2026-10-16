# Vehicle Repair Estimate (VRE)

Trích xuất và đối chiếu báo giá sửa chữa xe bằng Gemini.

Chạy với Python 3.11 trong conda `da_gemini`:

```bash
conda activate da_gemini
cd /home/ubuntu/ducanh/vehicle-repair-estimate
python -m pip install -r requirements.txt
export PYTHONPATH=src   # mã nguồn nằm trong src/
python -m ocr
```

Script đọc `GEMINI_API_KEY`, `GEMINI_MODEL` từ `.env` và sử dụng
biến chuỗi `PROMPT` trong `prompt.py`. Biến môi trường đã export được ưu tiên hơn `.env`;
`--model` được ưu tiên cao nhất. `.env` hỗ trợ các dòng đơn giản `KEY=value`
hoặc `KEY="value"`. Mặc định chạy ảnh JPG duy nhất trong `data/img/`.

Sửa nội dung hướng dẫn trong chuỗi `PROMPT` của `prompt.py`.
`--prompt /path/to/custom_prompt.py` dùng module Python khác có biến `PROMPT`;
`--prompt /path/to/prompt.txt` vẫn hỗ trợ prompt dạng text UTF-8.
File `.py` được thực thi để lấy `PROMPT`, vì vậy chỉ dùng module do bạn kiểm soát.

```bash
python -m ocr --dry-run
python -m ocr --list-models
python -m ocr '/path/to/bao gia.pdf' --model gemini-3.6-flash
python -m ocr 'data/img/TAILIEUTHIETHAIXE64A33283-20260828-071310.md'
```

File `.md`/`.markdown` được gửi dưới dạng text UTF-8 (giữ nguyên bảng HTML
nếu có), dùng cùng prompt, schema và cấu hình model như ảnh. Chi phí này
chưa bao gồm bước OCR tạo Markdown. `metadata.json.input_mode` ghi loại input.

Mỗi lần gọi tạo thư mục riêng trong `outputs/`, gồm `result.json` đã kiểm tra
schema, `raw.txt`, `response.json` (có token usage), `metadata.json` (model,
input, thời gian). Nếu model trả rỗng, chưa hoàn thành hoặc JSON sai schema,
script báo lỗi và giữ response để kiểm tra. Schema kiểm tra kiểu và trường;
độ chính xác OCR/số tiền vẫn cần đối chiếu với tài liệu gốc. Không tự tính
hoặc sửa tiền sau extraction.

Thời gian được in trên terminal và lưu trong `metadata.json`, kể cả khi gọi
API hoặc kiểm tra JSON thất bại:

- `prepare_media_seconds`: đọc/encode ảnh hoặc upload và chờ file sẵn sàng.
- `gemini_request_seconds`: thời gian chờ toàn bộ lời gọi Gemini, gồm mạng
  và xử lý phía server; không phải thời gian inference riêng của model.
- `elapsed_seconds`: tổng từ lúc bắt đầu xử lý đến khi lưu kết quả và xóa
  file upload tạm; không gồm khởi động Python, đọc cấu hình và ghi metadata.

Lệnh chạy vẫn là `python -m ocr`, không cần thêm tham số.

Chi phí ước tính được in trên terminal và lưu ở `metadata.json.cost`, gồm
input không cache, cached input, output và thinking. Thinking tính theo giá
output, cached tokens được trừ khỏi input thường để không tính hai lần.
Đây là ước tính Standard paid tier, không xác định key đang dùng free tier
hay paid tier; không bao gồm thuế, credits hoặc phí tool. Phí lưu explicit
context cache của mapping được ước tính riêng và cộng vào job tạo cache.

Bảng giá `gemini-3.6-flash` kiểm tra ngày 01/10/2026 tại
[Google](https://ai.google.dev/gemini-api/docs/pricing#gemini-3.6-flash):
đến 31/12/2026 là $0.75 input, $0.075 cached input, $3.75 output/thinking
trên một triệu token. Từ 01/01/2027 lần lượt là $1.50, $0.15, $7.50.
Model chưa có bảng giá hoặc response thiếu usage sẽ có `total_usd: null`.

## Luồng chính: OCR → mapping (một lệnh)

```bash
python -m repair_estimate_pipeline 'data/img/bao_gia.jpg'                 # OCR + mapping
python -m repair_estimate_pipeline 'data/img/bao_gia.jpg' --exact-only    # mapping không gọi Gemini
python -m repair_estimate_pipeline 'data/img/bao_gia.jpg' --mapping-model gemini-3.6-flash
```

Mỗi lần chạy tạo `outputs/repair_estimate_pipeline/<timestamp>/`:

| File | Nội dung |
|---|---|
| `summary.json` | trạng thái, thời gian, kết quả, chi phí **từng bước** và **tổng** |
| `report.txt` | báo cáo text giống log in ra terminal |
| `ocr/` | `response.json`, `raw.txt`, `result.json`, `metadata.json` của bước OCR |
| `mapping.json` | các trường OCR + `mapping` cho từng item |
| `mapping.calls.json` | từng lời gọi Gemini fallback: response, usage, cost, latency |

`summary.json` luôn được ghi kể cả khi một bước lỗi (bước sau ghi `skipped`,
token đã tốn vẫn được tính). Gọi từ Python:
`from repair_estimate_pipeline import run_repair_estimate_pipeline`.

## Chi phí Gemini (`cost/`)

Nơi duy nhất chứa bảng giá và logic tính tiền, dùng chung cho `ocr`, `mapping`,
`classification`, `pipeline`. Thêm model mới: thêm một dòng vào `PRICES` trong
`cost/pricing.py`.

```python
from cost import cost_from_response, sum_costs

cost = cost_from_response(response.model_dump(mode="json"), fallback_model=model)
total = sum_costs([cost_ocr, *cost_mapping_calls])  # total_usd, tokens, complete, unpriced_calls
```

Đây là ước tính theo Standard paid tier, không phải hóa đơn. `total_usd` của
`sum_costs` chỉ gồm lời gọi đã định giá; `complete=False` nếu có lời gọi chưa định giá.

## Endpoint được chọn

- `POST https://generativelanguage.googleapis.com/v1beta/interactions`:
  phù hợp đọc ảnh/PDF và trả structured JSON; Google khuyến nghị cho dự án mới.
  Dùng `store=False` vì mỗi lần chạy là một yêu cầu độc lập.
- `models/{model}:generateContent`: dùng cho mapping với explicit context caching.
- Files API: upload khi file lớn hơn 15 MiB; chờ ACTIVE trước khi đọc và xóa
  file tạm trong `finally`. File nhỏ gửi base64 inline, không cần upload riêng.
- `GET /v1beta/models`: dùng qua `--list-models` để kiểm tra model key được truy cập;
  không tự đổi model nếu model cấu hình không khả dụng.

Tham khảo chính thức:
[Interactions](https://ai.google.dev/gemini-api/docs/interactions-overview),
[đọc tài liệu](https://ai.google.dev/gemini-api/docs/document-processing),
[structured output](https://ai.google.dev/gemini-api/docs/structured-output),
[generateContent](https://ai.google.dev/api/generate-content).

## OCR và mapping độc lập / tích hợp

OCR trả `RepairEstimate` đã validate; lỗi API/schema được raise cho luồng gọi xử lý.
API không tự load `.env`; CLI vẫn load `.env`. Client truyền vào do caller quản lý.

```python
from google import genai
from ocr import extract_repair_estimate
from mapping import CodebookMapper, GeminiMatcher

with genai.Client() as client:  # GEMINI_API_KEY từ environment
    estimate = extract_repair_estimate("data/img/bao_gia.jpg", model="your-model", client=client)
    mapper = CodebookMapper(matcher=GeminiMatcher(client, "your-model"))
    mappings = mapper.map_repair_estimate(estimate)
    # Hoặc mapper.map_item(estimate.items[0])
    for item, mapped in zip(estimate.items, mappings):
        print(item.raw_name, [row.code for row in mapped.matches])
```

Mapping dùng duy nhất `data/codebook/codebook.xlsx` (các cột `MA`, `TEN`, `item_type`)
và một `MAPPING_INSTRUCTION` chung. Material/labor/paint được gộp chung batch,
không gọi riêng theo category hoặc part/service. Payload `names` là danh sách
`{name, item_type}` duy nhất; không gửi mã MA.

- `material`: exact với tên thuộc part trước; chưa khớp thì gửi Gemini cùng các công việc.
- `labor`/`paint`: có bộ phận trong raw_name thì map part; không có mới map service.
  Giữ đúng trái/phải, không tìm thấy bộ phận thì unmatched, không chuyển sang dịch vụ chung.

Tối đa 50 item chưa exact mỗi batch; cache loại các item đã có kết quả trước khi gọi.
Vì vậy không quá 50 item cần mapping thì tối đa một request Gemini cho bước mapping
(OCR vẫn là bước riêng). Nhiều hơn 50 item vẫn chia batch. Trả đúng thứ tự đầu vào.
Gemini trả name và item_type; code kiểm tra tên thuộc đúng loại rồi tra tất cả mã
trùng tên trong loại đó. Material không được trả service. Null trả unmatched.
Khóa cache bao gồm prompt chung mới và tên/loại codebook, không dùng kết quả prompt cũ.

```bash
# OCR độc lập
python -m ocr '/path/to/bao_gia.pdf'
# Mapping từ kết quả OCR, bật Gemini fallback mặc định
python -m mapping '/path/to/result.json' --output outputs/mapped.json
# Test exact offline, không cần key
python -m mapping '/path/to/result.json' --exact-only --output outputs/exact.json
# Unit tests offline (mock Gemini)
PYTHONPATH=src python -m unittest discover -s tests -v
```

CLI mapping giữ các trường OCR và thêm `mapping` vào mỗi item, theo đúng thứ tự.
`mapping.matches` chứa `code`, `name`, `row`, `item_type`; không loại các dòng trùng.
File `.calls.json` bên cạnh output lưu response/usage và latency từng lời gọi
Gemini, kể cả khi mapping thất bại. Chạy lại cùng `--output` sẽ ghi đè file đó.
`CodebookMapper()` không truyền matcher (hoặc CLI `--exact-only`) chỉ exact vật tư;
nhân công/sơn trả unmatched vì cần Gemini xác định bộ phận/dịch vụ.

## Explicit context cache cho mapping

Mapping dùng `client.models.generate_content`; OCR vẫn dùng Interactions API.
Khi có item cần gọi Gemini, tạo hoặc dùng lại cache Google chứa `MAPPING_INSTRUCTION`
và danh sách unique `{name, item_type}` của `codebook.xlsx`. Mỗi request mới chỉ gửi
items + `cached_content` + schema đầu ra; codebook và prompt không gửi lại.

Cấu hình server qua `.env` (không phải option UI):

```dotenv
MAPPING_CONTEXT_CACHE_ENABLED=true
MAPPING_CONTEXT_CACHE_TTL_SECONDS=1800
```

Mặc định bật, TTL 30 phút. Cache tạo khi cần và không tự gia hạn. Đổi model, prompt,
danh sách tên/loại, schema hoặc API key tạo cache mới; cache cũ tự hết hạn trên Google.
Registry và khóa tạo nằm trong `outputs/.gemini_context_cache/`, dùng chung volume
outputs giữa các job/worker trên cùng host. Không tạo lại cache sau mỗi job.
Đây là cache Google độc lập với cache kết quả Redis/Mongo: option API `use_cache`
điều khiển cache kết quả; biến server trên điều khiển context cache.
Xóa Mongo/Redis không xóa Google context cache hoặc registry trong outputs.

Lỗi tạo context cache thì cảnh báo và gửi đầy đủ codebook bằng generateContent.
Cache Google bị xóa sớm (404) thì bỏ registry và retry một lần bằng đầy đủ context.
Lỗi request khác được raise; response bị chặn/truncated không được ghi cache kết quả.

`mapping.calls.json` ghi `endpoint`, `context_cache`, response và token usage.
API/Mongo có `metrics.mapping.context_cache`: tên cache, expire_time, created,
token_count, ttl_seconds và storage_estimated_usd. Token cache nằm trong
`cost.tokens.total_cached_tokens`. Phí lưu `context_cache_storage_usd` được ước tính
cho toàn bộ TTL, cộng một lần vào cost của job tạo cache; job dùng lại không cộng lần nữa.
Đây là phân bổ chi phí dự kiến, không phải hóa đơn theo thời gian lưu thực tế.
Nếu thiếu số token/giá thì cost có `complete=false`. Phí lưu vẫn có thể phát sinh
khi request mapping lỗi. Context caching không bảo đảm giảm thinking token/latency.

Tài liệu: [Explicit caching](https://ai.google.dev/gemini-api/docs/generate-content/caching),
[generateContent](https://ai.google.dev/api/generate-content).

## Lưu và xem kết quả bằng MongoDB

Chạy pipeline và tự động lưu DB: `./run_repair_estimate_pipeline.sh data/img/ --save-mongodb`.
Option này dùng được với cả file và folder. Pipeline kiểm tra DB trước khi gọi
Gemini. Nếu ghi DB thất bại sau xử lý, JSON vẫn còn để import lại bằng lệnh bên dưới.

```bash
docker compose up -d
python -m storage ping
python -m storage import outputs/repair_estimate_pipeline/<run_id>
python -m storage show --limit 5
python -m storage show --vehicle-plate 30F60314 --full
```

Mongo Express chạy local tại `http://127.0.0.1:8082`, đăng nhập bằng
`MONGO_USERNAME` / `MONGO_PASSWORD` trong `.env`. UI chỉ bind vào loopback và dùng cho môi trường phát triển.
Trong UI, mở database `vehicle_repair_estimate`, rồi collection
`repair_estimates` để xem và lọc các kết quả đã import.

Module đọc `MONGO_URI`, `MONGO_DATABASE`, `MONGO_COLLECTION` từ `.env`.
`import` dùng `run_id` để upsert nên chạy lại cùng một run không tạo bản ghi trùng.
Mặc định `show` chỉ in thông tin tổng quan; thêm `--full` để xem toàn bộ items.

## FastAPI và Redis worker

### API đối tác (`/partner`)

Endpoint cho hệ thống bên ngoài, chạy chung container `api`/`worker` và cùng hàng đợi với demo,
nhưng xác thực bằng header `X-API-Key` thay vì Basic Auth:

- Swagger: `http://<IP_ADDRESS>:8000/partner/docs` (spec: `/partner/openapi.json`, bản xuất sẵn ở
  `docs/partner-openapi.json` để import Postman).
- `POST /partner/v1/estimates` gửi file và giữ request chờ tối đa `VRE_PARTNER_MAX_WAIT_SECONDS`
  (mặc định 90s, dưới timeout 100s của Cloudflare/proxy phổ biến): `200` kèm `result` khi xong,
  `202` kèm `status_url` khi chưa xong (job vẫn chạy), `500` với `status=failed`.
- `GET /partner/v1/estimates/{job_id}` hỏi lại trạng thái/kết quả. Khách chỉ thấy job của chính mình.
- Kết quả chỉ gồm `document` + `items` (kèm `mapping.method` = `exact|ai|unmatched` và mã AICycle);
  không trả chi phí, tên model hay đường dẫn nội bộ.
- MongoDB lưu mỗi lần chạy kèm `source` (`channel` = `demo|partner`, `client`, `reference_id`, `job_id`;
  `null` khi chạy CLI), `input.file_name` là tên file gốc và `input.sha256` là hash nội dung file.
  Lịch sử `/runs?q=` tìm được cả theo `reference_id`.

Cấp key trong `.env` (mỗi khách một key, xoá entry rồi recreate `api` để thu hồi):

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
# VRE_PARTNER_API_KEYS=aicycle-be:<key>,khach-b:<key2>
docker compose up -d --no-deps api
```

Nginx giới hạn 60 request/phút cho mỗi key (burst 20), 30 kết nối đồng thời mỗi IP và đặt
`proxy_read_timeout 120s` riêng cho `/partner/`. Khi đổi schema, xuất lại spec:
`curl -s http://<IP_ADDRESS>:8000/partner/openapi.json > docs/partner-openapi.json`.

### Nginx bảo vệ demo

Nginx là cửa vào duy nhất tại `http://<IP_ADDRESS>:8000/demo`. UI và API đều
yêu cầu Basic Auth. Tạo credentials local một lần trước khi chạy stack:

```bash
python nginx/create_credentials.py
docker compose up -d --build api worker nginx mongo-express
```

Username/password nằm trong `nginx/auth/credentials.txt`, không được commit hoặc
đóng vào image. Trình duyệt tự hỏi đăng nhập; curl dùng `-u demo` để hỏi mật khẩu.
Không đưa Gemini key hoặc secret vào JavaScript.

Nginx giới hạn body 26 MiB (API vẫn giới hạn file 25 MiB), tạo job 20 request/phút/IP
với burst 2 và tối đa 10 kết nối/IP. `/docs`, `/redoc`, `/openapi.json` và `/health/`
không truy cập qua gateway. FastAPI không publish cổng host; MongoDB/Mongo Express
chỉ bind localhost. Nginx tự cập nhật IP upstream API qua Docker DNS mỗi 5 giây,
kể cả khi API container được recreate riêng.

Cấu hình hiện tại là HTTP cho LAN. Trước khi public Internet, cần domain và TLS
(HTTPS tại gateway phía trước hoặc thêm server TLS vào Nginx); Basic Auth qua HTTP
không mã hóa thông tin đăng nhập. Chưa có quota tổng chi phí Gemini hoặc giới hạn queue.
Chạy kiểm tra XSS: `node tests/test_demo_xss.js`.

UI demo nằm tại `/demo` trên cùng địa chỉ API (ví dụ `http://192.168.80.24:8000/demo`).
Chọn file, thiết lập options và bấm xử lý; UI tự theo dõi job và hiển thị kết quả,
thời gian/chi phí OCR và mapping, cùng nút tải JSON. UI được đóng trong image API.

API nhận file theo `multipart/form-data`, đưa job vào Redis rồi worker chạy OCR → mapping
và mặc định lưu kết quả vào MongoDB. Chạy toàn bộ stack bằng Docker Compose:

```bash
docker compose up -d --build api worker
docker compose ps
```

API bind tại `127.0.0.1:8000`. Hai service dùng chung volume upload để worker đọc
được file API nhận, và volume output để giữ artifacts qua các lần recreate container.
Khi cần chạy ngoài Docker, vẫn có thể mở hai terminal với
`python -m repair_estimate_api` và `python -m repair_estimate_api.worker`.

Gửi một job:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/repair-estimate/jobs \
  -u demo \
  -F 'file=@data/img/TAILIEUTHIETHAIXE51K85618.jpg' \
  -F 'options={"use_cache":true,"save_mongodb":true}'
```

Response `202` có `job_id`, `status_url` và `result_url`. Dùng hai URL đó để xem
trạng thái hoặc lấy kết quả hoàn chỉnh. Swagger UI ở `http://127.0.0.1:8000/docs`.

Kết quả API và document MongoDB dùng chung `metrics.total`, `metrics.ocr`,
`metrics.mapping`; mỗi phần có `duration_seconds` và `cost` (token usage, chi phí USD).
`total` đo OCR + mapping, không bao gồm upload HTTP, chờ queue và ghi MongoDB.
OCR cache hit có `cost: null` vì không gọi Gemini. Các document đã lưu theo
`mapping_metrics` và kết quả job Redis cũ không tự chuyển sang cấu trúc mới.

API luôn chạy OCR rồi mapping (exact match + Gemini/cache fallback).
Các option public: `ocr_model`, `mapping_model`, `use_cache`,
`save_mongodb`, `timeout_seconds` (10–600). Model phải nằm trong
`VRE_API_ALLOWED_MODELS` (danh sách cách nhau bởi dấu phẩy, mặc định chỉ có
`GEMINI_MODEL`). Secret, URI, output/codebook/prompt path chỉ lấy từ server.

Biến cấu hình server tùy chọn:

```text
VRE_API_ALLOWED_MODELS=gemini-3.8-flash
VRE_API_HOST=127.0.0.1
VRE_API_PORT=8000
VRE_API_QUEUE=repair-estimate
VRE_API_UPLOAD_DIR=/tmp/vre-api/uploads
VRE_API_MAX_UPLOAD_BYTES=26214400
VRE_API_RESULT_TTL_SECONDS=604800
VRE_API_JOB_TIMEOUT_SECONDS=2100
VRE_API_WORKERS=10   # số job chạy song song trong container worker (compose mặc định 10)
```
