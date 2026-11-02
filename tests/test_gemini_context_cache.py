import json
from datetime import datetime, timedelta, timezone, date
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from google.genai import errors

from llm.gemini import Backend
from llm.gemini_context_cache import ContextCache
from mapping import LlmMatcher
from cost import cost_from_response, estimate_cache_storage, sum_costs
from test_llm_cache import estimate, MemoryCache

MODEL = 'gemini-3.8-flash'
NAMES = {'part': ['Cản trước'], 'service': ['Công tháo lắp']}
CONTEXT = {'names': NAMES}  # ContextCache nhận context dạng dict như backend gửi


def cache_record(name='cachedContents/codebook'):
    return SimpleNamespace(name=name, expire_time=datetime.now(timezone.utc) + timedelta(minutes=30),
                           usage_metadata=SimpleNamespace(total_token_count=69000))


def reply():
    payload = {'selections': [{'index': 0, 'name': 'Cản trước', 'item_type': 'part'}]}
    return SimpleNamespace(text=json.dumps(payload), candidates=[SimpleNamespace(finish_reason='STOP')],
                           model_dump=lambda **kw: {'usage_metadata': {'prompt_token_count': 70000,
                               'cached_content_token_count': 69000, 'candidates_token_count': 20,
                               'thoughts_token_count': 100}})


def matcher(client, path, **kw):
    return LlmMatcher(Backend(client, context_cache_dir=path, context_cache_enabled=True), MODEL, **kw)


def test_context_reused_across_jobs_and_items_only_sent(tmp_path):
    client = Mock()
    client.caches.create.return_value = cache_record()
    client.models.generate_content.return_value = reply()
    first = matcher(client, tmp_path)
    assert first.select_many(estimate(('Sơn cản trước', 'paint')).items, NAMES) == [('part', 'Cản trước')]
    second = matcher(client, tmp_path)
    second.select_many(estimate(('Gò cản trước', 'labor')).items, NAMES)
    assert client.caches.create.call_count == 1
    assert client.models.generate_content.call_count == 2
    sent = client.models.generate_content.call_args.kwargs
    assert set(json.loads(sent['contents'])) == {'items'}
    assert sent['config'].cached_content == 'cachedContents/codebook'
    assert sent['config'].system_instruction is None
    assert sent['config'].http_options.extra_body == {'store': False}
    assert first.calls[0]['context_cache']['created']
    assert not second.calls[0]['context_cache']['created']
    assert first.calls[0]['cost']['context_cache_storage_usd'] == pytest.approx(0.01725)
    assert second.calls[0]['cost'].get('context_cache_storage_usd', 0) == 0
    assert first.calls[0]['cost']['tokens']['total_cached_tokens'] == 69000


