"""Client Claude giả cho unit test: ghi lại tham số ``beta.messages.stream`` và trả message dựng sẵn."""
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

MODEL = "claude-opus-5-5"


def message(payload, *, usage=None, model=MODEL, stop_reason="end_turn", stop_details=None):
    """Message Claude trả JSON ``payload`` (hoặc text thô nếu là str)."""
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    data = {"model": model, "stop_reason": stop_reason, "stop_details": stop_details,
            "usage": usage or {}, "content": [{"type": "text", "text": text}]}
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                           model_dump=lambda **kwargs: {k: v for k, v in data.items() if v is not None})


class FakeClaude:
    """Request OCR (schema RepairEstimate) và mapping được tách riêng theo schema gửi đi.

    ``replies[kind]``: một message dùng cho mọi lần gọi, hoặc list message trả lần lượt.
    """

    def __init__(self, ocr=None, mapping=None):
        self.replies = {"ocr": ocr, "mapping": mapping}
        self.calls = {"ocr": [], "mapping": []}
        self.files = Mock()
        self.closed = False
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        kind = "ocr" if kwargs["output_config"]["format"]["schema"].get("title") == "RepairEstimate" else "mapping"
        self.calls[kind].append(kwargs)
        reply = self.replies[kind]
        reply = reply.pop(0) if isinstance(reply, list) else reply
        if reply is None:
            raise AssertionError(f"Không có reply giả cho request {kind}")
        return nullcontext(SimpleNamespace(get_final_message=lambda: reply))

    def call_count(self) -> int:
        return len(self.calls["ocr"]) + len(self.calls["mapping"])

    def sent_items(self, index: int = -1) -> list[dict]:
        """``items`` đã gửi trong request mapping thứ ``index``."""
        return json.loads(self.calls["mapping"][index]["messages"][0]["content"][0]["text"])["items"]

    def sent_names(self, index: int = -1) -> list:
        """Danh sách tên codebook nằm trong system prompt (khối được cache)."""
        return json.loads(self.calls["mapping"][index]["system"][1]["text"])["names"]

    def close(self):
        self.closed = True
