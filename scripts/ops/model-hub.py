#!/usr/bin/env python3
"""Private loopback model profiles, ChatGPT OAuth, and protocol adapter."""
import base64
import copy
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import urllib.error
import urllib.request
import jwt
import requests

ROOT = Path.home() / 'dafeiyu-test'
HERE = Path(__file__).resolve().parent
PRIVATE = ROOT / 'secrets/model-hub'
ORIGIN = 'http://127.0.0.1:18090'
GROUP_CHAT_ROUTE_SECONDS = 22.0
GROUP_CHAT_PRIMARY_SECONDS = 12.0
QQ_ORDINARY_REPLY_TOKENS = 160
QQ_SCORED_ORDINARY_PROMPT = (
    '针对当前群聊内容自然接一句，简短贴题，不复述，不把群友的经历或邀请说成自己的。'
    '只输出 text、emotion、intensity 三字段的 JSON。'
)
QQ_FAST_REPLY_PROMPT = '只输出最终 JSON，不展示推理过程。'
WORKBUDDY_BACKUP_SECONDS = 30.0
WECHAT_SHORT_ROUTE_SECONDS = 15.0
WECHAT_SHORT_PRIMARY_SECONDS = 9.0
PANEL_PROXY = 'http://127.0.0.1:18091'
CALLBACK = ORIGIN + '/auth/callback'
PROXY = 'http://127.0.0.1:17890'
# --- usage ledger patch v1 ---
# 用量记账：本文件同目录的 usage_ledger.py。缺文件时自动降级为不记账（不影响聊天）。
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
try:
    import usage_ledger as _usage_ledger
    _USAGE = _usage_ledger.UsageLedger(PRIVATE / 'usage-ledger.json',
                                       log=lambda m: print(m, flush=True))
except Exception as _usage_error:      # 记账模块缺失：聊天照常
    _usage_ledger = None
    _USAGE = None
    print('[usage] 记账模块不可用：%s' % type(_usage_error).__name__, flush=True)
# --- end usage ledger patch v1 ---
LOCK = threading.RLock()
# ---- 暖会话复用（gemini-web opt-in）----
# 按对话标识（请求 user 字段）键控网关 session_id；进程内内存态，空闲剪枝。
_WARM_LOCK = threading.RLock()
_WARM_SESSIONS = {}        # conv_key -> {"sid": str, "ts": float}
_WARM_IDLE = 840.0         # 略低于网关 900s 空闲超时，提前剪掉将死会话
# ChatGPT 套餐推理的固定上游；E2E 夹具会在导入后用同名字段改指向合成服务。
_RESPONSES_URL = 'https://api.openai.com/v1/responses'
SLOTS = threading.BoundedSemaphore(3)
PENDING = None


def limit_control():
    path = str(ROOT / 'qq-admin')
    if path not in sys.path:
        sys.path.insert(0, path)
    from admin_core import Controller
    return Controller(ROOT)

# ---- WorkBuddy 本地网关（DeepSeek 通道）----
GATEWAY_KEY = Path.home() / 'workbuddy-gateway/secrets/api.key'
GATEWAY_URL = 'http://127.0.0.1:8317/v1'
WORKBUDDY_STATUS_FILE = GATEWAY_KEY.parent.parent / 'workbuddy-status.json'
WORKBUDDY_STATUS_MAX_AGE = 90
WORKBUDDY_NO_ACCOUNT = 'workbuddy_no_available_account'
FALLBACK_COOLDOWN = 300          # 主通道故障后，多久内直接走备用
ROUTE_LOG = ROOT / 'run/model-route.log'
COOLDOWN_FILE = PRIVATE / 'cooldown.json'
LOOPBACK_HOSTS = {'127.0.0.1', 'localhost', '::1'}


def atomic(path, data):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False))
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def private_key(name):
    path = PRIVATE / name
    if not path.exists():
        path.write_text(secrets.token_urlsafe(40))
        os.chmod(path, 0o600)
    return path.read_text().strip()


def initialize():
    os.umask(0o077)
    PRIVATE.mkdir(parents=True, exist_ok=True)
    os.chmod(PRIVATE, 0o700)
    private_key('admin.key'); private_key('gateway.key'); private_key('panel.key')
    if not (PRIVATE / 'profiles.json').exists():
        atomic(PRIVATE / 'profiles.json', {'profiles': [
            {'id': name, 'name': title, 'model': name, 'base_url': 'https://token.sensenova.cn/v1',
             'protocol': 'chat', 'key_file': str(ROOT / 'secrets/llm.key'), 'idle_effort': True, 'proxy': False}
            for name, title in [('sensenova-6.8-flash-lite', '商汤 6.8 Flash Lite'), ('glm-5.2', 'GLM-5.2（商汤接口）')]]})
    saved = read_json(PRIVATE/'profiles.json')
    changed = False
    if not any(x['id'] == 'workbuddy-deepseek' for x in saved['profiles']):
        saved['profiles'].insert(min(1, len(saved['profiles'])), {
            'id': 'workbuddy-deepseek', 'name': 'WorkBuddy · DeepSeek V4.1 Flash',
            'model': 'deepseek-v4.1-flash', 'base_url': GATEWAY_URL, 'protocol': 'chat',
            'key_file': str(GATEWAY_KEY), 'idle_effort': False, 'proxy': False,
            'reasoning': 'high', 'fallback_to': 'sensenova-6.8-flash-lite'})
        changed = True
    else:
        # 迁移：WorkBuddy DeepSeek 默认启用其原生思考档（实测仅此字段生效，仍快于商汤 none）。
        for item in saved['profiles']:
            if item['id'] == 'workbuddy-deepseek' and 'reasoning' not in item:
                item['reasoning'] = 'high'
                changed = True
    if not any(x['id']=='gpt-oauth' for x in saved['profiles']):
        saved['profiles'].append({'id':'gpt-oauth', 'name':'GPT · ChatGPT 授权（需先登录）',
            'model':'gpt-6.1-sol', 'base_url':'https://api.openai.com/v1', 'protocol':'oauth',
            'proxy':True, 'idle_effort':False})
        changed = True
    if changed:
        atomic(PRIVATE/'profiles.json', saved)


def profiles():
    with LOCK:
        return read_json(PRIVATE / 'profiles.json')['profiles']


def validate_profile(item):
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,80}', item.get('id', '')):
        raise ValueError('配置标识只接受字母、数字、点、下划线与短横线')
    if not isinstance(item.get('name'), str) or not 1 <= len(item['name']) <= 80:
        raise ValueError('显示名称需要为 1 至 80 字')
    if not isinstance(item.get('model'), str) or not 1 <= len(item['model'].strip()) <= 128:
        raise ValueError('请填写模型 ID')
    url = urlsplit(item.get('base_url', ''))
    if (not url.hostname or url.username or url.password or url.query or url.fragment
            or len(item['base_url']) > 1024):
        raise ValueError('接口地址需要是无凭据、无查询参数的 Base URL')
    scheme = (url.scheme or '').lower()
    if scheme == 'http':
        # 明文 HTTP 仅放行本机回环（如 127.0.0.1:8317 的 WorkBuddy 反代），外部明文一律拒绝。
        if url.hostname not in LOOPBACK_HOSTS:
            raise ValueError('明文 HTTP 只允许指向 127.0.0.1 / localhost 本机回环，其它地址必须使用 HTTPS')
    elif scheme != 'https':
        raise ValueError('接口地址只支持 HTTPS，或指向本机回环的 HTTP')
    if item.get('protocol') not in ['chat', 'responses', 'oauth']:
        raise ValueError('不支持的接口协议')
    mode = str(item.get('reasoning') or '').lower()
    if mode and mode not in REASONING_MODES:
        raise ValueError('思考档位只支持 none / low / medium / high')
    if item['protocol'] == 'oauth' and item['base_url'].rstrip('/') != 'https://api.openai.com/v1':
        raise ValueError('ChatGPT OAuth 仅允许官方 Responses API')


def _upstream_error(response):
    try:
        payload = json.loads(response.content[:65536])
    except (ValueError, TypeError):
        payload = {}
    error = payload.get('error') if isinstance(payload, dict) else None
    if isinstance(error, dict):
        detail = ' '.join(str(error[k]) for k in ('code', 'type', 'message') if error.get(k) is not None)
        kind = 'no_available_account' if error.get('type') == 'no_available_account' else ''
        return UpstreamError(response.status_code, detail=detail[:200], kind=kind)
    return UpstreamError(response.status_code, detail=error[:200] if isinstance(error, str) else '')


def _deadline_timeout(deadline, timeout):
    if deadline is None:
        return timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0.02:
        raise requests.exceptions.Timeout('route_deadline')
    connect = min(float(timeout[0]), max(0.01, remaining / 3.0))
    return connect, min(float(timeout[1]), max(0.01, remaining - connect))


