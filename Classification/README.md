# Phân loại Codebook bằng Gemini

Gemini quyết định toàn bộ nhãn; không có rule từ khóa tự gán category.
File Excel gốc được giữ nguyên. Kết quả nằm trong thư mục riêng của từng lần chạy.

## Batch và định dạng gửi

Mặc định **100 dòng/request**, tuần tự một request tại một thời điểm.
Codebook hiện tại 3.346 dòng có 3.269 tên unique, tạo 33 request. Đây là cấu hình khởi đầu,
chưa benchmark tối ưu. Thử 50 nếu gặp timeout/output dài; thử 200 sau khi
đánh giá chất lượng và độ trễ trên dữ liệu đại diện.

Mỗi request độc lập gửi prompt + JSON Schema + JSON text gọn:

```json
{"names":["Áo cửa sau bên phải (vỏ cánh cửa)"]}
```

Không gửi file Excel, ảnh hoặc lịch sử request. Không gửi Group vì nhóm hệ
thống xe không thể hiện chắc chắn công/vật tư. Không gửi mã. Chỉ khử trùng tên giống nguyên văn, không bỏ dấu, đổi hoa/thường
hoặc chuẩn hóa khoảng trắng. Gemini trả lại name nguyên văn; kết quả được
áp cho mọi dòng cùng tên, giữ nguyên mã Excel. “Batch” ở đây là chia nhóm dòng trong
request đồng bộ, không phải dịch vụ Google Batch API và không mặc định có
chiết khấu Batch API.

## Hai nhãn đầu ra

Chỉ trả `name` nguyên văn và `item_type`:

- `part`: tên bộ phận, linh kiện, cụm/hệ thống, vật tư tiêu hao, dầu, keo, hóa chất.
- `service`: công việc/dịch vụ: sửa chữa, tháo lắp, thay thế, sơn, làm đồng,
  vệ sinh, căn chỉnh... Công kết hợp đồng+sơn cũng là service.

Ví dụ: Đèn pha -> part; Thay đèn pha -> service; Công tắc mở cửa -> part;
Dung dịch vệ sinh -> part; Vệ sinh nội thất -> service.

Không trả category, operations, vehicle_part, confidence, needs_review hoặc reason.
Excel thêm cột `item_type` ở cột D (thứ 4), sau MA/TEN/Group;
các cột có sẵn từ D trở đi được dịch sang phải. Nhãn không phụ thuộc các trường OCR.
Với đúng hai nhóm, các mục VAT/tổng tiền/hồ sơ/ảnh/video/tổng hợp nhiều loại
được quy ước vào service vì không phải tên vật thể. Quy ước này không có nghĩa
chúng là công sửa chữa thực tế; bước mapping vẫn cần chọn đúng ý nghĩa tên.

Chỉ phân loại một lần mỗi tên; tên trùng khác mã nhận cùng nhãn.

## Chạy

Từ thư mục BGSC:

```bash
conda activate da_gemini
pip install -r Classification/requirements.txt
python Classification/classify.py --dry-run

# Thử 1 batch (100 dòng), lưu checkpoint để chạy tiếp.
python Classification/classify.py --run-dir Classification/outputs/trial_binary100 --max-batches 1

# Chạy tiếp tất cả batch còn lại.
python Classification/classify.py --run-dir Classification/outputs/trial_binary100 --resume

# Hoặc chạy toàn bộ trong tmux, lưu cả console log.
tmux new -s codebook-classification
set -o pipefail
python -u Classification/classify.py --run-dir Classification/outputs/full_binary100 2>&1 | tee /tmp/codebook-classification.log
# Ctrl+B rồi D để tách; tmux attach -t codebook-classification để quay lại.
```

Mặc định đọc `src/.env` (`GEMINI_API_KEY`, `GEMINI_MODEL`); biến môi trường
đã đặt có ưu tiên hơn file. `--model` ưu tiên hơn `GEMINI_MODEL`.
Các tùy chọn khác: `--input FILE.xlsx`, `--sheet NAME`, `--batch-size 100`,
`--timeout 180`, `--env-file FILE`. `--max-batches` đếm batch mới, không đếm
checkpoint đã tồn tại. Dry-run đọc và kiểm tra dữ liệu, không cần key và
không gọi API hoặc tạo output.

Khi resume, phải giữ nguyên nội dung Excel, sheet, model, batch size,
prompt và schema. Thay cấu hình thì dùng run-dir mới. Các run cũ gửi mã và chỉ trả category
không thể resume bằng pipeline mới; giữ artifact cũ và chạy một run mới. Không chạy hai tiến
trình cùng ghi vào một run-dir. Không tự retry request lỗi; sửa nguyên nhân
và resume. Request đã gửi nhưng chưa lưu checkpoint có thể phải gọi lại
và phát sinh thêm chi phí.

## Artifact và đo lường

- `manifest.json`: hash input, model, batch size, hash prompt/schema.
- `batch_00001.json`: checkpoint đã qua kiểm tra schema, đủ mã và không trùng.
- Các file có timestamp `.input.json`, `.response.json`, `.metrics.json`:
  lưu input, response gốc, usage, thời gian và ước tính chi phí từng lần gọi.
- `unique_results.json`: một kết quả cho mỗi tên unique.
- `results.json`: log đầy đủ từng mã/tên, dòng Excel và item_type được ghép lại.
- `summary.json`: số dòng Excel đã ghép, số tên unique đã phân loại, thống kê
  item_type theo tên unique, thời gian gọi
  API cộng dồn và chi phí đã xác định. Chi phí dùng bảng giá trong `src/cost.py`;
  đây là ước tính, cần đối chiếu bảng giá hiện hành khi vận hành.
- `classified.xlsx`: chỉ xuất tên này khi tất cả dòng hoàn tất.
- `classified.partial.xlsx`: snapshot khi giới hạn batch hoặc gặp lỗi;
  các dòng chưa xử lý được xác định qua `results.json`. Snapshot partial cũ
  có thể còn sau khi resume hoàn tất; khi đó dùng `classified.xlsx`.

Model không có bảng giá hoặc response thiếu usage sẽ được tính vào
`unpriced_responses`; `known_estimated_cost_usd` chỉ là tổng phần đã biết.
Request lỗi không trả response/usage có thể phát sinh phí không đo được.
Lệnh lỗi trả exit code 1; dừng theo max-batches trả 0 với complete=false.

Kiểm tra chất lượng bằng mẫu gồm vật tư, công, sơn, tên trùng, tên có từ
“công tắc”, “dung dịch vệ sinh”, “vật tư sơn”, dòng tổng và công hỗn hợp.
Đánh giá bằng đối chiếu nhãn với ý nghĩa từng tên trong mẫu kiểm tra.

## Kiểm tra tự động

```bash
python -m unittest discover -s Classification -p 'test_*.py'
```

Tài liệu API: [Gemini structured outputs](https://ai.google.dev/gemini-api/docs/structured-output).
