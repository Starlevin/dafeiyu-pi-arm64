#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线单测：model-hub 400 model_not_available 自动选模与 fallback 判定。"""
import importlib.util
import json
import sys
import tempfile
import time
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

    def invoke(profile, body, _allow_auto=True, *, retry_timeout=None, _deadline=None):
        called.append((profile['id'], body['max_tokens'], retry_timeout, _deadline))
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
    assert [call[:3] for call in called] == [
        (primary['id'], 160, None), (backup['id'], 160, None),
        (backup['id'], 256, (1.25, 3.75)),
    ]
    assert called[0][3] < called[1][3]
    assert called[2][3] <= called[1][3]
    print('short_wechat_blank_backup_retries_once: PASS')


def test_workbuddy_account_snapshot_routes(hub):
    primary = {'id': 'workbuddy-deepseek', 'name': 'WorkBuddy', 'model': 'deepseek-v4.1-flash',
               'protocol': 'chat', 'base_url': hub.GATEWAY_URL, 'fallback_to': 'sensenova-6.8-flash-lite'}
    backup = {'id': 'sensenova-6.8-flash-lite', 'name': 'SenseNova'}
    body = {'model': primary['id'], 'messages': [{'role': 'user', 'content': 'synthetic'}]}
    calls, routes = [], []

    def invoke(profile, _body):
        calls.append(profile['id'])
        return {'choices': [{'message': {'content': 'ok'}}]}

    hub.profiles = lambda: [primary, backup]
    hub._invoke = invoke
    hub.route_log = routes.append
    with tempfile.TemporaryDirectory() as directory:
        hub.WORKBUDDY_STATUS_FILE = Path(directory) / 'workbuddy-status.json'
        hub.COOLDOWN_FILE = Path(directory) / 'cooldown.json'

        def publish(accounts, at, model=primary['model']):
            hub.WORKBUDDY_STATUS_FILE.write_text(json.dumps({
                'updatedAt': at, 'models': [{'id': model, 'enabled': True, 'availableAccounts': accounts}],
            }), encoding='utf-8')

        def route(expected, reason=''):
            calls.clear()
            hub.complete(body)
            assert calls == [expected], calls
            assert routes[-1]['reason'] == reason, routes[-1]

        publish(0, time.time())
        route(backup['id'], hub.WORKBUDDY_NO_ACCOUNT)
        assert hub.cooldown_state() == {}

        hub.set_cooldown(primary['id'], hub.WORKBUDDY_NO_ACCOUNT)
        failed_at = hub.cooldown_state()[primary['id']]['at']
        publish(1, failed_at - 1)
        route(backup['id'], 'cooldown_' + hub.WORKBUDDY_NO_ACCOUNT)
        publish(1, time.time() + .1)
        route(primary['id'])
        assert hub.cooldown_state() == {}

        hub.set_cooldown(primary['id'], 'http_429')
        publish(1, time.time() + .1)
        route(backup['id'], 'cooldown_http_429')
        hub.clear_cooldown(primary['id'])

        for count, age, model in ((0, 91, primary['model']), (0, -5, primary['model']),
                                  (False, 0, primary['model']), (0, 0, 'different-model')):
            publish(count, time.time() - age, model)
            route(primary['id'])
        hub.WORKBUDDY_STATUS_FILE.write_text('{', encoding='utf-8')
        route(primary['id'])
    print('workbuddy_account_snapshot: PASS')