def _deadline_kwargs(deadline):
    return {'deadline': deadline} if deadline is not None else {}


def http(method, url, key=None, proxy=False, body=None, form=None, stream=False, timeout=(10, 150), deadline=None):
    session = requests.Session()
    session.trust_env = False
    if proxy:
        session.proxies = {'https': PROXY, 'http': PROXY}
    headers = {'User-Agent': 'DaFeiYu/1.0'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    try:
        response = session.request(method, url, headers=headers, json=body, data=form,
                                   timeout=_deadline_timeout(deadline, timeout), allow_redirects=False, stream=True)
    except Exception:
        session.close()
        raise
    if not 200 <= response.status_code < 300:
        try:
            raise _upstream_error(response)
        finally:
            response.close()
            session.close()
    if stream:
        return response
    data = bytearray()
    try:
        for chunk in response.iter_content(65536):
            if deadline is not None and time.monotonic() >= deadline:
                raise requests.exceptions.Timeout('route_deadline')
            data.extend(chunk)
            if len(data) > 20_000_000:
                raise ValueError('上游响应过大')
        try:
            return json.loads(data) if data else {}
        except json.JSONDecodeError as error:
            raise UpstreamError(502, '上游返回不是有效 JSON') from error
    except requests.exceptions.ConnectionError as error:
        if deadline is not None:
            from urllib3.exceptions import ReadTimeoutError
            if any(isinstance(arg, ReadTimeoutError) for arg in error.args):
                raise requests.exceptions.Timeout('route_deadline') from error
        raise
    finally:
        response.close(); session.close()


class UpstreamError(Exception):
    def __init__(self, status, message=None, detail='', kind=''):
        self.status = status
        self.detail = detail or ''
        self.kind = kind
        super().__init__(message or ('上游接口返回 HTTP ' + str(status)))


class Fallbackable(Exception):
    """上游不可用但换备用模型仍可能成功；与参数错误等「不可回退」故障区分开。"""

    def __init__(self, reason, status=None):
        self.reason = reason
        self.status = status
        super().__init__(reason)


# 只有这些情况才回退：鉴权失效、限流、上游服务端故障、超时、空回复。
# 400 参数错误、上下文超限、用户取消、内容拒答都**不**回退，避免把真错误掩盖成「换模型就好了」。
# 例外：400/404 且明确是「模型不存在/无可用渠道」时，由 _model_unavailable 单独识别后
# 先尝试自动选模，选不到再走 fallback。
FALLBACK_STATUS = {401, 403, 408, 409, 425, 429, 500, 502, 503, 504}

# --- 自动选模（上游静默下线模型时自愈） ---------------------------------------
MODEL_ERROR_HINTS = (
    'model_not_available', 'model_not_found', 'invalid model', 'unknown model',
    'model does not exist', 'no available channel', 'no available model',
    '模型不存在', '模型不可用', '无可用渠道', '无可用模型',
)
MODEL_CACHE = {}          # base_url -> {'at': float, 'models': [id...]}
MODEL_CACHE_TTL = 600.0   # 可用模型清单缓存 10 分钟
MODEL_OVERRIDES = {}      # profile id -> 已自动切换到的模型 id（进程内）


def _model_unavailable(error):
    """True only when the upstream explicitly says the model is unavailable."""
    if getattr(error, 'status', None) not in (400, 404, 422):
        return False
    detail = str(getattr(error, 'detail', '') or '').lower()
    return bool(detail) and any(token in detail for token in MODEL_ERROR_HINTS)


def _available_models(profile, deadline=None):
    """Query {base_url}/models with a short cache; never raises."""
    base = str(profile.get('base_url') or '').rstrip('/')
    if not base or profile.get('protocol') == 'oauth':
        return []
    cached = MODEL_CACHE.get(base)
    now = time.time()
    if cached and now - float(cached.get('at', 0.0)) < MODEL_CACHE_TTL:
        return list(cached.get('models') or [])
    try:
        key = Path(profile['key_file']).read_text().strip()
    except (OSError, KeyError):
        return []
    try:
        result = http('GET', base + '/models', key, profile.get('proxy', False),
                      timeout=(5, 20), **_deadline_kwargs(deadline))
    except Exception:
        return []
    items = result.get('data') if isinstance(result, dict) else []
    models = [str(item.get('id')) for item in (items or [])
              if isinstance(item, dict) and item.get('id')]
    MODEL_CACHE[base] = {'at': now, 'models': models}
    return models


def _pick_model(profile, models):
    """Choose a replacement in the same family, preferring *-auto."""
    current = str(profile.get('model') or '')
    candidates = [m for m in models if m and m != current
                  and not any(x in m.lower() for x in ('embed', 'rerank'))]
    if not candidates:
        return ''
    family = current.split('-')[0].lower() if current else ''
    same = [m for m in candidates if family and m.lower().startswith(family + '-')]
    for model in same:
        if 'auto' in model.lower():
            return model
    if same:
        return same[0]
    for model in candidates:
        if 'auto' in model.lower():
            return model
    return ''


def _profile_with_override(profile):
    model = MODEL_OVERRIDES.get(profile.get('id'))
    if model and model != profile.get('model'):
        return dict(profile, model=model)
    return profile

# 可显式指定的思考档位；profile 里配 reasoning 即覆盖 idle_effort 的旧行为。
REASONING_MODES = {'none', 'low', 'medium', 'high'}


def _is_local_workbuddy(profile):
    return (profile.get('id') == 'workbuddy-deepseek'
            and profile.get('protocol') == 'chat'
            and profile.get('base_url', '').rstrip('/') == GATEWAY_URL)


def _workbuddy_model_state(profile):
    """Return (availability, observed time) when the gateway snapshot is current."""
    if not _is_local_workbuddy(profile):
        return None, 0
    try:
        if not WORKBUDDY_STATUS_FILE.is_file():
            return None, 0
        with WORKBUDDY_STATUS_FILE.open('rb') as stream:
            raw = stream.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            return None, 0
        status = json.loads(raw)
        at = status.get('updatedAt') if isinstance(status, dict) else None
        if type(at) not in (int, float) or not -2 <= time.time() - at <= WORKBUDDY_STATUS_MAX_AGE:
            return None, 0
        models = status.get('models')
        if not isinstance(models, list):
            return None, 0
        matching = [row for row in models if isinstance(row, dict) and row.get('id') == profile.get('model')]
        if len(matching) == 1 and matching[0].get('enabled') is True:
            available = matching[0].get('availableAccounts')
            if type(available) is int and available >= 0:
                return available > 0, at
    except (OSError, ValueError, RecursionError):
        pass
    return None, 0


def cooldown_state():
    try:
        data = read_json(COOLDOWN_FILE, {})
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: row for key, row in data.items() if isinstance(row, dict)}


def _cooldown_deadline(item):
    try:
        until = float(item.get('until') or 0)
        return until if math.isfinite(until) else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def cooldown_until(pid):
    return _cooldown_deadline(cooldown_state().get(pid) or {})


def set_cooldown(pid, reason, seconds=FALLBACK_COOLDOWN):
    with LOCK:
        data = cooldown_state()
        data[pid] = {'until': time.time() + seconds, 'reason': reason, 'at': time.time()}
        atomic(COOLDOWN_FILE, data)


_UNGUARDED_COOLDOWN_CLEAR = object()


def clear_cooldown(pid, expected=_UNGUARDED_COOLDOWN_CLEAR):
    # A successful request may only clear the failure it saw before starting.
    if expected is None:
        return
    with LOCK:
        data = cooldown_state()
        if expected is not _UNGUARDED_COOLDOWN_CLEAR and data.get(pid) != expected:
            return
        if data.pop(pid, None) is not None:
            atomic(COOLDOWN_FILE, data)


def route_log(record):
    """只记录路由元数据；不写用户消息、回复正文或任何密钥。"""
    try:
        ROUTE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ROUTE_LOG.open('a') as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + '\n')
    except OSError:
        pass


