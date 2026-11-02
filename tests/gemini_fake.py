"""Response Gemini giả cho unit test: dùng chung cho ``interactions.create`` (OCR) và ``models.generate_content`` (mapping)."""
import json
from types import SimpleNamespace

MODEL = "gemini-3.8-flash"


def response(payload, *, status="completed", finish_reason="STOP", usage=None):
    """Interaction/GenerateContentResponse trả JSON ``payload``; ``usage`` là usage của Interactions API."""
    text = json.dumps(payload, ensure_ascii=False)
    data = {"status": status, "usage": usage or {}}
    return SimpleNamespace(text=text, output_text=text, status=status,
                           candidates=[SimpleNamespace(finish_reason=finish_reason)],
                           model_dump=lambda **kwargs: data)


def sent_contents(client) -> dict:
    """JSON ``contents`` của lời gọi ``generate_content`` gần nhất."""
    return json.loads(client.models.generate_content.call_args.kwargs["contents"])
