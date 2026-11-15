from pathlib import Path
import os


def configured_path(variable, fallback):
    value = os.environ.get(variable, '').strip()
    return Path(value).expanduser() if value else Path(fallback).expanduser()


def runtime_home():
    return configured_path(
        'DAFEIYU_WECHAT_HOME',
        Path.home() / '.local' / 'share' / 'dafeiyu-wechat',
    )


def core_home():
    return configured_path('DAFEIYU_WECHAT_CORE_HOME', runtime_home() / 'core')


def staging_home():
    return configured_path('DAFEIYU_WECHAT_STAGING_HOME', runtime_home() / 'staging')


def client_runtime_home():
    return configured_path(
        'DAFEIYU_WECHAT_CLIENT_RUNTIME', runtime_home() / 'client-session' / 'runtime')


def config_home():
    return configured_path(
        'DAFEIYU_WECHAT_CONFIG_HOME',
        Path.home() / '.config' / 'dafeiyu-wechat',
    )


def state_home():
    xdg_state = os.environ.get('XDG_STATE_HOME', '').strip()
    base = Path(xdg_state).expanduser() if xdg_state else Path.home() / '.local' / 'state'
    return configured_path('DAFEIYU_WECHAT_STATE_HOME', base / 'dafeiyu-wechat')


def configured_group(name):
    return os.environ.get(f'DAFEIYU_WECHAT_{name.upper()}', '').strip()


def token_file():
    return configured_path(
        'DAFEIYU_WECHAT_TOKEN_FILE',
        config_home() / 'config.json',
    )
