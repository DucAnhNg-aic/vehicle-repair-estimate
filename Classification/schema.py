"""Binary semantic classification of unique Codebook names."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class ClassificationItem(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    name: str = Field(description="Tên đầu vào đúng nguyên văn; không sửa hoặc chuẩn hóa. Dùng để ghép lại các dòng Excel cùng tên.")
    item_type: Literal["part", "service"] = Field(
        description="part: tên bộ phận, linh kiện, cụm/hệ thống hoặc vật tư như dầu, keo, hóa chất. service: công việc/dịch vụ như sửa chữa, tháo lắp, thay thế, sơn, vệ sinh. Trong phân loại hai nhóm, mục hành chính/tổng hợp không phải tên vật thể cũng vào service.")


class ClassificationBatch(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    items: list[ClassificationItem] = Field(description="Đúng một kết quả cho mỗi tên unique đầu vào, giữ nguyên tên.")
