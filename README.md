# Vehicle Repair Estimate (VRE)

Trích xuất và đối chiếu báo giá sửa chữa xe bằng LLM (OCR + mapping codebook).
OCR và mapping chạy được với hai provider độc lập: **Gemini** và **Claude**; mỗi job chọn một
provider (xem [Provider LLM](#provider-llm-llm)), mặc định `LLM_PROVIDER=gemini`. Luồng phân loại codebook một lần (`classification/`) dùng Gemini.

Chạy với Python 3.11 trong conda `da_gemini`:

```bash
conda activate da_gemini
cd /home/ubuntu/ducanh/vehicle-repair-estimate
python -m pip install -r requirements.txt
export PYTHONPATH=src   # mã nguồn nằm trong src/
python -m ocr
```

Script đọc provider mặc định `LLM_PROVIDER`, model của provider đó (`GEMINI_OCR_MODEL` /
`CLAUDE_OCR_MODEL`) và API key tương ứng (`GEMINI_API_KEY`, `ANTHROPIC_API_KEY`) từ `.env`, dùng
biến chuỗi `PROMPT` trong `prompt.py`. Biến môi trường đã export được ưu tiên hơn `.env`;
`--provider` chọn bộ model mặc định của provider khác, `--model` được ưu tiên cao nhất. `.env` hỗ trợ các dòng đơn giản `KEY=value`
hoặc `KEY="value"`. Mặc định chạy ảnh JPG duy nhất trong `data/img/`.

Sửa nội dung hướng dẫn trong chuỗi `PROMPT` của `prompt.py`.
`--prompt /path/to/custom_prompt.py` dùng module Python khác có biến `PROMPT`;
`--prompt /path/to/prompt.txt` vẫn hỗ trợ prompt dạng text UTF-8.
File `.py` được thực thi để lấy `PROMPT`, vì vậy chỉ dùng module do bạn kiểm soát.

```bash
python -m ocr --dry-run
python -m ocr --list-models                     # model key của provider (theo --provider/--model) truy cập được
python -m ocr '/path/to/bao gia.pdf' --provider claude
python -m ocr '/path/to/bao gia.pdf' --model claude-sonnet-5-5
python -m ocr '/path/to/bao gia.pdf' --model gemini-3.8-flash
python -m ocr 'data/img/TAILIEUTHIETHAIXE64A33283-20260828-071310.md'
```

File `.md`/`.markdown` được gửi dưới dạng text UTF-8 (giữ nguyên bảng HTML
nếu có), dùng cùng prompt, schema và cấu hình model như ảnh. Chi phí này
chưa bao gồm bước OCR tạo Markdown. `metadata.json.input_mode` ghi loại input.

Mỗi lần gọi tạo thư mục riêng trong `outputs/`, gồm `result.json` đã kiểm tra
schema, `raw.txt`, `response.json` (có token usage), `metadata.json` (provider, model,
endpoint, input, thời gian). Nếu model trả rỗng, bị cắt, bị chặn/từ chối hoặc JSON sai
schema, script báo lỗi và giữ response để kiểm tra. Schema kiểm tra kiểu và trường;
độ chính xác OCR/số tiền vẫn cần đối chiếu với tài liệu gốc. Không tự tính
hoặc sửa tiền sau extraction.

Thời gian được in trên terminal và lưu trong `metadata.json`, kể cả khi gọi
API hoặc kiểm tra JSON thất bại:

- `prepare_media_seconds`: đọc/encode ảnh (Claude: chuyển JPEG nếu cần) hoặc upload file lớn.
- `llm_request_seconds`: thời gian chờ toàn bộ lời gọi model, gồm mạng
  và xử lý phía server; không phải thời gian inference riêng của model.
- `elapsed_seconds`: tổng từ lúc bắt đầu xử lý đến khi lưu kết quả và xóa
  file upload tạm; không gồm khởi động Python, đọc cấu hình và ghi metadata.

Chi phí ước tính được in trên terminal và lưu ở `metadata.json.cost` (xem
[Chi phí LLM](#chi-phí-llm-cost)).

## Luồng chính: OCR → mapping (một lệnh)

```bash
python -m repair_estimate_pipeline 'data/img/bao_gia.jpg'                 # OCR + mapping
python -m repair_estimate_pipeline 'data/img/bao_gia.jpg' --exact-only    # mapping không gọi LLM
python -m repair_estimate_pipeline 'data/img/bao_gia.jpg' --provider claude    # cả OCR và mapping bằng Claude
python -m repair_estimate_pipeline 'data/img/bao_gia.jpg' --model claude-sonnet-5-5
# Chỉ CLI: thử trộn provider bằng cách truyền model hãng khác cho mapping
python -m repair_estimate_pipeline 'data/img/bao_gia.jpg' --model gemini-3.5-flash-lite --mapping-model claude-haiku-5-5
```

Mỗi lần chạy tạo `outputs/repair_estimate_pipeline/<timestamp>/`:

| File | Nội dung |
|---|---|
| `summary.json` | trạng thái, thời gian, kết quả, chi phí **từng bước** và **tổng** |
| `report.txt` | báo cáo text giống log in ra terminal |
| `ocr/` | `response.json`, `raw.txt`, `result.json`, `metadata.json` của bước OCR |
| `mapping.json` | các trường OCR + `mapping` cho từng item |
| `mapping.calls.json` | từng lời gọi LLM fallback: provider, response, usage, cost, latency |

`summary.json` luôn được ghi kể cả khi một bước lỗi (bước sau ghi `skipped`,
token đã tốn vẫn được tính). Thiếu API key của provider cần dùng thì báo lỗi trước khi
gọi API. Gọi từ Python: `from repair_estimate_pipeline import run_repair_estimate_pipeline`.

## Provider LLM (`llm/`)

Mỗi provider là một module độc lập trong `src/llm/`, cài cùng giao diện ở `llm/base.py`
(`create_client`, `list_models`, `Backend` với `extract` cho OCR và `select` cho mapping).
OCR (`ocr/extractor.py`) và mapping (`LlmMatcher` trong `mapping/mapper.py`) chỉ làm việc
với `Backend` nên không phụ thuộc SDK của hãng nào. Model thuộc provider theo tiền tố tên:

| Tiền tố model | Provider | API key | Module |
|---|---|---|---|
| `gemini-...` | Gemini | `GEMINI_API_KEY` | `llm/gemini.py` |
| `claude-...` | Claude | `ANTHROPIC_API_KEY` | `llm/claude.py` |

Mỗi job (demo, API nội bộ, API đối tác) chọn **một** provider cho cả OCR và mapping; không chọn
thì dùng `LLM_PROVIDER` (thiếu thì theo provider của `OCR_MODEL` cũ, cuối cùng là `gemini`).
Model mặc định của từng provider (`llm.default_models`):

| Biến | Thiếu thì |
|---|---|
| `GEMINI_OCR_MODEL` / `CLAUDE_OCR_MODEL` | `OCR_MODEL` cũ nếu cùng provider, rồi `gemini-3.8-flash` / `claude-haiku-5-5` |
| `GEMINI_MAPPING_MODEL` / `CLAUDE_MAPPING_MODEL` | `MAPPING_MODEL` cũ nếu cùng provider, rồi model OCR của provider |

`.env` cũ chỉ có `OCR_MODEL=gemini-...` vẫn chạy như trước (chỉ bật Gemini).

`LlmClients` giữ mỗi provider một client, mở khi model đầu tiên của provider đó được dùng,
và đóng khi thoát `with`. Chỉ cần key của provider có model đang cấu hình. Item map bằng
LLM có `mapping.method` là tên provider (`"gemini"` / `"claude"`); API đối tác trả `"ai"`.
Thêm provider mới: viết module theo `llm/base.py` rồi khai báo một dòng trong `llm.PROVIDERS`.

### Gemini (`llm/gemini.py`)

- OCR: `POST /v1beta/interactions`, `store=False`, structured JSON theo schema
  `RepairEstimate`. `OCR_THINKING_LEVEL` gửi thành `generation_config.thinking_level`.
- Mapping: `models/{model}:generateContent` với explicit context cache (xem bên dưới),
  `thinking_config.thinking_level` (mặc định `LOW`).
- Files API: upload khi file lớn hơn 15 MiB; chờ ACTIVE trước khi đọc và xóa
  file tạm trong `finally`. File nhỏ gửi base64 inline.
- `GET /v1beta/models` qua `--list-models`.

Tham khảo: [Interactions](https://ai.google.dev/gemini-api/docs/interactions-overview),
[đọc tài liệu](https://ai.google.dev/gemini-api/docs/document-processing),
[structured output](https://ai.google.dev/gemini-api/docs/structured-output),
[generateContent](https://ai.google.dev/api/generate-content).

### Claude (`llm/claude.py`)

- `POST /v1/messages` qua streaming (`client.beta.messages.stream`): request dài
  (thinking + JSON lớn) không bị cắt bởi read timeout. `--timeout`/`timeout_seconds`
  là timeout mỗi HTTP request.
- Structured outputs (`output_config.format`): JSON theo schema Pydantic của
  `RepairEstimate`/`WorkBatchSelection`; code vẫn validate lại trước khi dùng.
- `OCR_THINKING_LEVEL`/`MAPPING_THINKING_LEVEL` (low/medium/high) được gửi thành
  `output_config.effort`. Để trống OCR thì dùng mặc định của model; mapping mặc định `low`.
- Sonnet 5.5 bật server-side fallback (`fallbacks: "default"`, danh sách trong
  `FALLBACK_MODELS`): bị safety classifier từ chối thì API tự chạy lại trên model khác trong
  cùng request; chi phí tính theo model thực sự phục vụ (`response.model`).
- Chỉ nhận ảnh JPEG/PNG/WebP, tối đa 10 MB (base64) và 8000 px mỗi chiều. Ảnh HEIC/HEIF
  hoặc vượt giới hạn được chuyển sang JPEG, thu cạnh dài về 2576 px (mức Claude tự thu nhỏ
  về nên không mất chi tiết). Khóa cache OCR vẫn tính trên file gốc.
- PDF lớn hơn 15 MiB upload qua Files API rồi xóa trong `finally`; file nhỏ gửi base64.
- `GET /v1/models` qua `--list-models`.

Tham khảo: [Messages API](https://platform.claude.com/docs/en/build-with-claude/working-with-messages),
[structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs),
[vision](https://platform.claude.com/docs/en/build-with-claude/vision),
[PDF](https://platform.claude.com/docs/en/build-with-claude/pdf-support),
[effort](https://platform.claude.com/docs/en/build-with-claude/effort).

## Chi phí LLM (`cost/`)

Nơi duy nhất chứa bảng giá và logic tính tiền cho mọi provider, dùng chung cho `ocr`,
`mapping`, `classification`, `pipeline`. Model nhận ra theo tiền tố tên. Thêm model mới:
thêm một dòng vào `PRICES` (Gemini) hoặc `CLAUDE_PRICES` (Claude) trong `cost/pricing.py`.

```python
from cost import cost_from_response, sum_costs

cost = cost_from_response(response.model_dump(mode="json"), fallback_model=model)
total = sum_costs([cost_ocr, *cost_mapping_calls])  # total_usd, tokens, complete, unpriced_calls
```

Đây là ước tính theo giá niêm yết (Standard paid tier), không phải hóa đơn; không gồm
thuế, credits hoặc phí tool. `total_usd` của `sum_costs` chỉ gồm lời gọi đã định giá;
`complete=False` nếu có lời gọi chưa định giá. Model chưa có bảng giá hoặc response thiếu
usage có `total_usd: null`.

**Gemini** ([pricing](https://ai.google.dev/gemini-api/docs/pricing), kiểm tra 08/10/2026),
USD / 1 triệu token. Thinking tính theo giá output; cached tokens được trừ khỏi input thường.
Phí lưu context cache của mapping được ước tính riêng và cộng vào job tạo cache.

| Model | Input | Cached input | Output/thinking |
|---|---|---|---|
| `gemini-3.6/3.7/3.8-flash` đến 31/12/2026 | 0.75 | 0.075 | 3.75 |
| `gemini-3.6/3.7/3.8-flash` từ 01/01/2027 | 1.50 | 0.15 | 7.50 |
| `gemini-3.5-flash-lite` đến 31/12/2026 | 0.30 | 0.03 | 2.50 |

**Claude** ([pricing](https://platform.claude.com/docs/en/about-claude/pricing), kiểm tra
01/11/2026), USD / 1 triệu token. Thinking nằm chung trong output (`total_thought_tokens` luôn 0).

| Model | Input | Ghi cache 5 phút / 1 giờ | Đọc cache | Output |
|---|---|---|---|---|
| `claude-opus-5-5` | 4 | 5 / 8 | 0.20 | 20 |
| `claude-sonnet-5-5` | 2 | 2.50 / 4 | 0.10 | 10 |
| `claude-haiku-5-5` (prompt ≤ 100K token) | 0.10 | 0.125 / 0.20 | 0.01 | 0.50 |
| `claude-haiku-5-5` (prompt > 100K token) | 0.50 | 0.625 / 1 | 0.05 | 2.50 |

## OCR và mapping độc lập / tích hợp

OCR trả `RepairEstimate` đã validate; lỗi API/schema được raise cho luồng gọi xử lý.
API không tự load `.env`; CLI vẫn load `.env`. `LlmClients` do caller quản lý.

```python
from llm import LlmClients, default_models
from ocr import extract_repair_estimate
from mapping import CodebookMapper, LlmMatcher

with LlmClients(timeout=180) as llm:  # API key lấy từ environment theo provider
    ocr_model, mapping_model = default_models("claude")  # hoặc tên model cụ thể
    estimate = extract_repair_estimate("data/img/bao_gia.jpg", model=ocr_model, backend=llm.backend(ocr_model))
    mapper = CodebookMapper(matcher=LlmMatcher(llm.backend(mapping_model), mapping_model))
    mappings = mapper.map_repair_estimate(estimate)
    for item, mapped in zip(estimate.items, mappings):
        print(item.raw_name, [row.code for row in mapped.matches])
```

Mapping dùng duy nhất `data/codebook/codebook.xlsx` (các cột `MA`, `TEN`, `item_type`)
và một `MAPPING_INSTRUCTION` chung cho mọi provider. Material/labor/paint được gộp chung batch,
không gọi riêng theo category hoặc part/service. Payload `names` là danh sách
`{name, item_type}` duy nhất; không gửi mã MA.

- `material`: exact với tên thuộc part trước; chưa khớp thì gửi LLM cùng các công việc.
- `labor`/`paint`: có bộ phận trong raw_name thì map part; không có mới map service.
  Giữ đúng trái/phải, không tìm thấy bộ phận thì unmatched, không chuyển sang dịch vụ chung.

Tối đa 50 item chưa exact mỗi batch; cache loại các item đã có kết quả trước khi gọi.
Vì vậy không quá 50 item cần mapping thì tối đa một request LLM cho bước mapping
(OCR vẫn là bước riêng). Nhiều hơn 50 item vẫn chia batch. Trả đúng thứ tự đầu vào.
Model trả name và item_type; code kiểm tra tên thuộc đúng loại rồi tra tất cả mã
trùng tên trong loại đó. Material không được trả service. Null trả unmatched.
`metrics.mapping.llm_count`/`llm_calls` là tổng số item/lời gọi AI của run (mọi provider).
Khóa cache kết quả bao gồm model, provider, prompt chung và tên/loại codebook.

```bash
# OCR độc lập
python -m ocr '/path/to/bao_gia.pdf'
# Mapping từ kết quả OCR, bật LLM fallback mặc định (<PROVIDER>_MAPPING_MODEL, --provider hoặc --model)
python -m mapping '/path/to/result.json' --output outputs/mapped.json
# Test exact offline, không cần key
python -m mapping '/path/to/result.json' --exact-only --output outputs/exact.json
# Unit tests offline (client giả: tests/claude_fake.py, tests/gemini_fake.py)
python -m pytest -q
```

CLI mapping giữ các trường OCR và thêm `mapping` vào mỗi item, theo đúng thứ tự.
`mapping.matches` chứa `code`, `name`, `row`, `item_type`; không loại các dòng trùng.
File `.calls.json` bên cạnh output lưu response/usage và latency từng lời gọi
LLM, kể cả khi mapping thất bại. Chạy lại cùng `--output` sẽ ghi đè file đó.
`CodebookMapper()` không truyền matcher (hoặc CLI `--exact-only`) chỉ exact vật tư;
nhân công/sơn trả unmatched vì cần LLM xác định bộ phận/dịch vụ.

## Cache phần codebook của mapping

Prompt mapping gồm phần tĩnh (`MAPPING_INSTRUCTION` + danh sách unique `{name, item_type}`
của `codebook.xlsx`) và phần `items` thay đổi theo từng job. Mỗi provider tự cache phần tĩnh
theo cơ chế của mình; mỗi request chỉ gửi mới `items`. Cả hai độc lập với cache kết quả
Redis/Mongo (option API `use_cache`). Token đọc từ cache nằm trong
`cost.tokens.total_cached_tokens`.

### Gemini: explicit context cache

Khi có item cần gọi Gemini, tạo hoặc dùng lại cache Google (`cachedContents`) chứa
instruction + codebook; request gửi items + `cached_content` + schema đầu ra.
Cấu hình server qua `.env` (không phải option UI):

```dotenv
MAPPING_CONTEXT_CACHE_ENABLED=true
MAPPING_CONTEXT_CACHE_TTL_SECONDS=1800
```

Mặc định bật, TTL 30 phút. Cache tạo khi cần và không tự gia hạn. Đổi model, prompt,
danh sách tên/loại, schema hoặc API key tạo cache mới; cache cũ tự hết hạn trên Google.
Registry và khóa tạo nằm trong `outputs/.../.gemini_context_cache/`, dùng chung volume
outputs giữa các job/worker trên cùng host. Xóa Mongo/Redis không xóa context cache.

Lỗi tạo context cache thì cảnh báo và gửi đầy đủ codebook bằng generateContent.
Cache Google bị xóa sớm (404) thì bỏ registry và retry một lần bằng đầy đủ context.
`mapping.calls.json` và `metrics.mapping.context_cache` ghi tên cache, expire_time,
created, token_count, ttl_seconds và storage_estimated_usd. Phí lưu
`context_cache_storage_usd` được ước tính cho toàn bộ TTL, cộng một lần vào cost của job
tạo cache (kể cả khi request mapping sau đó lỗi); job dùng lại không cộng lần nữa.

Tài liệu: [Explicit caching](https://ai.google.dev/gemini-api/docs/generate-content/caching).

### Claude: prompt caching

System prompt mapping gồm hai khối: instruction và codebook; khối codebook có `cache_control`
TTL 1 giờ. Claude cache theo prefix: mọi request (mọi job/worker) có cùng prompt + codebook
đọc lại từ cache. Không cần tạo/đăng ký cache, không có phí lưu theo thời gian; mỗi lần
trúng cache được gia hạn TTL. Đổi model, prompt hoặc codebook làm đổi prefix nên tự ghi
cache mới. Ghi cache 1 giờ tốn 2x giá input, đọc cache 0.05x (Opus/Sonnet 5.5) hoặc 0.1x
(Haiku 5.5). Token ghi cache nằm trong `total_cache_write_tokens`; `report.txt` in cả đọc và ghi.

Với Haiku 5.5, độ dài prompt tính cả token đọc/ghi cache: codebook đầy đủ (~98K token) có
thể đẩy request qua mốc 100K token và chịu giá cao hơn.

Tài liệu: [prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).

## Lưu và xem kết quả bằng MongoDB

Chạy pipeline và tự động lưu DB: `./run_repair_estimate_pipeline.sh data/img/ --save-mongodb`.
Option này dùng được với cả file và folder. Pipeline kiểm tra DB trước khi gọi
LLM. Nếu ghi DB thất bại sau xử lý, JSON vẫn còn để import lại bằng lệnh bên dưới.

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
- Form field `engine` (không bắt buộc, **cố ý không ghi trên Swagger công khai**): chọn engine AI cho cả OCR
  và mapping. Chỉ BE của AICycle biết giá trị: `1` = Gemini, `5` = Claude (khớp `AICycle 1.x` / `5.x` ở demo);
  bỏ trống = `LLM_PROVIDER` của server. Giá trị lạ (kể cả `gemini`/`claude`) trả `422 INVALID_REQUEST`,
  engine chưa bật trả `422 ENGINE_DISABLED`; thông báo lỗi và Swagger không chứa tên hãng. Response có thêm
  `engine` (bí danh đã dùng) để BE log/đối soát chi phí; bỏ trường này nếu chuyển tiếp cho khách cuối.
  Bí danh khai báo ở cột `alias` của `llm.PROVIDERS`.
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
Không đưa Anthropic API key hoặc secret vào JavaScript.

Nginx giới hạn body 26 MiB (API vẫn giới hạn file 25 MiB), tạo job 20 request/phút/IP
với burst 2 và tối đa 10 kết nối/IP. `/docs`, `/redoc`, `/openapi.json` và `/health/`
không truy cập qua gateway. FastAPI không publish cổng host; MongoDB/Mongo Express
chỉ bind localhost. Nginx tự cập nhật IP upstream API qua Docker DNS mỗi 5 giây,
kể cả khi API container được recreate riêng.

Cấu hình hiện tại là HTTP cho LAN. Trước khi public Internet, cần domain và TLS
(HTTPS tại gateway phía trước hoặc thêm server TLS vào Nginx); Basic Auth qua HTTP
không mã hóa thông tin đăng nhập. Chưa có quota tổng chi phí LLM hoặc giới hạn queue.
Chạy kiểm tra XSS: `node tests/test_demo_xss.js`.

UI demo nằm tại `/demo` trên cùng địa chỉ API (ví dụ `http://<IP_ADDRESS>:8000/demo`).
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
OCR cache hit có `cost: null` vì không gọi LLM. Các document đã lưu theo
`mapping_metrics` và kết quả job Redis cũ không tự chuyển sang cấu trúc mới.

API luôn chạy OCR rồi mapping (exact match + LLM/cache fallback).
Các option public: `provider` (`gemini` | `claude`), `ocr_model`, `mapping_model`, `use_cache`,
`save_mongodb`, `timeout_seconds` (10–600). Một job chỉ dùng một provider:

- Không gửi `provider` thì suy ra từ model đã chọn; không chọn gì thì dùng `LLM_PROVIDER`.
- Model bỏ trống lấy model mặc định của provider (`<PROVIDER>_OCR_MODEL`, `<PROVIDER>_MAPPING_MODEL`).
- Lỗi `422`: `MODEL_NOT_ALLOWED` (model ngoài `VRE_API_ALLOWED_MODELS`), `MODEL_PROVIDER_MISMATCH`
  (model OCR/mapping khác provider, hoặc khác `provider` đã gửi), `PROVIDER_DISABLED`.

Provider `LLM_PROVIDER` luôn bật; provider còn lại chỉ bật khi có API key (trong env hoặc `.env`,
API không nạp secret) **và** `VRE_API_ALLOWED_MODELS` có model của nó. Model mặc định của provider
được bật phải nằm trong allowlist, tên model không thuộc provider nào thì API báo lỗi khi khởi động.
Bỏ trống allowlist = model mặc định của các provider có key. Secret, URI, output/codebook/prompt
path chỉ lấy từ server. `demo-config` trả `providers` (provider mặc định đứng đầu) kèm model của từng provider.

`VRE_API_SHOW_AI_OPTIONS=true` hiện ở demo dropdown engine (chỉ khi bật từ 2 provider), model và
reasoning; danh sách model lọc theo engine đang chọn. `false` ẩn và luôn dùng cấu hình server.
Engine đặt tên theo số hiệu model đã đổi: `AICycle 1.x` = Gemini, `AICycle 5.x` = Claude.
Demo không hiện tên thật của model: `gemini-3.8-flash` → `aicycle-1.8-swift`,
`gemini-3.5-flash-lite` → `aicycle-1.5-swift-mini`, `claude-opus-5-5` → `aicycle-5.5-ultra`,
`claude-sonnet-5-5` → `aicycle-5.5-pro`, `claude-haiku-5-5` → `aicycle-5.5-air`
(giá trị gửi API vẫn là tên thật).

Biến cấu hình server tùy chọn:

```text
VRE_API_ALLOWED_MODELS=gemini-3.8-flash,gemini-3.5-flash-lite,claude-haiku-5-5,claude-sonnet-5-5
VRE_API_HOST=127.0.0.1
VRE_API_PORT=8000
VRE_API_QUEUE=repair-estimate
VRE_API_UPLOAD_DIR=/tmp/vre-api/uploads
VRE_API_MAX_UPLOAD_BYTES=26214400
VRE_API_RESULT_TTL_SECONDS=604800
VRE_API_JOB_TIMEOUT_SECONDS=2100
VRE_API_WORKERS=10   # số job chạy song song trong container worker (compose mặc định 10)
```
