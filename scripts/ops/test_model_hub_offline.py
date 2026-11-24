#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线单测：model-hub 400 model_not_available 自动选模与 fallback 判定。"""
import importlib.util
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent

# --- stubs: 本机无 requests/jwt，只测纯逻辑与 _invoke 流程 ---
if 'jwt' not in sys.modules:
    jwt = types.ModuleType('jwt')
    jwt.encode = lambda *a, **k: ''
    jwt.decode = lambda *a, **k: {}
    sys.modules['jwt'] = jwt
if 'requests' not in sys.modules:
    requests = types.ModuleType('requests')

    class _Session:
        def __init__(self, *a, **k):
            pass

    requests.Session = _Session
    requests.exceptions = types.SimpleNamespace(
        Timeout=type('Timeout', (Exception,), {}),
        ConnectionError=type('ConnectionError', (Exception,), {}))
    sys.modules['requests'] = requests


def load():
    spec = importlib.util.spec_from_file_location('model_hub_under_test', str(HERE / 'model-hub.py'))
    mod = importlib.util.module_from_spec(spec)
    sys.modules['model_hub_under_test'] = mod
    spec.loader.exec_module(mod)
    return mod


def test_short_wechat_blank_backup_retries_once(hub):
    primary = {'id': 'workbuddy-deepseek', 'name': 'test', 'protocol': 'chat',
               'base_url': hub.GATEWAY_URL, 'fallback_to': 'sensenova-6.8-flash-lite'}
    backup = {'id': 'sensenova-6.8-flash-lite', 'name': 'test backup', 'protocol': 'chat'}
    hub.profiles = lambda: [primary, backup]
    hub.cooldown_until = lambda _profile: 0
    hub.set_cooldown = lambda *_args: None
    hub.route_log = lambda _record: None
    called = []

    def invoke(profile, body, _allow_auto=True, *, retry_timeout=None):
        called.append((profile['id'], body['max_tokens'], retry_timeout))
        if profile is primary:
            raise hub.Fallbackable('http_503')
        text = '' if body['max_tokens'] == 160 else '{"participate":true,"text":"fixture","emotion":"neutral","intensity":1}'
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': text}}]}

    hub._invoke = invoke
    result = hub.complete({'model': primary['id'], 'max_tokens': 160,
                           'response_format': {'type': 'json_object'},
                           'messages': [{'role': 'system', 'content':
                               '本轮来自 wechat 客户端适配层。[QQ_PARTICIPATION] 输出 participate、text、emotion、intensity。'}]})
    assert 'fixture' in result['choices'][0]['message']['content']
    assert called == [(primary['id'], 160, None), (backup['id'], 160, None),
                      (backup['id'], 256, (1.25, 3.75))]
    print('short_wechat_blank_backup_retries_once: PASS')


def main():
    hub = load()

    # 1) 错误识别
    assert hub._model_unavailable(hub.UpstreamError(400, detail='{"error":{"code":"model_not_available"}}'))
    assert hub._model_unavailable(hub.UpstreamError(404, detail='model_not_found'))
    assert not hub._model_unavailable(hub.UpstreamError(400, detail='invalid request'))
    assert not hub._model_unavailable(hub.UpstreamError(500, detail='model_not_available'))
    print('model_unavailable: 4/4 PASS')

    # 2) 选模
    assert hub._pick_model({'model': 'gemini-flash-lite'},
                           ['gemini-auto', 'gemini-2.5-flash', 'sensenova-6.8-flash-lite']) == 'gemini-auto'
    assert hub._pick_model({'model': 'gemini-flash-lite'},
                           ['gemini-2.5-flash', 'gemini-auto']) == 'gemini-auto'
    assert hub._pick_model({'model': 'gemini-flash-lite'},
                           ['gemini-2.5-flash', 'text-embedding-3']) == 'gemini-2.5-flash'
    assert hub._pick_model({'model': 'deepseek-v4.1-flash'},
                           ['sensenova-6.8-flash-lite', 'gemini-auto']) == 'gemini-auto'
    assert hub._pick_model({'model': 'deepseek-v4.1-flash'},
                           ['sensenova-6.8-flash-lite']) == ''
    assert hub._pick_model({'model': 'gemini-flash-lite'}, []) == ''
    print('pick_model: 6/6 PASS')

    # 3) _invoke 全流程：400 -> 查模型 -> 自动重试成功
    tmp = Path(tempfile.mkdtemp())
    key = tmp / 'key'
    key.write_text('x')
    profile = {'id': 'gemini-web', 'model': 'gemini-flash-lite',
               'base_url': 'http://127.0.0.1:8085/v1', 'protocol': 'chat',
               'key_file': str(key), 'proxy': False, 'idle_effort': False}
    calls = []

    def fake_http(method, url, key=None, proxy=False, body=None, form=None,
                  stream=False, timeout=(10, 150)):
        model = (body or {}).get('model') if isinstance(body, dict) else None
        calls.append((method, url.rsplit('/', 1)[-1], model))
        if method == 'GET' and url.endswith('/models'):
            return {'data': [{'id': 'gemini-auto'}, {'id': 'gemini-2.5-flash'}]}
        if method == 'POST' and model == 'gemini-flash-lite':
            raise hub.UpstreamError(400, detail='{"error":{"code":"model_not_available"}}')
        return {'choices': [{'message': {'role': 'assistant', 'content': 'pong'}}],
                'usage': {'total_tokens': 1}}

    hub.http = fake_http
    hub._usage_note = lambda *a, **k: None
    hub.MODEL_OVERRIDES.clear()
    hub.MODEL_CACHE.clear()
    result = hub._invoke(profile, {'messages': [{'role': 'user', 'content': 'hi'}]})
    assert result['choices'][0]['message']['content'] == 'pong'
    assert hub.MODEL_OVERRIDES['gemini-web'] == 'gemini-auto'
    assert ('POST', 'completions', 'gemini-auto') in calls
    print('invoke_auto_retry: PASS')

    # 4) 查不到同族模型时交给 fallback（抛 Fallbackable）
    hub.MODEL_OVERRIDES.clear()
    hub.MODEL_CACHE.clear()
    profile2 = dict(profile, id='deepseek-x', model='deepseek-v4.1-flash', base_url='http://127.0.0.1:8317/v1')

    def fake_http2(method, url, key=None, proxy=False, body=None, form=None,
                   stream=False, timeout=(10, 150)):
        if method == 'GET':
            return {'data': [{'id': 'sensenova-6.8-flash-lite'}]}
        raise hub.UpstreamError(400, detail='model_not_available')

    hub.http = fake_http2
    try:
        hub._invoke(profile2, {'messages': [{'role': 'user', 'content': 'hi'}]})
        raise AssertionError('should raise Fallbackable')
    except hub.Fallbackable as exc:
        assert exc.reason == 'model_not_available'
    print('invoke_fallback: PASS')

    print('ALL MODEL-HUB OFFLINE TESTS PASS')


if __name__ == '__main__':
    main()
    test_short_wechat_blank_backup_retries_once(load())
