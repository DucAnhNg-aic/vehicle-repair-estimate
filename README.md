# Đọc báo giá sửa chữa bằng Gemini

Chạy với Python 3.11 trong conda `da_gemini`:

```bash
conda activate da_gemini
cd /home/ubuntu/ducanh/BGSC
python -m pip install -r requirements.txt
python -m ocr
```

Script đọc `GEMINI_API_KEY`, `GEMINI_MODEL` từ `ocr/.env` và sử dụng
biến chuỗi `PROMPT` trong `prompt.py`. Biến môi trường đã export được ưu tiên hơn `.env`;
`--model` được ưu tiên cao nhất. `.env` hỗ trợ các dòng đơn giản `KEY=value`
hoặc `KEY="value"`. Mặc định chạy ảnh JPG duy nhất trong `img/`.

Sửa nội dung hướng dẫn trong chuỗi `PROMPT` của `prompt.py`.
`--prompt /path/to/custom_prompt.py` dùng module Python khác có biến `PROMPT`;
`--prompt /path/to/prompt.txt` vẫn hỗ trợ prompt dạng text UTF-8.
File `.py` được thực thi để lấy `PROMPT`, vì vậy chỉ dùng module do bạn kiểm soát.

```bash
python -m ocr --dry-run
python -m ocr --list-models
python -m ocr '/path/to/bao gia.pdf' --model gemini-3.6-flash
python -m ocr 'img/TAILIEUTHIETHAIXE64A33283-20260828-071310.md'
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
hay paid tier; không bao gồm thuế, credits hoặc phí lưu cache/tool.

Bảng giá `gemini-3.6-flash` kiểm tra ngày 01/10/2026 tại
[Google](https://ai.google.dev/gemini-api/docs/pricing#gemini-3.6-flash):
đến 31/12/2026 là $0.75 input, $0.075 cached input, $3.75 output/thinking
trên một triệu token. Từ 01/01/2027 lần lượt là $1.50, $0.15, $7.50.
Model chưa có bảng giá hoặc response thiếu usage sẽ có `total_usd: null`.

## Endpoint được chọn

- `POST https://generativelanguage.googleapis.com/v1beta/interactions`:
  phù hợp đọc ảnh/PDF và trả structured JSON; Google khuyến nghị cho dự án mới.
  Dùng `store=False` vì mỗi lần chạy là một yêu cầu độc lập.
- `models/{model}:generateContent`: vẫn được hỗ trợ, nhưng script này dùng
  Interactions giống code hiện có ở `GPLX_OCR/Gemini`.
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

OCR trả `Quotation` đã validate; lỗi API/schema được raise cho luồng gọi xử lý.
API không tự load `.env`; CLI vẫn load `ocr/.env`. Client truyền vào do caller quản lý.

```python
from google import genai
from ocr import extract_document
from mapping import CodebookMapper, GeminiMatcher

with genai.Client() as client:  # GEMINI_API_KEY từ environment
    quotation = extract_document("img/bao_gia.jpg", model="your-model", client=client)
    mapper = CodebookMapper(matcher=GeminiMatcher(client, "your-model"))
    mappings = mapper.map_quotation(quotation)
    # Hoặc mapper.map_item(quotation.items[0])
    for item, mapped in zip(quotation.items, mappings):
        print(item.raw_name, [row.code for row in mapped.matches])
```

Mapping dùng `raw_name` so sánh **nguyên văn** với `TEN` (phân biệt hoa/thường,
không bỏ dấu/trim). `labor` và `paint` dùng service codebook; `material` dùng
part codebook. Exact match trả tất cả dòng có cùng tên, không gọi Gemini.
Nếu không exact, gửi item OCR và toàn bộ danh sách tên duy nhất của codebook
được chọn cho Gemini. Gemini chọn một tên hoặc null; chương trình tra lại tất cả
mã `MA` của tên đó. Tên ngoài danh sách/API lỗi/schema lỗi được raise;
null trả `unmatched` với `matches=[]`. Không tự chọn gần đúng khi Gemini lỗi.

```bash
# OCR độc lập
python -m ocr '/path/to/bao_gia.pdf'
# Mapping từ kết quả OCR, bật Gemini fallback mặc định
python -m mapping '/path/to/result.json' --output outputs/mapped.json
# Test exact offline, không cần key
python -m mapping '/path/to/result.json' --exact-only --output outputs/exact.json
# Unit tests offline (mock Gemini)
python -m unittest discover -s tests -v
```

CLI mapping giữ các trường OCR và thêm `mapping` vào mỗi item, theo đúng thứ tự.
`mapping.matches` chứa `code`, `name`, `sheet`, `row`; không loại các dòng trùng.
File `.calls.json` bên cạnh output lưu response/usage và latency từng lời gọi
Gemini, kể cả khi mapping thất bại. Chạy lại cùng `--output` sẽ ghi đè file đó.
`CodebookMapper()` không truyền matcher chỉ chạy exact, phù hợp test offline.