def test_expired_and_changed_context_create_new_cache(tmp_path):
    client = Mock()
    client.caches.create.return_value = cache_record()
    cache = ContextCache(client, MODEL, tmp_path)
    cache.acquire('prompt', CONTEXT, {})
    path = next(tmp_path.glob('*.json'))
    data = json.loads(path.read_text())
    data['expire_time'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    path.write_text(json.dumps(data))
    cache.acquire('prompt', CONTEXT, {})
    cache.acquire('changed', CONTEXT, {})
    cache.acquire('changed', {'names': {'part': ['Khác'], 'service': []}}, {})
    cache.acquire('changed', CONTEXT, {'new_schema': True})
    assert client.caches.create.call_count == 5


def test_cache_creation_failure_falls_back_to_full_context(tmp_path):
    client = Mock()
    client.caches.create.side_effect = RuntimeError('unsupported cache')
    client.models.generate_content.return_value = reply()
    m = matcher(client, tmp_path)
    with pytest.warns(RuntimeWarning):
        m.select_many(estimate(('Sơn cản trước', 'paint')).items, NAMES)
    m.select_many(estimate(('Gò cản trước', 'labor')).items, NAMES)
    assert client.caches.create.call_count == 1
    sent = client.models.generate_content.call_args.kwargs
    assert set(json.loads(sent['contents'])) == {'names', 'items'}
    assert sent['config'].system_instruction == m.INSTRUCTION
    assert sent['config'].cached_content is None


def test_remote_deleted_cache_retries_once(tmp_path):
    client = Mock()
    client.caches.create.return_value = cache_record()
    client.models.generate_content.side_effect = [errors.ClientError(404, {'error': {'message': 'gone'}}), reply()]
    m = matcher(client, tmp_path)
    m.select_many(estimate(('Sơn cản trước', 'paint')).items, NAMES)
    assert client.models.generate_content.call_count == 2
    assert client.models.generate_content.call_args.kwargs['config'].cached_content is None
    assert not list(tmp_path.glob('*.json'))
    assert m.calls[0]['context_cache_fallback'] == 'not_found'


def test_result_cache_hit_does_not_create_google_cache(tmp_path):
    client = Mock()
    client.caches.create.return_value = cache_record()
    client.models.generate_content.return_value = reply()
    m = matcher(client, tmp_path, cache=MemoryCache())
    items = estimate(('Sơn cản trước', 'paint')).items
    m.select_many(items, NAMES)
    m.select_many(items, NAMES)
    assert client.caches.create.call_count == client.models.generate_content.call_count == 1
    assert m.cache_hits == 1


def test_blocked_or_truncated_response_is_not_result_cached(tmp_path):
    client = Mock()
    r = reply()
    r.candidates[0].finish_reason = 'MAX_TOKENS'
    client.models.generate_content.return_value = r
    cache = MemoryCache()
    m = LlmMatcher(Backend(client, context_cache_enabled=False), MODEL, cache=cache)
    with pytest.raises(RuntimeError):
        m.select_many(estimate(('Sơn cản trước', 'paint')).items, NAMES)
    assert cache.sets == 0
    assert m.calls[0]['cost']['total_usd'] is not None


def test_generate_content_usage_and_storage_cost():
    data = {'usageMetadata': {'promptTokenCount': 1000, 'cachedContentTokenCount': 800,
                             'candidatesTokenCount': 50, 'thoughtsTokenCount': 100}}
    c = cost_from_response(data, MODEL, date(2026, 10, 8))
    assert c['total_usd'] == pytest.approx((200*.75 + 800*.075 + 150*3.75)/1e6)
    assert estimate_cache_storage(MODEL, 69000, 1800, date(2026, 10, 8)) == pytest.approx(.01725)
    assert estimate_cache_storage('unknown', 69000, 1800) is None
    c['context_cache_storage_usd'] = .01725
    c['total_usd'] += .01725
    assert sum_costs([c])['context_cache_storage_usd'] == .01725


def test_parallel_workers_share_single_creation(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    client = Mock()
    client.caches.create.return_value = cache_record()
    workers = [ContextCache(client, MODEL, tmp_path) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda c: c.acquire('prompt', CONTEXT, {}), workers))
    assert client.caches.create.call_count == 1
    assert sorted(r['created'] for r in results) == [False, True]


def test_cache_storage_recorded_even_if_generation_fails(tmp_path):
    client = Mock()
    client.caches.create.return_value = cache_record()
    client.models.generate_content.side_effect = RuntimeError('network failed')
    m = matcher(client, tmp_path)
    with pytest.raises(RuntimeError):
        m.select_many(estimate(('Sơn cản trước', 'paint')).items, NAMES)
    total = m.cost_summary()
    assert total['total_usd'] == pytest.approx(.01725)
    assert not total['complete']


def test_schema_or_model_or_credentials_isolates_context(tmp_path, monkeypatch):
    client = Mock()
    client.caches.create.return_value = cache_record()
    c = ContextCache(client, MODEL, tmp_path)
    c.acquire('prompt', CONTEXT, {})
    monkeypatch.setenv('GEMINI_API_KEY', 'fake-different-account')
    c.acquire('prompt', CONTEXT, {})
    ContextCache(client, 'gemini-3.6-flash', tmp_path).acquire('prompt', CONTEXT, {})
    assert client.caches.create.call_count == 3


def test_sdk_serializes_raw_json_schema_without_additional_properties_conversion():
    import httpx
    from google import genai
    from google.genai import types
    from mapping.schema import WorkBatchSelection
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={
            'candidates': [{'content': {'role': 'model', 'parts': [{'text': json.dumps({
                'selections': [{'index': 0, 'name': 'Cản trước', 'item_type': 'part'}]})}]},
                'finishReason': 'STOP'}],
            'usageMetadata': {'promptTokenCount': 100, 'candidatesTokenCount': 10},
        })

    with genai.Client(api_key='fake-key', http_options=types.HttpOptions(
        client_args={'transport': httpx.MockTransport(respond)})) as client:
        m = LlmMatcher(Backend(client, context_cache_enabled=False), MODEL, thinking_level='LOW')
        assert m.select_many(estimate(('Sơn cản trước', 'paint')).items, NAMES) == [('part', 'Cản trước')]
    config = requests[0]['generationConfig']
    assert config['responseJsonSchema'] == WorkBatchSelection.model_json_schema()
    assert 'responseSchema' not in config
    assert config['responseJsonSchema']['additionalProperties'] is False
    assert requests[0]['store'] is False
    assert config['thinkingConfig'].get('thinkingLevel', config['thinkingConfig'].get('thinking_level')) == 'LOW', config