def test_short_wechat_reply_has_its_own_budget(hub):
    primary = {'id': 'workbuddy-deepseek', 'name': 'primary', 'protocol': 'chat',
               'base_url': hub.GATEWAY_URL, 'fallback_to': 'sensenova-6.8-flash-lite'}
    backup = {'id': 'sensenova-6.8-flash-lite', 'name': 'backup', 'protocol': 'chat'}
    result = {'choices': [{'message': {'content':
        '{"participate":true,"text":"接上同一话题","emotion":"neutral","intensity":1}'}}]}
    hub.profiles = lambda: [primary, backup]
    hub.time = types.SimpleNamespace(monotonic=lambda: 100.0, time=lambda: 100.0,
                                     strftime=lambda *_: 'fixture')
    hub._workbuddy_model_state = lambda _: (None, 0)
    hub.cooldown_until = lambda _: 0
    hub.set_cooldown = lambda *_: None
    hub.clear_cooldown = lambda *_: None
    routes = []
    hub.route_log = routes.append
    for platform, limit, tools, expected in (
            ('wechat', 128, None, (109.0, 115.0)),
            ('qq', 128, None, (112.0, 122.0)),
            ('wechat', 161, None, (None, None)),
            ('wechat', 128, [{'type': 'function'}], (None, None))):
        deadlines = []
        def invoke(profile, _body, _deadline=None):
            deadlines.append(_deadline)
            if profile['id'] == primary['id']:
                raise hub.Fallbackable('timeout')
            return result
        hub._invoke = invoke
        req = {'model': primary['id'], 'max_tokens': limit,
               'response_format': {'type': 'json_object'},
               'messages': [{'role': 'system',
                             'content': f'[QQ_PARTICIPATION] 本轮来自 {platform} 客户端适配层 text emotion intensity'}]}
        if tools:
            req['tools'] = tools
        assert hub.complete(req)['choices']
        assert tuple(deadlines) == expected, (platform, limit, deadlines)
        assert routes[-1]['reason'] == ('group_primary_timeout' if expected[0] else 'timeout')
    print('wechat_short_deadline_scope: PASS')


def test_workbuddy_account_error_is_scoped(hub):
    payload = json.dumps({'error': {'type': 'no_available_account', 'message': 'fixture private detail'}})
    parsed = hub._upstream_error(types.SimpleNamespace(status_code=503, content=payload.encode()))
    assert parsed.kind == 'no_available_account' and 'fixture private detail' not in str(parsed)
    invoke = hub._invoke
    with tempfile.TemporaryDirectory() as directory:
        key = Path(directory) / 'key'
        key.write_text('fixture')
        profile = {'id': 'workbuddy-deepseek', 'base_url': hub.GATEWAY_URL, 'protocol': 'chat',
                   'key_file': str(key), 'model': 'deepseek-v4.1-flash'}
        def fail(*args, **kwargs):
            raise parsed
        hub.http = fail
        for endpoint, expected in ((hub.GATEWAY_URL, hub.WORKBUDDY_NO_ACCOUNT),
                                   ('http://127.0.0.1:8318/v1', 'http_503')):
            try:
                invoke(dict(profile, base_url=endpoint), {})
            except hub.Fallbackable as error:
                assert error.reason == expected, error.reason
            else:
                raise AssertionError('upstream refusal accepted')
    print('workbuddy_account_error_scope: PASS')


def test_short_wechat_reserves_budget_for_the_visible_reply(hub):
    profile = {'id': 'workbuddy-deepseek', 'name': 'WorkBuddy', 'model': 'deepseek-v4.1-flash',
               'protocol': 'chat', 'base_url': hub.GATEWAY_URL, 'reasoning': 'high'}
    hub.profiles = lambda: [profile]
    hub.route_log = lambda _row: None
    hub.clear_cooldown = lambda _profile: None
    observed = []
    def invoke(actual, _request):
        observed.append(actual['reasoning'])
        return {'choices': [{'message': {'content':
            '{"participate":false,"text":"","emotion":"neutral","intensity":0}'}}]}
    hub._invoke = invoke
    prompt = '本轮来自 wechat 客户端适配层。[QQ_PARTICIPATION] 输出 participate、text、emotion、intensity。'
    for note, budget in ((prompt, 128), (prompt.replace('wechat', 'qq'), 128), (prompt, 161)):
        hub.complete({'model': profile['id'], 'max_tokens': budget,
                      'response_format': {'type': 'json_object'},
                      'messages': [{'role': 'system', 'content': note}]})
    assert observed == ['none', 'high', 'high'] and profile['reasoning'] == 'high'
    print('short_wechat_visible_reply_budget: PASS')


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

    test_workbuddy_account_error_is_scoped(load())
    test_workbuddy_account_snapshot_routes(load())
    test_short_wechat_reserves_budget_for_the_visible_reply(load())
    test_short_wechat_reply_has_its_own_budget(load())
    print('ALL MODEL-HUB OFFLINE TESTS PASS')


if __name__ == '__main__':
    main()
    test_short_wechat_blank_backup_retries_once(load())
