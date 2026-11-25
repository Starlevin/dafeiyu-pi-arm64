"""Verify live Swift policy reload through the console adapter with no chat traffic."""
import argparse
from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import re
import runpy
import select
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'integrations/pi-console/server'))
import unified_console as console

logic = runpy.run_path(str(ROOT / 'scripts/test-qq-ingress-persistence.py'))
await_state, initial_state, stop = (logic[name] for name in ('await_state', 'initial_state', 'stop'))


def read_status(log):
    match = re.search(r'QQ_CONTROL_URL=http://127\.0\.0\.1:(\d+)/#([0-9A-Za-z-]+)', log.read_text())
    if not match:
        return {}
    port, token = match.groups()
    handle = http.client.HTTPConnection('127.0.0.1', int(port), timeout=2)
    try:
        handle.request('GET', '/api/status', headers={
            'x-qq-control': token, 'Origin': f'http://127.0.0.1:{port}'
        })
        reply = handle.getresponse()
        return json.load(reply) if reply.status == 200 else {}
    finally:
        handle.close()


@contextmanager
def core(binary, root, phase):
    fixture = ROOT / 'Tests/QQEngineTests/onebot_fixture.py'
    run_dir = root / phase
    run_dir.mkdir()
    (run_dir / 'message-shape').write_text('groupParticipation')
    peer = subprocess.Popen([sys.executable, '-B', str(fixture), str(run_dir)],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert select.select([peer.stdout], [], [], 5)[0]
        port = int(peer.stdout.readline())
        config_file = root / 'qq-state.json'
        state = json.loads(config_file.read_bytes()) if config_file.is_file() else initial_state()
        state['config']['endpoint'] = f'ws://127.0.0.1:{port}'
        config_file.write_text(json.dumps(state))
        env = {'PATH': os.environ['PATH'], 'HOME': str(run_dir),
               'LD_LIBRARY_PATH': os.environ.get('LD_LIBRARY_PATH', ''),
               'DAFEIYU_DATA_DIR': str(root), 'DAFEIYU_AUTO_RESUME': '1',
               'DAFEIYU_ONEBOT_TOKEN': 'synthetic-test-token', 'DAFEIYU_LLM_API_KEY': 'fixture',
               'DAFEIYU_LLM_PROVIDER': 'custom', 'DAFEIYU_LLM_BASE_URL': 'http://127.0.0.1:9/v1',
               'DAFEIYU_DECISION_CONFIG_FILE': str(root / 'decision.json'),
               'DAFEIYU_PROACTIVE_GROUP_MODES': '99999=quiet,88888=active',
               'DAFEIYU_REPLY_DECISION_V2': 'enforce'}
        log = root / 'run/qq-bot.log'
        log.parent.mkdir(exist_ok=True)
        descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'wb') as output:
            engine = subprocess.Popen([str(binary)], cwd=run_dir, env=env, stdout=output, stderr=output)
        try:
            await_state(lambda: read_status(log).get('running') is True, 'QQ did not become ready')
            assert not (root / 'qq-admin/admin_core.py').exists()
            yield
            assert 'send_group_msg' not in (run_dir / 'actions').read_text()
        finally:
            stop(engine)
    finally:
        stop(peer)


@contextmanager
def authenticated_console(root):
    cfg = {'stateDir': str(root / 'console-state'), 'qqRoot': str(root),
           'qqDecisionConfigFile': str(root / 'decision.json'), 'uiDir': str(ROOT / 'integrations/pi-console/ui'),
           'loopPort': 0, 'lanPort': 0, 'lanHost': '127.0.0.1', 'kdfIterations': 1000}
    app = console.App(cfg)
    password = 'isolated-console-policy-fixture'
    app.passwords.set(password)
    with console.ConsoleServer(('127.0.0.1', 0), console.Handler, app) as server:
        app.loop_port = server.server_port
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        origin = f'http://127.0.0.1:{server.server_port}'
        cookie = None

        def request(path, data=None):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=6)
            headers = {'Origin': origin, 'Content-Type': 'application/json'}
            if cookie:
                headers['Cookie'] = cookie
            try:
                connection.request('GET' if data is None else 'POST', path,
                                   None if data is None else json.dumps(data), headers)
                response = connection.getresponse()
                return response.status, json.load(response), response.getheader('Set-Cookie')
            finally:
                connection.close()

        def policy_request(data=None):
            status, body, _ = request('/api/proactive/config', data)
            assert status == 200
            return body

        try:
            assert request('/api/proactive/config')[0] == 401
            status, _, value = request('/api/login', {'password': password})
            assert status == 200 and value
            cookie = value.split(';', 1)[0]
            yield policy_request
        finally:
            server.shutdown()
            worker.join(timeout=3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=lambda argument: Path(argument).resolve(strict=True))
    binary = parser.parse_args().binary
    with tempfile.TemporaryDirectory(prefix='qq-policy-isolated-') as directory:
        root = Path(directory)
        policy = root / 'decision.json'
        group, other = 'group:99999', 'group:88888'
        policy.write_text(json.dumps({group: {'mode': 'enforce', 'threshold': 0.45,
                                               'ordinaryReplyEnabled': True, 'publicQuestionEnabled': True},
                                      other: {'mode': 'enforce', 'threshold': 0.52, 'ordinaryReplyEnabled': True}}))
        with core(binary, root, 'startup'), authenticated_console(root) as read_policy:
            current = read_policy()['qqRuntime']
            assert current['verification'] == 'applied' and current['running'] is True
            assert current['groups'][group] == {'ordinary': True, 'publicQuestion': True, 'threshold': 0.45}
            log = root / 'run/qq-bot.log'
            try:
                log.chmod(0o644)
                assert read_policy()['qqRuntime']['verification'] == 'unavailable'
            finally:
                log.chmod(0o600)
            original_other = current['groups'][other]
            result = read_policy({'platform': 'qq', 'group': group,
                                  'ordinaryReplyEnabled': False, 'threshold': 0.61})
            assert result['verification'] == 'persisted'
            current = read_policy()['qqRuntime']
            expected = {'ordinary': False, 'publicQuestion': True, 'threshold': 0.61}
            assert current['verification'] == 'applied' and current['groups'][group] == expected
            assert current['groups'][other] == original_other
            saved = policy.read_bytes()
            document = json.loads(saved)
            document[group]['threshold'] = 'bad'
            policy.write_text(json.dumps(document))
            assert read_policy()['qqRuntime']['verification'] == 'pending'
            policy.write_bytes(saved)
        with core(binary, root, 'restart'), authenticated_console(root) as read_policy:
            after = read_policy()['qqRuntime']
            assert after['verification'] == 'applied' and after['groups'][group] == expected
            assert after['groups'][other] == original_other
        print('QQ_POLICY_RUNTIME=PASS immediate_reload=PASS stale_revision=PASS restart=PASS other_group=PASS '
              'authenticated_http=PASS standalone_loopback=PASS private_log=PASS outbound_sends=0', flush=True)


if __name__ == '__main__':
    main()