def empty_reply(result):
    """Reject unusable upstream completion envelopes, even after HTTP 200."""
    choices = result.get('choices') if isinstance(result, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return True
    choice = choices[0]
    if choice.get('finish_reason') in ('content_filter', 'sensitive'):
        return False  # 拒答属于正常结论，不当作上游故障
    message = choice.get('message')
    if not isinstance(message, dict):
        return True
    tool_calls = message.get('tool_calls')
    if tool_calls is not None and (
            not isinstance(tool_calls, list) or any(not isinstance(call, dict) for call in tool_calls)):
        return True
    content = message.get('content')
    return not (isinstance(content, str) and content.strip()) and not tool_calls


def _usage_profile(profile):
    """把 profile 归成 (provider, 实际 model)。provider 是推断名，不含密钥。"""
    try:
        provider = _usage_ledger.infer_provider(profile)
    except Exception:
        provider = 'unknown'
    return provider, str(profile.get('model') or 'unknown')


def _usage_begin(profile):
    """请求前登记幂等键：重启或重放都不会重复计一次。失败返回 None。"""
    if _USAGE is None:
        return None
    try:
        provider, model = _usage_profile(profile)
        return _USAGE.reserve(provider, model)
    except Exception as error:
        print('[usage] 预登记失败：%s' % type(error).__name__, flush=True)
        return None


def _usage_note(profile, usage, status='ok', token=None):
    """记一次真实上游调用。usage 缺失就只计「缺用量」，绝不估算。"""
    if _USAGE is None:
        return
    try:
        provider, model = _usage_profile(profile)
        _USAGE.record(provider, model, usage, status=status, token=token)
    except Exception as error:
        print('[usage] 记账失败：%s' % type(error).__name__, flush=True)



def _warm_prune():
    now = time.time()
    dead = [k for k, v in _WARM_SESSIONS.items() if now - v["ts"] > _WARM_IDLE]
    for k in dead:
        _WARM_SESSIONS.pop(k, None)


def _gemini_create_session(base, key, model, priming, limit):
    # No priming: the first warm turn (full system folded in) allocates the
    # conversation lazily, so the first turn pays one generation, not priming+turn.
    body = {"model": model}
    res = http("POST", base + "/sessions", key, False, body, timeout=(10, limit))
    return res.get("session_id") if isinstance(res, dict) else None


def _gemini_warm(profile, payload):
    """Opt-in warm-session reuse for gemini-web. Returns the upstream result dict,
    or None to fall through to the cold single-shot path. Warm-specific failures
    degrade to cold (never to the backup model) so persona/behavior stays on Gemini.
    The full current system is folded into the trailing-turn user message each turn
    so per-turn system additions (private style / knowledge hint / diary) are never
    lost; temporary_chat=False is mandatory for continuing an established chat."""
    conv = payload.get("user")
    if not conv or not isinstance(conv, str):
        return None
    msgs = payload.get("messages") or []
    system = "\n\n".join(str(m.get("content") or "") for m in msgs
                         if isinstance(m, dict) and m.get("role") == "system" and m.get("content"))
    last_ai = -1
    for i, m in enumerate(msgs):
        if isinstance(m, dict) and m.get("role") == "assistant":
            last_ai = i
    trailing = msgs[last_ai + 1:]
    trailing_text = "\n\n".join(str(m.get("content") or "") for m in trailing
                                if isinstance(m, dict) and m.get("content")
                                and m.get("role") != "system")
    if not trailing_text.strip():
        return None
    content = (system + "\n\n" + trailing_text) if system.strip() else trailing_text
    base = profile["base_url"].rstrip("/")
    try:
        key = Path(profile["key_file"]).read_text().strip()
    except OSError:
        return None
    try:
        limit = int(profile.get("timeout") or 70)
    except (TypeError, ValueError):
        limit = 70
    with _WARM_LOCK:
        _warm_prune()
        entry = _WARM_SESSIONS.get(conv)
        sid = entry["sid"] if entry else None
    if not sid:
        try:
            sid = _gemini_create_session(base, key, profile["model"], system, limit)
        except Exception:
            return None
        if not sid:
            return None
        with _WARM_LOCK:
            _WARM_SESSIONS[conv] = {"sid": sid, "ts": time.time()}
    body = {"model": profile["model"], "session_id": sid, "temporary_chat": False,
            "messages": [{"role": "user", "content": content}]}
    try:
        result = http("POST", base + "/chat/completions", key, False, body, timeout=(10, limit))
    except UpstreamError as error:
        if error.status != 409:
            return None
        # session expired/invalidated upstream: drop and recreate once, then retry
        with _WARM_LOCK:
            _WARM_SESSIONS.pop(conv, None)
        try:
            sid = _gemini_create_session(base, key, profile["model"], system, limit)
            if not sid:
                return None
            with _WARM_LOCK:
                _WARM_SESSIONS[conv] = {"sid": sid, "ts": time.time()}
            body["session_id"] = sid
            result = http("POST", base + "/chat/completions", key, False, body, timeout=(10, limit))
        except Exception:
            return None
    except Exception:
        return None
    with _WARM_LOCK:
        if conv in _WARM_SESSIONS:
            _WARM_SESSIONS[conv]["ts"] = time.time()
    return result


def _memory_json_request(body):
    """True only for the QQ memory schema; do not rewrite normal chat JSON."""
    fmt = body.get('response_format') if isinstance(body, dict) else None
    if not (isinstance(fmt, dict) and str(fmt.get('type') or '').lower() == 'json_object'):
        return False
    try:
        sys_text = ' '.join(str(item.get('content') or '') for item in (body.get('messages') or [])
                            if isinstance(item, dict) and item.get('role') == 'system')
    except Exception:
        return False
    low = sys_text.lower()
    return ('记忆整理' in sys_text) or ('[qq_memory_v2]' in low) or ('memorybooks' in low)


def _normalize_memory_json(result, body):
    """Strip code fences from the QQ memory JSON response only.

    gemini-web currently ignores response_format and wraps valid JSON in a
    ```json fence. The Swift memory decoder intentionally requires exact
    top-level keys, so removing the fence is lossless and prevents every
    memory attempt from failing after a successful model call.
    """
    if not _memory_json_request(body) or not isinstance(result, dict):
        return result
    try:
        for choice in (result.get('choices') or []):
            message = choice.get('message') if isinstance(choice, dict) else None
            if not isinstance(message, dict):
                continue
            content = message.get('content')
            if not isinstance(content, str) or not content.strip().startswith('```'):
                continue
            candidate = _strip_fence(content)
            try:
                parsed = json.loads(candidate)
            except Exception:
                continue
            if isinstance(parsed, dict) and set(parsed.keys()) == {'summary', 'changes'}:
                message['content'] = json.dumps(parsed, ensure_ascii=False, separators=(',', ':'))
    except Exception:
        pass
    return result


def _invoke(profile, body, _allow_auto=True, *, retry_timeout=None, _deadline=None):
    """发一次上游请求。可回退故障统一抛 Fallbackable，其余异常原样上抛。"""
    profile = _profile_with_override(profile)
    payload = copy.deepcopy(body)
    payload['model'] = profile['model']
    payload['stream'] = False
    if profile['protocol'] == 'chat':
        payload.pop('thinking', None)
        # reasoning 显式档位（none/low/medium/high）优先；未设置时沿用 idle_effort 旧行为。
        # 实测 WorkBuddy DeepSeek 只认 reasoning_effort，thinking 字段会被上游忽略。
        mode = str(profile.get('reasoning') or '').lower()
        if mode in REASONING_MODES:
            payload['reasoning_effort'] = mode
        elif profile.get('idle_effort'):
            payload.setdefault('reasoning_effort', 'none')
        else:
            payload.pop('reasoning_effort', None)
        # opt-in 暖会话复用：仅 gemini-web 且 profile.warm 开启、请求带 user 对话标识。
        # 暖路径失败回退冷路径（继续往下），不回退备用模型，保住人设与行为。
        if profile.get('warm') and profile.get('id') == 'gemini-web':
            _warm_result = _gemini_warm(profile, payload)
            if _warm_result is not None:
                _usage_note(profile, _warm_result.get('usage') if isinstance(_warm_result, dict) else None)
                return _normalize_memory_json(_warm_result, body)
        key_path = Path(profile['key_file'])
        if not key_path.exists():
            raise Fallbackable('missing_key')
        key = key_path.read_text().strip()
        try:
            limit = int(profile.get('timeout') or 150)
        except (TypeError, ValueError):
            limit = 150
        try:
            _result = http('POST', profile['base_url'].rstrip('/') + '/chat/completions', key,
                           profile.get('proxy', False), payload,
                           timeout=retry_timeout if retry_timeout is not None else (10, limit),
                           **_deadline_kwargs(_deadline))
        except requests.exceptions.Timeout:
            _usage_note(profile, None, 'timeout')
            raise Fallbackable('timeout')
        except requests.exceptions.ConnectionError:
            _usage_note(profile, None, 'connect_error')
            raise Fallbackable('connect_error')
        except UpstreamError as error:
            _usage_note(profile, None, 'http_%d' % error.status)
            if error.status == 503 and error.kind == 'no_available_account' and _is_local_workbuddy(profile):
                raise Fallbackable(WORKBUDDY_NO_ACCOUNT, error.status) from error
            if _allow_auto and _model_unavailable(error):
                replacement = _pick_model(profile, _available_models(profile, _deadline)
                                          if _deadline is not None else _available_models(profile))
                if replacement:
                    MODEL_OVERRIDES[profile['id']] = replacement
                    print('[auto-model] profile=%s from=%s to=%s reason=model_not_available'
                          % (profile['id'], profile['model'], replacement), flush=True)
                    return _invoke(dict(profile, model=replacement), body, False,
                                   **({'_deadline': _deadline} if _deadline is not None else {}))
                print('[auto-model] profile=%s no_alternative reason=model_not_available'
                      % profile['id'], flush=True)
                raise Fallbackable('model_not_available', error.status)
            raise Fallbackable('http_%d' % error.status, error.status) if error.status in FALLBACK_STATUS else error
        _usage_note(profile, _result.get('usage') if isinstance(_result, dict) else None)
        return _normalize_memory_json(_result, body)
    oauth = profile['protocol'] == 'oauth'
    key = oauth_access() if oauth else Path(profile['key_file']).read_text().strip()
    payload = to_responses(payload, profile['model'], oauth)
    if not oauth:
        _result = http('POST', profile['base_url'].rstrip('/')+'/responses', key, profile.get('proxy', False), payload)
        _usage_note(profile, (_result or {}).get('usage') if isinstance(_result, dict) else None)
        return from_responses(_result)
    # Official ChatGPT plan usage requires streaming and store=false.
    response = http('POST', _RESPONSES_URL, key, True, payload, stream=True)
    _token = _usage_begin(profile)
    total = 0
    try:
        for line in response.iter_lines():
            total += len(line)
            if total > 20_000_000:
                raise ValueError('流式响应过大')
            if not line.startswith(b'data: '):
                continue
            if line[6:] == b'[DONE]':
                break
            event = json.loads(line[6:])
            if event.get('type') == 'response.completed':
                _done = event['response']
                _usage_note(profile, (_done or {}).get('usage'), token=_token)
                return from_responses(_done)
            if event.get('type') in ['response.failed', 'response.incomplete', 'error']:
                _usage_note(profile, None, event.get('type') or 'failed', token=_token)
                raise ValueError('GPT 请求失败或未完成，请检查授权和额度')
        _usage_note(profile, None, 'stream_broken', token=_token)
        raise ValueError('响应流在完成前断开')
    finally:
        response.close()


def _strip_fence(text):
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```[A-Za-z]*\s*', '', text)
        if text.rstrip().endswith('```'):
            text = text.rstrip()[:-3]
    return text.strip()


def _first_object(text):
    start = text.find('{')
    while start != -1:
        depth = 0
        for index in range(start, len(text)):
            char = text[index]
            if char == '{':
                depth += 1
            elif char == '}':
                depth -= 1
                if depth == 0:
                    return text[start:index + 1]
        start = text.find('{', start + 1)
    return None


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ['true', '1', 'yes', 'y', '是']


def _as_int(value, low, high):
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return max(low, min(high, number))


REPLY_KEYS = {'participate', 'text', 'emotion', 'intensity'}

# QQPersona.swift QQEmotion (not QQPersonality).
EMOTIONS = {'neutral', 'joy', 'smug', 'annoyed', 'sad', 'sleepy', 'flustered', 'teasing'}
EMOTION_ALIASES = {
    'gentle': 'neutral', 'calm': 'neutral', 'energetic': 'joy', 'tsundere': 'flustered',
    '心疼': 'neutral', '关心': 'neutral', '温柔': 'neutral', '安慰': 'neutral',
    '开心': 'joy', '高兴': 'joy', '兴奋': 'joy', '得意': 'smug',
    '疲惫': 'sleepy', '困': 'sleepy', '害羞': 'flustered', '傲娇': 'flustered',
    '生气': 'annoyed', '难过': 'sad', '调侃': 'teasing', '打趣': 'teasing',
}
REPLY_JSON_HINT = ('仅约束回复格式，保留原有人设与内容规则：只输出一个 JSON 对象，'
                   '不要代码块、不要解释。必须且只能包含 text、emotion、intensity 三个键。'
                   'emotion 必须为 neutral/joy/smug/annoyed/sad/sleepy/flustered/teasing；'
                   'intensity 为0到3的整数。不要增加 participate。')
PROACTIVE_JSON_HINT = ('本轮为 QQ_PARTICIPATION 主动参与评估：只输出 JSON，'
                       '必须包含 participate 布尔、text、emotion、intensity 四个键。'
                       'emotion 只能是 neutral/joy/smug/annoyed/sad/sleepy/flustered/teasing。'
                       '不参与时 participate=false,text="",emotion="neutral",intensity=0。')


def _emotion(value):
    text = str(value or '').strip().lower()
    return text if text in EMOTIONS else EMOTION_ALIASES.get(text, 'neutral')


def proactive_reply(body):
    return any('[QQ_PARTICIPATION]' in str(item.get('content') or '')
               for item in body.get('messages', [])
               if isinstance(item, dict) and item.get('role') == 'system')


def wants_reply_json(body):
    """判断这次调用是不是引擎要的四字段回复对象（只在确认为真时才做兜底改写）。

    引擎稳定带上 response_format=json_object，这是最可靠的信号；系统提示里未必出现
    英文键名 participate/intensity，所以关键词只作为补充判断。
    """
    fmt = body.get('response_format')
    if isinstance(fmt, dict) and str(fmt.get('type') or '').lower() == 'json_object':
        try:
            sys_text = ' '.join(str(item.get('content') or '') for item in (body.get('messages') or [])
                                if isinstance(item, dict) and item.get('role') == 'system').lower()
        except Exception:
            return True
        # Memory and image-intent calls use their own JSON schemas.
        return not any(marker in sys_text for marker in ('memorybooks', '记忆整理', '[image_prompt_plan]'))
    try:
        text = ' '.join(str(item.get('content') or '') for item in (body.get('messages') or [])
                        if isinstance(item, dict) and item.get('role') == 'system')
    except Exception:
        return False
    low = text.lower()
    return 'participate' in low and 'intensity' in low


def salvage_reply(result, strict=False, proactive=False, stickers=False):
    """引擎要求严格 JSON（participate/text/emotion/intensity）。

    上游偶发加代码块包裹、写坏类型、漏字段，或用自然语言代替 JSON，引擎都会判
    「输出格式无效」并退回短句兜底。这里就地修好。strict=True 表示已确认这是引擎的
    回复请求，允许补默认字段、从截断内容里抽 text；否则只做无损规整。
    """
    if not strict:
        return result
    try:
        choice = (result.get('choices') or [{}])[0]
        if choice.get('finish_reason') in ('content_filter', 'sensitive'):
            return result
        message = choice.get('message')
    except Exception:
        return result
    if not isinstance(message, dict):
        return result
    content = message.get('content')
    if not isinstance(content, str) or not content.strip():
        print('[reply-json] 上游回复为空 finish=%s reasoning=%d' % (
            choice.get('finish_reason'), len(str(message.get('reasoning_content') or ''))), flush=True)
        return result
    body_text = _strip_fence(content).strip()
    data = None
    for candidate in (body_text, _first_object(body_text)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            data = parsed
            break
    if data is None and strict:
        quoted = re.search(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"', body_text)
        if quoted:
            try:
                data = {'text': json.loads('"' + quoted.group(1) + '"')}
            except Exception:
                data = {'text': quoted.group(1)}
            print('[reply-json] 从截断内容中抽出 text', flush=True)
        elif '{' not in body_text and '}' not in body_text:
            data = {'text': body_text}
            print('[reply-json] 自然语言代为回复，已包装成回复对象', flush=True)
    if not isinstance(data, dict) or 'text' not in data:
        print('[reply-json] 无法修复 finish=%s 长度=%d 首80=%r' % (
            choice.get('finish_reason'), len(content), '[omitted]'), flush=True)
        return result
    # Active group participation must fail closed without an explicit decision.
    if proactive and type(data.get('participate')) is not bool:
        return result
    if not strict and not REPLY_KEYS <= set(data):
        return result
    participate = _as_bool(data.get('participate', True))
    if proactive and not participate:
        fixed = {'participate': False, 'text': '', 'emotion': 'neutral', 'intensity': 0}
    else:
        text = str(data.get('text') or '').strip()
        if not text:
            return result
        intensity = _as_int(data.get('intensity', 1), 0, 3)
        fixed = {'participate': True, 'text': text,
                 'emotion': _emotion(data.get('emotion')),
                 'intensity': 1 if intensity is None else intensity}
    if not proactive:
        fixed.pop('participate', None)
    if stickers:
        value = data.get('sticker_id')
        fixed['sticker_id'] = value if isinstance(value, str) else None
    message['content'] = json.dumps(fixed, ensure_ascii=False, separators=(',', ':'))
    if not (REPLY_KEYS <= set(data) and all(data.get(k) == fixed.get(k) for k in REPLY_KEYS)):
        print('[reply-json] 已就地修正回复 JSON（键=%s 原文长度=%d）' % (
            ','.join(sorted(fixed)), len(content)), flush=True)
    return result


def complete(body):
    """单选模型 + 主备用自动回退。

    规则：用户选了谁就先走谁；主通道出现可回退故障时，立刻改用 fallback_to 指定的
    备用模型，并记录冷却。冷却期内不再空等主通道，直接走备用；冷却到期后下一条
    新消息会重新尝试主通道。整条链路只返回一次结果，不会让引擎重复发送。
    """
    requested = body.get('model')
    profile = next((x for x in profiles() if x['id'] == requested), None)
    if not profile:
        raise ValueError('未知模型配置，请先在模型后台保存该标识')
    started = time.monotonic()
    try:
        parts = ['%s:%d' % (str(item.get('role')), len(str(item.get('content') or '')))
                 for item in (body.get('messages') or []) if isinstance(item, dict)]
        print('[req] %s max_tokens=%s msgs=%s fmt=%s' % (
            requested, body.get('max_tokens'), ','.join(parts), body.get('response_format')), flush=True)
    except Exception:
        pass
    backup_id = profile.get('fallback_to')
    backup = next((x for x in profiles() if x['id'] == backup_id), None) if backup_id else None

    strict_mode = wants_reply_json(body)
    is_proactive = proactive_reply(body)
    sys_text = ''
    try:
        sys_text = ' '.join(str(item.get('content') or '') for item in (body.get('messages') or [])
                            if isinstance(item, dict) and item.get('role') == 'system')
        low = sys_text.lower()
        print('[sys] len=%d participate=%s intensity=%s memory=%s budget=%s head=%r' % (
            len(sys_text), 'participate' in low, 'intensity' in low, 'memorybooks' in low,
            body.get('max_tokens'), '[omitted]'), flush=True)
    except Exception:
        pass
    budget = body.get('max_tokens')
    bounded_workbuddy_chat = (
        bool(backup and backup.get('protocol') == 'chat' and not backup.get('warm'))
        and _is_local_workbuddy(profile) and strict_mode
        and not body.get('tools') and type(budget) is int and 0 < budget
    )
    # QQ has already scored this request and does not send [QQ_PARTICIPATION].
    # Match its short single-pass prompt, never user content or the review path.
    short_qq_reply = (
        bounded_workbuddy_chat and not is_proactive
        and QQ_SCORED_ORDINARY_PROMPT in sys_text and QQ_FAST_REPLY_PROMPT in sys_text
        and all(isinstance(item, dict) and isinstance(item.get('content'), str)
                for item in body.get('messages') or [])
    )
    if short_qq_reply:
        body = {**body, 'max_tokens': min(budget, QQ_ORDINARY_REPLY_TOKENS)}
    fast_group_route = (
        bounded_workbuddy_chat and (short_qq_reply or (is_proactive and budget <= 160)
             or ('本轮是群内公开且暂未得到回答的问题' in sys_text and budget <= 400
                 and all(key in sys_text.lower() for key in ('text', 'emotion', 'intensity'))))
    )
    short_wechat_reply = (strict_mode and is_proactive and '本轮来自 wechat 客户端适配层' in sys_text
                          and type(budget) is int and 0 < budget <= 160 and not body.get('tools')
                          and _is_local_workbuddy(profile))
    route_deadline = primary_deadline = None
    if fast_group_route:
        primary_seconds, route_seconds = (
            (WECHAT_SHORT_PRIMARY_SECONDS, WECHAT_SHORT_ROUTE_SECONDS) if short_wechat_reply
            else (GROUP_CHAT_PRIMARY_SECONDS, GROUP_CHAT_ROUTE_SECONDS)
        )
        route_deadline = started + route_seconds
        primary_deadline = min(route_deadline, started + primary_seconds)
    primary = profile
    if short_qq_reply or (short_wechat_reply and profile.get('reasoning') == 'high'):
        # Reserve the short participation budget for visible text and JSON.
        primary = dict(profile, reasoning='none')
    # The participation instructions mention sticker_id even when no candidates were sent.
    sticker_mode = strict_mode and (
        '本轮可选表情 JSON：\n[' in sys_text if is_proactive else 'sticker_id' in sys_text)
    if strict_mode:
        body = dict(body)
        body['messages'] = list(body.get('messages') or []) + [
            {'role': 'system', 'content': (PROACTIVE_JSON_HINT if is_proactive else REPLY_JSON_HINT)
             + (' 另外保留 sticker_id 为原始候选 id 或 null；仍遵守原候选白名单。' if sticker_mode else '')}]

    def finish(result, actual, reason=None):
        result = salvage_reply(result, strict_mode, is_proactive, sticker_mode)
        route_log({'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'requested': profile['id'],
                   'actual': actual, 'fallback': bool(reason), 'reason': reason or '',
                   'latency_ms': int((time.monotonic() - started) * 1000)})
        return result

    def short_backup_is_empty(result):
        if empty_reply(result):
            return True
        choice = result['choices'][0]
        message = choice.get('message') or {}
        if choice.get('finish_reason') in ('content_filter', 'sensitive') or message.get('tool_calls'):
            return False
        try:
            fields = json.loads(_strip_fence(message.get('content') or '').strip())
        except ValueError:
            return True
        return not isinstance(fields, dict) or (
            fields.get('participate') is not False and not str(fields.get('text') or '').strip())

    def invoke_with_deadline(target, payload, deadline, *args, **kwargs):
        if deadline is not None:
            if time.monotonic() >= deadline:
                raise Fallbackable('timeout')
            kwargs['_deadline'] = deadline
        result = _invoke(target, payload, *args, **kwargs)
        if deadline is not None and time.monotonic() >= deadline:
            raise Fallbackable('timeout')
        return result

    def attempt_backup():
        deadline = route_deadline
        if _is_local_workbuddy(profile) and backup.get('protocol') == 'chat':
            budget = time.monotonic() + WORKBUDDY_BACKUP_SECONDS
            deadline = budget if deadline is None else min(deadline, budget)
        result = invoke_with_deadline(backup, body, deadline)
        if not short_wechat_reply or backup.get('id') != 'sensenova-6.8-flash-lite':
            return result
        result = salvage_reply(result, strict_mode, is_proactive, sticker_mode)
        if not short_backup_is_empty(result):
            return result
        retry_deadline = time.monotonic() + 5.0
        if deadline is not None:
            retry_deadline = min(deadline, retry_deadline)
        retry = invoke_with_deadline(backup, {**body, 'max_tokens': 256}, retry_deadline,
                                     False, retry_timeout=(1.25, 3.75))
        retry = salvage_reply(retry, strict_mode, is_proactive, sticker_mode)
        if short_backup_is_empty(retry):
            raise Fallbackable('empty_reply')
        return retry

    def standby_backup(reason):
        try:
            result = attempt_backup()
            if empty_reply(result):
                raise Fallbackable('empty_reply')
        except Exception as error:
            if fast_group_route or _is_local_workbuddy(profile):
                failure = 'backup_failed'
                if isinstance(error, Fallbackable):
                    failure = 'backup_empty' if error.reason == 'empty_reply' else 'backup_' + error.reason
                route_log({'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'requested': profile['id'],
                           'actual': '', 'fallback': True, 'reason': failure,
                           'latency_ms': int((time.monotonic() - started) * 1000)})
            raise UpstreamError(503, '主通道不可用（%s），备用模型也不可用：%s' % (reason, error))
        return finish(result, backup['id'], reason)

    observed_cooldown = cooldown_state().get(profile['id'])
    if backup:
        account_available, observed_at = _workbuddy_model_state(profile)
        if account_available is False:
            return standby_backup(WORKBUDDY_NO_ACCOUNT)
        cooldown = observed_cooldown or {}
        if _cooldown_deadline(cooldown) > time.time():
            prior = cooldown.get('reason')
            if not isinstance(prior, str) or not re.fullmatch(r'[a-z0-9_]{1,50}', prior):
                prior = 'unknown'
            failed_at = cooldown.get('at')
            recovered = (account_available is True and prior == WORKBUDDY_NO_ACCOUNT
                         and type(failed_at) in (int, float) and observed_at > failed_at)
            if not recovered:
                return standby_backup('cooldown_' + prior)

    try:
        result = invoke_with_deadline(primary, body, primary_deadline)
        if empty_reply(result):
            raise Fallbackable('empty_reply')
        try:
            clear_cooldown(profile['id'], observed_cooldown)
        except OSError:
            pass  # Cooldown persistence must not determine delivery.
        return finish(result, profile['id'])
    except Fallbackable as error:
        if not backup:
            route_log({'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'requested': profile['id'],
                       'actual': '', 'fallback': False, 'reason': error.reason,
                       'latency_ms': int((time.monotonic() - started) * 1000)})
            raise UpstreamError(503, '%s 不可用（%s），且没有配置备用模型' % (profile['name'], error.reason))
        if fast_group_route and error.reason == 'timeout':
            return standby_backup('group_primary_timeout')
        try:
            set_cooldown(profile['id'], error.reason)
        except OSError:
            pass  # Cooldown persistence must not determine delivery.
        if fast_group_route or _is_local_workbuddy(profile):
            return standby_backup(error.reason)
        try:
            result = attempt_backup()
        except Exception as secondary:
            raise UpstreamError(503, '%s 不可用（%s），备用模型 %s 也不可用：%s'
                                % (profile['name'], error.reason, backup['name'], secondary))
        if empty_reply(result):
            raise UpstreamError(503, '%s 不可用（%s），备用模型 %s 返回空回复'
                                % (profile['name'], error.reason, backup['name']))
        return finish(result, backup['id'], error.reason)


def oauth_access():
    with LOCK:
        account = read_json(PRIVATE / 'oauth.json')
        if not account or 'chatgpt.tokens.use.direct' not in account.get('scope', '').split():
            raise ValueError('请先完成 ChatGPT 授权，并允许使用套餐推理')
        if account['expires_at'] < time.time() + 90:
            result = http('POST', 'https://auth.openai.com/api/accounts/oauth/token', proxy=True,
                form={'grant_type': 'refresh_token', 'client_id': account['client_id'],
                      'refresh_token': account['refresh_token'], 'resource': 'https://api.openai.com/v1'})
            account.update(access_token=result['access_token'], expires_at=time.time()+result.get('expires_in', 3600))
            for key in ['refresh_token', 'id_token', 'scope']:
                if key in result:
                    account[key] = result[key]
            atomic(PRIVATE / 'oauth.json', account)
        return account['access_token']


def start_oauth():
    global PENDING
    with LOCK:
        if not (PRIVATE / 'host-id').exists():
            (PRIVATE / 'host-id').write_text(secrets.token_urlsafe(24))
        verifier = secrets.token_urlsafe(64)
        old = read_json(PRIVATE / 'oauth.json', {})
        client_id = old.get('client_id', 'dynamic_agent_client')
        pending = {'state': secrets.token_urlsafe(32), 'nonce': secrets.token_urlsafe(32),
                   'verifier': verifier, 'created': time.time(), 'client_id': client_id,
                   'subject': old.get('subject')}
        query = {'client_id': client_id, 'ext_agent_host_id': (PRIVATE/'host-id').read_text(),
                 'response_type': 'code', 'redirect_uri': CALLBACK,
                 'scope': 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct',
                 'resource': 'https://api.openai.com/v1', 'state': pending['state'], 'nonce': pending['nonce'],
                 'code_challenge_method': 'S256',
                 'code_challenge': base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')}
        if client_id == 'dynamic_agent_client':
            query['agent_name_hint'] = 'DaFeiYu'
        elif old.get('id_token'):
            query['id_token_hint'] = old['id_token']
        PENDING = pending
        return 'https://auth.openai.com/api/accounts/authorize?' + urlencode(query)


def finish_oauth(query):
    global PENDING
    with LOCK:
        pending = PENDING
        if (not pending or time.time()-pending['created'] > 600
                or not hmac.compare_digest(query.get('state', [''])[0], pending['state'])):
            raise ValueError('授权状态不匹配或已过期，请重新开始登录')
        PENDING = None  # Never exchange the same authorization code twice.
    if query.get('error'):
        raise ValueError('授权未完成，请返回后台重新开始')
    issued = query.get('client_id', [pending['client_id']])[0]
    if issued == 'dynamic_agent_client' or not re.fullmatch(r'[A-Za-z0-9_-]{4,200}', issued):
        raise ValueError('授权回调缺少已注册的客户端标识')
    if pending['client_id'] != 'dynamic_agent_client' and issued != pending['client_id']:
        raise ValueError('返回的客户端标识发生变化，未保存凭据')
    code = query.get('code', [''])[0]
    if not code:
        raise ValueError('回调缺少授权码')
    result = http('POST', 'https://auth.openai.com/api/accounts/oauth/token', proxy=True,
        form={'grant_type': 'authorization_code', 'client_id': issued, 'code': code,
              'code_verifier': pending['verifier'], 'redirect_uri': CALLBACK, 'resource': 'https://api.openai.com/v1'})
    metadata = http('GET', 'https://auth.openai.com/.well-known/openid-configuration', proxy=True)
    jwks_uri = metadata['jwks_uri']
    if urlsplit(jwks_uri).scheme != 'https' or urlsplit(jwks_uri).hostname != 'auth.openai.com':
        raise ValueError('拒绝非官方签名密钥地址')
    if metadata['issuer'].rstrip('/') != 'https://auth.openai.com':
        raise ValueError('发行者与官方地址不一致')
    keys = http('GET', jwks_uri, proxy=True)['keys']
    header = jwt.get_unverified_header(result['id_token'])
    key_data = next(x for x in keys if x.get('kid') == header.get('kid'))
    identity = jwt.decode(result['id_token'], jwt.PyJWK.from_dict(key_data).key,
                          algorithms=['RS256'], audience=issued, issuer=metadata['issuer'],
                          options={'require': ['exp', 'iss', 'aud', 'sub', 'nonce']})
    if not hmac.compare_digest(identity['nonce'], pending['nonce']):
        raise ValueError('ID token nonce 核对失败')
    if pending['subject'] and identity['sub'] != pending['subject']:
        raise ValueError('登录账号发生变化，未覆盖已有账号')
    if 'chatgpt.tokens.use.direct' not in result.get('scope', '').split():
        raise ValueError('登录成功但未授予套餐推理权限，未启用 GPT')
    account = {k: result[k] for k in ['access_token', 'refresh_token', 'id_token', 'scope'] if k in result}
    account.update(client_id=issued, subject=identity['sub'], expires_at=time.time()+result.get('expires_in', 3600))
    with LOCK:
        atomic(PRIVATE/'oauth.json', account)


def to_responses(body, model, streaming):
    items = []
    for message in body.get('messages', []):
        role = message['role']
        if role == 'tool':
            items.append({'type': 'function_call_output', 'call_id': message['tool_call_id'], 'output': message.get('content', '')})
            continue
        content = message.get('content')
        if content:
            if isinstance(content, list):
                parts = []
                for part in content:
                    if part['type'] == 'text':
                        parts.append({'type': 'output_text' if role == 'assistant' else 'input_text', 'text': part['text']})
                    elif part['type'] == 'image_url':
                        parts.append({'type': 'input_image', 'image_url': part['image_url']['url']})
                    else:
                        raise ValueError('不支持的消息内容类型')
                content = parts
            items.append({'role': role, 'content': content})
        for call in message.get('tool_calls', []):
            items.append({'type': 'function_call', 'call_id': call['id'], **call['function']})
    result = {'model': model, 'input': items, 'store': False, 'stream': streaming,
              'max_output_tokens': max(128, int(body.get('max_tokens', 500)))}
    if body.get('tools'):
        result['tools'] = [{'type': 'function', **x['function']} for x in body['tools'] if x.get('type') == 'function']
    if body.get('response_format', {}).get('type') == 'json_object':
        result['text'] = {'format': {'type': 'json_object'}}
    return result


def from_responses(response):
    if response.get('status') != 'completed':
        raise ValueError('模型响应未完成，未作为成功回复返回')
    texts, calls = [], []
    for item in response.get('output', []):
        if item.get('type') == 'message':
            texts.extend(x['text'] for x in item.get('content', []) if x.get('type') == 'output_text')
        elif item.get('type') == 'function_call':
            calls.append({'id': item['call_id'], 'type': 'function', 'function': {'name': item['name'], 'arguments': item['arguments']}})
    message = {'role': 'assistant', 'content': ''.join(texts)}
    if calls:
        message['tool_calls'] = calls
    usage = response.get('usage', {})
    return {'model': response.get('model'), 'choices': [{'message': message, 'finish_reason': 'tool_calls' if calls else 'stop'}],
            'usage': {'prompt_tokens': usage.get('input_tokens', 0), 'completion_tokens': usage.get('output_tokens', 0),
                      'total_tokens': usage.get('total_tokens', 0)}}


def model_control():
    spec = importlib.util.spec_from_file_location('model_control', ROOT/'model-control.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def last_route():
    """最近一次真实路由记录，用于面板展示「上次回退原因」。"""
    if not ROUTE_LOG.exists():
        return None
    try:
        for line in reversed(ROUTE_LOG.read_text(errors='replace').strip().splitlines()[-20:]):
            try:
                return json.loads(line)
            except ValueError:
                continue
    except OSError:
        pass
    return None


def route_status():
    engine_model = ''
    try:
        engine_model = model_control().panel()['config']['ai']['model']
    except Exception:
        pass
    now = time.time()
    items = []
    for x in profiles():
        cd = cooldown_state().get(x['id']) or {}
        remaining = max(0, int(float(cd.get('until') or 0) - now))
        items.append({'id': x['id'], 'name': x['name'], 'model': x['model'],
                      'base_url': x['base_url'], 'fallback_to': x.get('fallback_to') or '',
                      'active': x['id'] == engine_model, 'cooldown': remaining,
                      'cooldown_reason': (cd.get('reason') or '') if remaining else '',
                      'has_key': bool(x.get('key_file') and Path(x['key_file']).exists())})
    return {'profiles': items, 'active': engine_model, 'last_route': last_route()}


# ---- 主面板代理：让这一个页面就能管理会话范围和运行状态 ----

def panel_entry():
    """引擎每次启动都会换端口和令牌；这里每次都从运行日志读取当前值。"""
    log = (ROOT/'run/qq-bot.log').read_text(errors='replace')
    found = re.search(r'QQ_CONTROL_URL=(http://127\.0\.0\.1:\d+)/#([A-Za-z0-9_-]+)', log)
    if not found:
        raise ValueError('主面板尚未就绪，请稍后刷新')
    return found.groups()


def napcat_entry():
    """NapCat WebUI 支持 ?token= 直接登录；令牌是固定的，不用每次手输。"""
    try:
        token = (Path.home() / 'napcat-test/secrets/webui.token').read_text().strip()
    except OSError:
        return ''
    return 'http://127.0.0.1:6099/webui/?token=' + token if token else ''


def panel_state():
    data = model_control().panel()
    config = data.get('config', {})
    targets = []
    for item in config.get('targets', []):
        key = ('group:' if item.get('group') else 'private:') + str(item.get('number'))
        targets.append({'key': key, 'number': item.get('number'), 'group': bool(item.get('group')),
                        'name': item.get('name') or '', 'enabled': bool(item.get('enabled'))})
    contacts = [{'key': c.get('key'), 'number': c.get('number'), 'group': bool(c.get('group')), 'name': c.get('name') or ''}
                for c in data.get('contacts', []) if c.get('key')]
    origin, token = panel_entry()
    return {'napcatUrl': napcat_entry(),
            'connected': bool(data.get('connected')), 'running': bool(data.get('running')),
            'busy': bool(data.get('busy')), 'status': data.get('status') or '', 'error': data.get('error') or '',
            'model': (config.get('ai') or {}).get('model', ''), 'targets': targets, 'contacts': contacts,
            'onlineEnabled': bool(config.get('onlineEnabled')), 'memoryEnabled': bool(config.get('memoryEnabled')),
            'groupParticipationEnabled': bool(config.get('groupParticipationEnabled')),
            'imageGeneration': data.get('imageGeneration') or {},
            'panelUrl': PANEL_PROXY + '/#' + private_key('panel.key')}


def panel_action(action, **fields):
    return model_control().panel(action, **fields)


def apply_scope(add, drop, enabled):
    """暂停 → 增删/保存范围 → 按原状态恢复，一次请求完成，页面不用关心这些步骤。"""
    control = model_control()
    before = control.panel()
    if before.get('busy'):
        raise ValueError('引擎正在处理消息，请稍后再试')
    was_running = bool(before.get('running'))
    deadline = before.get('runDeadline')
    if not was_running and not before.get('connected'):
        raise ValueError('QQ 未连接，无法修改范围')
    try:
        if was_running:
            control.panel('pause')
        for key in add:
            control.panel('add', target=key)
        for key in drop:
            control.panel('remove', target=key)
        control.panel('save', enabled=enabled)
    finally:
        if was_running:
            minutes = max(1, int((deadline - time.time() + 59) // 60)) if deadline else 0
            try:
                control.panel('start', singleReply=False, durationMinutes=minutes)
            except Exception:
                pass
    return panel_state()


def refresh_contacts():
    """断开重连一次，强制引擎重新拉取好友与群列表。

    引擎只在 connect 时抓一次联系人，后加的好友不会自动出现；主面板原来只能手工点
    断开再连接。这里把整套动作包起来：保持原运行状态、等真正断开才连、连不上就明确
    报错（不会静默留在断线状态）。
    """
    control = model_control()
    before = control.panel()
    if before.get('busy'):
        raise ValueError('引擎正在处理消息，请稍后再试')
    config = before.get('config') or {}
    endpoint = config.get('endpoint')
    self_id = config.get('expectedSelfID')
    if not endpoint or not self_id:
        raise ValueError('还没有保存 OneBot 地址或账号，无法重连')
    keys_before = [c.get('key') for c in (before.get('contacts') or [])]
    was_running = bool(before.get('running'))
    deadline = before.get('runDeadline')
    started = time.monotonic()
    if was_running:
        control.panel('pause')
    control.panel('disconnect')
    for _ in range(15):
        time.sleep(0.5)
        if not control.panel().get('connected'):
            break
    control.panel('connect', endpoint=endpoint, expectedSelfID=self_id, token='', key='')
    state = None
    for _ in range(30):
        time.sleep(1)
        state = control.panel()
        if state.get('connected'):
            break
    if not (state or {}).get('connected'):
        raise UpstreamError(504, '重连失败，QQ 当前未连接；请到主面板点“连接”，或运行 start-dafeiyu-autostart.sh')
    keys_after = [c.get('key') for c in (state.get('contacts') or [])]
    restored = False
    if was_running:
        minutes = max(1, int((deadline - time.time() + 59) // 60)) if deadline else 0
        try:
            control.panel('start', singleReply=False, durationMinutes=minutes)
            restored = True
        except Exception:
            restored = False
    return {'before': len(keys_before), 'after': len(keys_after),
            'added': [k for k in keys_after if k not in keys_before],
            'removed': [k for k in keys_before if k not in keys_after],
            'restored': restored, 'was_running': was_running,
            'elapsed_ms': int((time.monotonic() - started) * 1000),
            'panel': panel_state()}


class Handler(BaseHTTPRequestHandler):
    server_version = 'DaFeiYu'
    def log_message(self, *_):
        pass  # OAuth callback query contains a code. Never log request URLs.

    def reply(self, code, data, kind='application/json'):
        raw = json.dumps(data, ensure_ascii=False).encode() if kind == 'application/json' else data.encode()
        self.send_response(code)
        self.send_header('Content-Type', kind+'; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            return

    def authorized(self, gateway=False):
        expected = private_key('gateway.key' if gateway else 'admin.key')
        provided = self.headers.get('Authorization', '').removeprefix('Bearer ') if gateway else self.headers.get('x-model-control', '')
        return (hmac.compare_digest(provided, expected) and self.headers.get('Host') == '127.0.0.1:18090'
                and (gateway or self.headers.get('Origin') in [None, ORIGIN]))

    def do_GET(self):
        try:
            path = urlsplit(self.path)
            if self.headers.get('Host') != '127.0.0.1:18090':
                return self.reply(403, {'error': '拒绝非本机入口'})
            if path.path in ['/', '/hub.js']:
                name = 'hub.html' if path.path == '/' else 'hub.js'
                return self.reply(200, (HERE/name).read_text(), 'text/html' if path.path=='/' else 'text/javascript')
            if path.path == '/auth/callback':
                finish_oauth(parse_qs(path.query))
                return self.reply(200, '<p>ChatGPT 授权已保存。请关闭本页，回到模型后台查看账号模型列表。尚未自动切换聊天模型。</p>', 'text/html')
            if not self.authorized(gateway=path.path.startswith('/v1/')):
                return self.reply(403, {'error': '后台密钥无效'})
            if path.path == '/v1/models':
                return self.reply(200, {'data': [{'id': x['id']} for x in profiles()]})
            if path.path == '/api/status':
                account = read_json(PRIVATE/'oauth.json', {})
                public = [{k: v for k,v in x.items() if k != 'key_file'} | {'has_key': bool(x.get('key_file') and Path(x['key_file']).exists())}
                          for x in profiles()]
                try:
                    panel = model_control().panel()
                    engine = {k: panel[k] for k in ['connected', 'running', 'busy']}
                    engine['model'] = panel['config']['ai']['model']
                except Exception:
                    engine = {'error': '主面板暂不可读'}
                return self.reply(200, {'profiles': public, 'oauth_connected': bool(account), 'engine': engine,
                                        'route': route_status()})
            if path.path == '/api/route':
                return self.reply(200, route_status())
            if path.path == '/api/usage':
                # 只读用量：数据只来自 provider 返回的 usage，不含正文与密钥。
                if _USAGE is None:
                    return self.reply(200, {'available': False, 'reason': '本次启动未加载记账模块'})
                try:
                    with LOCK:
                        return self.reply(200, _USAGE.dashboard())
                except Exception:
                    return self.reply(200, {'available': False, 'reason': '用量账本暂不可读'})
            if path.path == '/api/panel':
                return self.reply(200, panel_state())
            if path.path == '/api/limits':
                return self.reply(200, limit_control().limits())
            if path.path == '/api/proxy/status':
                proxy_root = Path.home()/'.local/share/dafeiyu-proxy'
                controller = (proxy_root/'controller.key').read_text().strip()
                data = http('GET', 'http://127.0.0.1:19090/proxies', controller)
                group = data['proxies']['AI-US']
                recovery = read_json(ROOT/'run/recovery-state.json', {})
                return self.reply(200, {'selected': group['now'], 'nodes': group['all'],
                    'recovery': recovery.get('status', 'unknown')})
            return self.reply(404, {'error': '页面不存在'})
        except Exception as error:
            self.reply(getattr(error, 'status', 400), {'error': str(error) if isinstance(error, (ValueError, UpstreamError)) else '操作未完成，请回到后台检查'})

    def do_POST(self):
        gateway = self.path.startswith('/v1/')
        if not self.authorized(gateway):
            return self.reply(403, {'error': '后台密钥或来源无效'})
        if self.headers.get('Transfer-Encoding') or not self.headers.get('Content-Type', '').startswith('application/json'):
            return self.reply(400, {'error': '需要 JSON 请求'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 16_000_000:
                return self.reply(413, {'error': '请求长度无效'})
            if not SLOTS.acquire(blocking=False):
                return self.reply(429, {'error': '后台忙，请稍后再试'})
            try:
                body = json.loads(self.rfile.read(length))
                return self.post(body)
            finally:
                SLOTS.release()
        except Exception as error:
            self.reply(getattr(error, 'status', 400), {'error': str(error) if isinstance(error, (ValueError, UpstreamError)) else '操作未完成；未自动重试'})

    def post(self, body):
        if self.path == '/api/limits':
            with LOCK:
                controller = limit_control()
                message = controller.set_limit(str(body.get('target') or ''), str(body.get('value', '')))
                return self.reply(200, {'message':message, 'limits':controller.limits()})
        if self.path == '/v1/chat/completions':
            return self.reply(200, complete(body))
        if self.path == '/api/oauth/start':
            return self.reply(200, {'url': start_oauth()})
        if self.path == '/api/panel/action':
            action = body.get('action')
            if action not in ['start', 'pause']:
                raise ValueError('只支持开始或暂停')
            if action == 'start':
                panel_action('start', singleReply=False, durationMinutes=int(body.get('durationMinutes') or 0))
            else:
                panel_action('pause')
            return self.reply(200, panel_state())
        if self.path == '/api/scope':
            def keys(name):
                return [x for x in (body.get(name) or []) if isinstance(x, str)][:20]
            return self.reply(200, apply_scope(keys('add'), keys('drop'), keys('enabled')))
        if self.path == '/api/contacts/refresh':
            return self.reply(200, refresh_contacts())
        if self.path == '/api/oauth/models':
            result = http('GET', 'https://api.openai.com/v1/models', oauth_access(), True)
            data = result.get('models', result.get('data', []))
            ids = [x.get('slug', x.get('id')) for x in data if x.get('visibility', 'list') == 'list']
            return self.reply(200, {'models': ids})
        if self.path == '/api/proxy/select':
            proxy_root = Path.home()/'.local/share/dafeiyu-proxy'
            controller = (proxy_root/'controller.key').read_text().strip()
            group = http('GET', 'http://127.0.0.1:19090/proxies', controller)['proxies']['AI-US']
            if body.get('name') not in group['all']:
                raise ValueError('请从已配置的美国 AI 节点中选择')
            http('PUT', 'http://127.0.0.1:19090/proxies/AI-US', controller, body={'name':body['name']})
            return self.reply(200, {'ok':True})
        if self.path == '/api/profiles/save':
            item = {k: body[k] for k in ['id', 'name', 'model', 'base_url', 'protocol', 'proxy', 'idle_effort', 'reasoning', 'fallback_to'] if k in body}
            validate_profile(item)
            if item.get('fallback_to'):
                if item['fallback_to'] == item['id']:
                    raise ValueError('备用模型不能是自己')
                if not any(x['id'] == item['fallback_to'] for x in profiles()):
                    raise ValueError('备用模型不存在')
            with LOCK:
                items = profiles()
                previous = next((x for x in items if x['id'] == item['id']), {})
                panel = model_control().panel()
                if (panel['running'] or panel.get('busy')) and panel['config']['ai']['model'] == item['id']:
                    raise ValueError('请先暂停自动回复，再修改当前使用中的接口配置')
                secret = body.get('key', '').strip()
                if len(secret)>8192 or any(c.isspace() for c in secret):
                    raise ValueError('API Key 格式无效')
                if (item['protocol'] != 'oauth' and not secret and previous.get('key_file')
                        and previous['base_url'].rstrip('/') != item['base_url'].rstrip('/')):
                    raise ValueError('更换接口地址时，请重新填写该服务的 API Key；不会把旧密钥转发给新接口')
                if item['protocol'] != 'oauth':
                    if secret:
                        secret_path = PRIVATE / (item['id']+'.key')
                        secret_path.write_text(secret); os.chmod(secret_path, 0o600)
                        item['key_file'] = str(secret_path)
                    elif previous.get('key_file'):
                        item['key_file'] = previous['key_file']
                    else:
                        raise ValueError('请填写 API Key')
                # Updating an active profile could route the next request to a different endpoint.
                updated = [x for x in items if x['id'] != item['id']] + [item]
                atomic(PRIVATE/'profiles.json', {'profiles': updated})
            return self.reply(200, {'ok': True})
        if self.path == '/api/profiles/activate':
            item = next((x for x in profiles() if x['id'] == body.get('id')), None)
            if not item:
                raise ValueError('配置不存在')
            if item['protocol'] == 'oauth':
                oauth_access()
            with LOCK:
                model_control().switch(item['id'], check_catalog=False)
            return self.reply(200, {'ok': True})
        if self.path == '/api/profiles/test':
            item = next((x for x in profiles() if x['id'] == body.get('id')), None)
            if not item:
                raise ValueError('配置不存在')
            result = complete({'model': item['id'], 'messages': [{'role':'user','content':'Reply only OK.'}], 'max_tokens': 16})
            if not result.get('choices'):
                raise ValueError('模型未返回有效结果')
            return self.reply(200, {'ok': True, 'model': result.get('model'), 'usage': result.get('usage')})
        return self.reply(404, {'error': '操作不存在'})


class PanelProxy(BaseHTTPRequestHandler):
    """主面板的反向代理。

    引擎每次启动都会换随机端口和随机令牌，导致主面板链接必然过期。这里用固定端口
    18091 + 固定短密钥转一层，把 Host / Origin / x-qq-control 换成引擎当前的真实值，
    用户侧地址就永远不变。
    """

    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        return

    def do_GET(self):
        self.forward('GET')

    def do_POST(self):
        self.forward('POST')

    def send_bytes(self, code, data, content_type):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            return

    def forward(self, method):
        length = int(self.headers.get('content-length') or 0)
        body = self.rfile.read(length) if length else None
        try:
            origin, token = panel_entry()
        except Exception:
            self.send_bytes(503, '{"error":"主面板尚未就绪，请稍后刷新"}'.encode(), 'application/json')
            return
        if self.path.startswith('/api/'):
            provided = self.headers.get('x-qq-control', '')
            if not hmac.compare_digest(provided, private_key('panel.key')):
                self.send_bytes(403, '{"error":"Unauthorized"}'.encode(), 'application/json')
                return
        headers = {'x-qq-control': token, 'origin': origin, 'host': origin.split('//', 1)[1],
                   'content-type': self.headers.get('content-type') or 'application/json'}
        request = urllib.request.Request(origin + self.path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                self.send_bytes(response.status, response.read(), response.headers.get('Content-Type') or 'application/json')
        except urllib.error.HTTPError as error:
            self.send_bytes(error.code, error.read() or b'{}', error.headers.get('Content-Type') or 'application/json')
        except Exception:
            self.send_bytes(502, '{"error":"主面板连接失败"}'.encode(), 'application/json')


if __name__ == '__main__':
    initialize()
    proxy = ThreadingHTTPServer(('127.0.0.1', 18091), PanelProxy)
    proxy.daemon_threads = True
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    print('panel proxy ready on loopback:18091', flush=True)
    server = ThreadingHTTPServer(('127.0.0.1', 18090), Handler)
    server.daemon_threads = True
    print('model-hub ready on loopback:18090', flush=True)
    server.serve_forever()
