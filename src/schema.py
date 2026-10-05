"""Output contract; null values remain null.

Mô tả từng trường nằm trong Field(description=...) và được gửi cho Gemini
qua JSON schema. Quy tắc chung (nhiệm vụ, định dạng số, ví dụ, không bỏ dòng)
nằm trong prompt.py.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def _amount(cat: str) -> str:
    return (f'Nếu category = "{cat}": giá trị phù hợp của dòng, chọn theo mục '
            f'"Chọn giá trị tiền cho material_amount / labor_amount / paint_amount" trong hướng dẫn. '
            f'Nếu category khác: 0.')


def _rate(cat: str, src: str) -> str:
    return f'Nếu category = "{cat}": {src} nếu có, ngược lại null. Nếu category khác: 0.'


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class Document(StrictModel):
    quotation_number: str | None = Field(
        description="Số báo giá/số phiếu đúng nguyên văn trong tài liệu. Null nếu không có.")
    date: str | None = Field(
        description="Ngày (và giờ nếu có) của báo giá, giữ nguyên văn như trong tài liệu, "
                    "không đổi định dạng. Null nếu không có.")
    vehicle_plate: str | None = Field(
        description="Biển số xe đúng nguyên văn trong tài liệu. Null nếu không có.")
    vehicle_model: str | None = Field(
        description="Hiệu/loại xe đúng nguyên văn trong tài liệu. Null nếu không có.")
    vin: str | None = Field(
        description="Số khung (VIN) đúng nguyên văn trong tài liệu. Null nếu không có.")


class QuotationItem(StrictModel):
    raw_section: str | None = Field(
        description='Tên section đúng nguyên văn trong tài liệu, ví dụ: "Phần công dịch vụ", '
                    '"Công S/C Sơn", "Phụ tùng / vật tư", "Phần phụ tùng/dầu mỡ/vật tư".')
    section_type: Literal["service", "paint_service", "material", "unknown"] = Field(
        description="Loại section. Không được chỉ dựa vào tên section nếu nội dung thực tế cho thấy khác.")
    raw_code: str | None = Field(
        description="Mã của dòng (mã hạng mục/mã phụ tùng) đúng nguyên văn trong tài liệu. "
                    "Null nếu dòng không có mã.")
    raw_name: str = Field(
        description="Tên hạng mục/công việc đúng nguyên văn trong tài liệu.")
    operation: Literal[
        "paint", "repair", "bodywork", "remove_install", "replace",
        "weld", "reshape", "polish", "clean", "other",
    ] | None = Field(description="Loại thao tác chính.")
    vehicle_part: str | None = Field(
        description='Bộ phận xe được nhắc đến trong tên công việc, ví dụ: "cản trước", "cửa sau phải", '
                    '"đèn pha phải", "mặt ca lăng", "vè trước trái". '
                    "Nếu không có bộ phận xe cụ thể thì trả về null.")
    item_name: str = Field(
        description="Quy tắc: 1. Nếu tên công việc chứa một bộ phận xe rõ ràng, item_name là tên bộ phận đó. "
                    '2. Nếu tên công việc không chứa bộ phận xe cụ thể, ví dụ "Tháo lắp phục vụ sơn", '
                    '"Công tháo lắp", thì item_name là tên công việc đã được chuẩn hóa nhẹ, '
                    "không được tự gán sang bộ phận xe.")
    category: Literal["material", "labor", "paint"] = Field(
        description='"material": phụ tùng, vật tư, dầu mỡ, linh kiện thay thế. '
                    '"labor": công việc sửa chữa, làm đồng, tháo lắp, hàn, chỉnh hình, thay thế, '
                    "đánh bóng hoặc các công việc khác không phải sơn. "
                    '"paint": công việc sơn thực tế. '
                    'Lưu ý: có chữ "sơn" trong tên chưa chắc là loại "paint". Ví dụ: '
                    '"Sơn cản trước" -> paint; "Sơn cửa sau phải" -> paint; '
                    '"Tháo lắp phục vụ sơn" -> labor; "Công tháo lắp phục vụ sơn" -> labor; '
                    '"Chuẩn bị phục vụ sơn" -> labor, trừ khi nội dung thể hiện rõ đây là công đoạn sơn. '
                    'Nếu dòng nằm trong section phụ tùng/vật tư thì ưu tiên phân loại là "material".')
    unit: str | None = Field(
        description='Đơn vị tính đúng như trong tài liệu, ví dụ "LẦN", "CÁI". Null nếu không có.')
    quantity: float | None = Field(
        description="Số lượng của dòng. Null nếu không có.")
    unit_price: float | None = Field(
        description="Đơn giá của dòng (đơn giá trước giảm giá nếu bảng có cột này). Null nếu không có.")
    discount_rate: float | None = Field(
        description="Tỷ lệ giảm giá của dòng, dạng số (10% -> 10). Null nếu không có.")
    discount_amount: float | None = Field(
        description="Số tiền giảm giá của dòng như ghi trong tài liệu. Null nếu không có.")
    tax_rate: float | None = Field(
        description="Thuế suất của dòng, dạng số (8% -> 8). Nếu tài liệu chỉ có VAT tổng toàn báo giá "
                    "nhưng không ghi thuế từng dòng, không tự gán thuế từng dòng ở bước extraction; "
                    "để tax_rate = null cho dòng đó.")
    tax_amount: float | None = Field(
        description="Số tiền thuế của dòng như ghi trong tài liệu. Null nếu không có.")
    amount: float | None = Field(
        description="Thành tiền của dòng như ghi trong tài liệu, không tự tính. Null nếu không có.")
    material_amount: float | None = Field(description=_amount("material"))
    labor_amount: float | None = Field(description=_amount("labor"))
    paint_amount: float | None = Field(description=_amount("paint"))
    material_discount_rate: float | None = Field(description=_rate("material", "discount_rate"))
    labor_discount_rate: float | None = Field(description=_rate("labor", "discount_rate"))
    paint_discount_rate: float | None = Field(description=_rate("paint", "discount_rate"))
    material_tax_rate: float | None = Field(description=_rate("material", "tax_rate"))
    labor_tax_rate: float | None = Field(description=_rate("labor", "tax_rate"))
    paint_tax_rate: float | None = Field(description=_rate("paint", "tax_rate"))
    source_evidence: str = Field(
        description="Đoạn trích nguyên văn từ tài liệu làm căn cứ cho dòng này "
                    "(ví dụ tên hạng mục như in trên tài liệu).")
    confidence: Literal["high", "medium", "low"] = Field(
        description="Mức tự tin khi đọc dòng này. high: chữ và số đọc rõ, đúng cột. "
                    "medium: có phần mờ hoặc phải suy luận (ví dụ category, operation, vehicle_part). "
                    "low: có trường nghi ngờ (khó đọc, không chắc số tiền thuộc cột nào); "
                    "khi đó giữ raw value, không tự sửa.")


class Quotation(StrictModel):
    document: Document = Field(description="Thông tin chung của báo giá (không phải dòng hạng mục).")
    items: list[QuotationItem] = Field(description="Tất cả các dòng hạng mục hợp lệ trong báo giá.")
