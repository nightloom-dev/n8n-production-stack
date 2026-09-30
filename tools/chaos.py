#!/usr/bin/env python3
"""chaos run: load the stack, break its parts one by one, see what each break costs

    tools/chaos.py [--instance local] [--only SCENARIO ...]

requests go through Caddy to the "chaos echo" workflow (webhook, JS Code node, Python Code node),
so each one hits the queue, a worker and both runners. every answer is checked

  baseline          no faults, just throughput and latency: one at a time, then 20 at once
  worker-restart    worker-1 restarted (SIGTERM), it finishes its work and worker-2 takes the rest
  worker-kill       worker-1's n8n gets SIGKILL. its executions fail as stalled, docker brings it back,
                    senders get an error and resend, ExecutionsLost lands in Telegram
  main-down         main stopped, webhooks keep working, TargetDown lands in Telegram
  redis-restart     Redis restarted under load, failed requests are resent
  postgres-restart  same for Postgres
  deploy            new version pushed with n8nctl under load, then rolled back
  restore           fresh backup restored into a scratch db, n8n there must have the same workflows
                    and decrypt the credentials to the same values

during the run Alertmanager talks to a fake Telegram on this machine so every alert gets recorded,
then it goes back to TELEGRAM_API_URL from .env. alerts already pending or firing at the start
(a full disk, say) are ignored. run tools/setup.py first. stdlib only
"""
import argparse
import hashlib
import http.server
import itertools
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, namedtuple
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import n8nctl  # noqa: E402

T0 = time.time()
SINK_PORT = 8099

# fingerprint of an n8n db as one JSON line: workflows plus credentials decrypted with this
# container's key. hashes only, no secret leaves the container
FINGERPRINT = r"""set -e
trap 'rm -f /tmp/c.json /tmp/w.json' EXIT
n8n export:credentials --all --decrypted --output=/tmp/c.json >/dev/null
n8n export:workflow --all --output=/tmp/w.json >/dev/null
node -e '
const fs = require("fs"), sha = x => require("crypto").createHash("sha256").update(JSON.stringify(x)).digest("hex");
const c = JSON.parse(fs.readFileSync("/tmp/c.json")).map(x => [x.id, x.name, x.type, x.data]).sort();
const w = JSON.parse(fs.readFileSync("/tmp/w.json")).map(x => [x.id, x.name, x.nodes, x.connections]).sort();
console.log(JSON.stringify({workflows: w.length, workflows_sha256: sha(w), credentials: c.length,
                            credentials_sha256: sha(c)}));'
"""


def log(msg):
    print(f'[{time.time() - T0:4.0f}s] {msg}', flush=True)


def compose(*args, env=None):
    r = subprocess.run(['docker', 'compose', *args], cwd=ROOT, capture_output=True, text=True, env=env)
    if r.returncode:
        sys.exit(f'docker compose {" ".join(args)} failed:\n{r.stderr}')
    return r.stdout.strip()


def inspect(service, fmt):
    return subprocess.run(['docker', 'inspect', '-f', fmt, compose('ps', '-q', service)],
                          capture_output=True, text=True, check=True).stdout.strip()


def healthy(service):
    return inspect(service, '{{.State.Health.Status}}') == 'healthy'


def wait_until(check, timeout, what):
    end = time.time() + timeout
    while not check():
        if time.time() > end:
            sys.exit(f'gave up waiting for {what}')
        time.sleep(1)


class Echo:
    """one request to the echo workflow: ('ok', version) or (what went wrong, None)"""

    def __init__(self, api):
        self.url, self.tls = api.base.removesuffix('/api/v1') + '/webhook/chaos/echo', api.tls

    def __call__(self, rid, sleep_ms=0, timeout=120):
        req = urllib.request.Request(self.url, data=json.dumps({'id': rid, 'sleep_ms': sleep_ms}).encode(),
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, context=self.tls, timeout=timeout) as r:
                body = json.loads(r.read())
        except urllib.error.HTTPError as e:
            return f'HTTP {e.code}', None
        except (OSError, ValueError) as e:  # refused, reset, timed out, not JSON
            return type(e).__name__, None
        good = body.get('js') == hashlib.sha256(rid.encode()).hexdigest()[:12] and body.get('py') == rid[::-1]
        if not good:
            log(f'wrong answer to {rid}: {json.dumps(body)[:300]}')
            return 'wrong answer', None
        return 'ok', body.get('version')


Result = namedtuple('Result', 'sent seconds first first_s outcome version')


class Load:
    """requests from `concurrency` threads until stop(). with resend a failed one is sent again
    every 5 s, up to 4 more times, like Stripe and most webhook senders do"""

    def __init__(self, echo, name, concurrency, sleep_ms=0, resend=False):
        self.echo, self.name, self.sleep_ms, self.resend = echo, name, sleep_ms, resend
        self.results, self.done, self.ids, self.started = [], threading.Event(), itertools.count(), time.time()
        self.threads = [threading.Thread(target=self.loop, daemon=True) for _ in range(concurrency)]
        for t in self.threads:
            t.start()

    def loop(self):
        while not self.done.is_set():
            rid, sent = f'{self.name}-{next(self.ids)}', time.time()
            first, version = outcome, _ = self.echo(rid, self.sleep_ms)
            first_s = time.time() - sent
            for _ in range(4 if self.resend and outcome != 'ok' else 0):
                time.sleep(5)
                outcome, version = self.echo(rid, self.sleep_ms)
                if outcome == 'ok':
                    break
            self.results.append(Result(sent, time.time() - sent, first, first_s, outcome, version))

    def last_version(self):
        ok = [r for r in self.results if r.outcome == 'ok']
        return max(ok).version if ok else None

    def stop(self):
        """wait for requests in flight, then sum up"""
        self.done.set()
        for t in self.threads:
            t.join()
        wall = time.time() - self.started
        fast = sorted(r.seconds for r in self.results if r.first == 'ok')
        summary = {'requests': len(self.results), 'per_second': round(len(self.results) / wall, 1),
                   'p50_ms': round(fast[len(fast) // 2] * 1000) if fast else None,
                   'p95_ms': round(fast[int(len(fast) * 0.95)] * 1000) if fast else None,
                   'failed': dict(Counter(r.first for r in self.results if r.first != 'ok'))}
        if slowest := max((r.first_s for r in self.results if r.first != 'ok'), default=None):
            summary['failed_after_s'] = round(slowest, 1)  # longest a sender waited before getting a failure
        if self.resend:
            summary['failed_after_resending'] = dict(Counter(r.outcome for r in self.results if r.outcome != 'ok'))
        return summary


def last_execution(c):
    data = c.api('GET', '/executions', workflowId=c.wid, limit=1)['data']
    return int(data[0]['id']) if data else 0


def left_running(c, after):
    """echo executions after id `after` still shown as running when every request got its answer"""
    return sum(int(e['id']) > after for e in c.api.all('/executions', workflowId=c.wid, status='running'))


def failed_executions(c, after):
    """echo executions after id `after` that ended in error or crashed"""
    return len({e['id'] for status in ('error', 'crashed')
                for e in c.api.all('/executions', workflowId=c.wid, status=status) if int(e['id']) > after})


class Telegram(http.server.BaseHTTPRequestHandler):
    """fake api.telegram.org, keeps what Alertmanager sends"""
    messages = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0)).decode()
        if self.path.endswith('/sendMessage'):
            try:
                text = json.loads(body)['text']
            except ValueError:
                text = urllib.parse.parse_qs(body).get('text', [''])[0]
            Telegram.messages.append((round(time.time() - T0), text))
            log(f'telegram: {text.strip()}')
        answer = json.dumps({'ok': True, 'result': {'message_id': len(Telegram.messages), 'date': int(time.time()),
                                                    'chat': {'id': 0, 'type': 'private'}}}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def log_message(self, *_):
        pass


def fired(alert, text):
    return any(f'FIRING {alert}' in m and text in m for _, m in Telegram.messages)


def unresolved():
    """alerts whose last message was FIRING, like 'TargetDown main:5678'"""
    last = {}
    for _, m in Telegram.messages:
        for line in m.splitlines():  # FIRING TargetDown main:5678: n8n does not answer
            state, _, what = line.partition(' ')
            last[what.split(': ')[0]] = state
    return {what for what, state in last.items() if state == 'FIRING'}


def active_alerts():
    """alerts pending or firing in Prometheus, named like in unresolved()"""
    url = f"http://{n8nctl.read_env(ROOT / '.env').get('PROMETHEUS_PUBLISH', '127.0.0.1:9090')}/api/v1/alerts"
    with urllib.request.urlopen(url, timeout=10) as r:
        alerts = json.load(r)['data']['alerts']
    return {' '.join(filter(None, (a['labels']['alertname'], a['labels'].get('instance')))) for a in alerts}


def baseline(c):
    alone = Load(c.echo, 'alone', 1)
    wait_until(lambda: len(alone.results) >= 50, 120, '50 requests one at a time')
    alone = alone.stop()
    load = Load(c.echo, 'baseline', 20, resend=True)
    wait_until(lambda: len(load.results) >= 2000, 600, '2000 requests')
    return {**load.stop(), 'one_at_a_time': alone}


def worker_restart(c):
    load = Load(c.echo, 'restart', 10, sleep_ms=2000)
    time.sleep(10)
    t = time.time()
    compose('restart', 'worker-1')
    wait_until(lambda: healthy('worker-1'), 180, 'worker-1 to be healthy')
    back = time.time() - t
    time.sleep(5)
    return {**load.stop(), 'worker_back_after_s': round(back, 1)}


def worker_kill(c):
    after = last_execution(c)
    load = Load(c.echo, 'kill', 10, sleep_ms=3000, resend=True)
    time.sleep(10)
    started = inspect('worker-1', '{{.State.StartedAt}}')
    tini = inspect('worker-1', '{{.State.Pid}}')
    node = int(Path(f'/proc/{tini}/task/{tini}/children').read_text().split()[0])
    os.kill(node, signal.SIGKILL)  # same as the OOM killer would do
    t = time.time()
    log('killed n8n in worker-1')
    wait_until(lambda: inspect('worker-1', '{{.State.StartedAt}}') != started and healthy('worker-1'), 180,
               'worker-1 to come back')
    back = time.time() - t
    time.sleep(5)
    stats = load.stop()  # returns when the requests caught by the kill have failed and been resent
    wait_until(lambda: fired('ExecutionsLost', ''), 300, 'the ExecutionsLost alert')
    return {**stats, 'worker_back_after_s': round(back, 1), 'executions_failed': failed_executions(c, after),
            'alert_after_s': round(next(s for s, m in Telegram.messages if 'FIRING ExecutionsLost' in m) - (t - T0))}


def main_down(c):
    compose('stop', 'main')
    t = time.time()
    log('main stopped')
    load = Load(c.echo, 'main-down', 5)
    wait_until(lambda: fired('TargetDown', 'main:5678'), 300, 'the TargetDown alert for main')
    alerted = time.time() - t
    stats = load.stop()
    compose('start', 'main')
    wait_until(lambda: healthy('main'), 180, 'main to be healthy')
    return {**stats, 'main_down_s': round(time.time() - t), 'alert_after_s': round(alerted)}


def restart_under_load(c, service):
    after = last_execution(c)
    load = Load(c.echo, service, 10, sleep_ms=1000, resend=True)
    time.sleep(10)
    t = time.time()
    compose('restart', service)
    wait_until(lambda: healthy(service), 120, f'{service} to be healthy')
    back = time.time() - t
    time.sleep(20)
    stats = load.stop()
    return {**stats, 'restart_s': round(back, 1), 'executions_failed': failed_executions(c, after),
            'executions_left_running': left_running(c, after)}


def n8nctl_run(*args):
    return subprocess.run([sys.executable, ROOT / 'n8nctl.py', *map(str, args)], capture_output=True, text=True)


def deploy(c):
    work = Path(tempfile.mkdtemp(prefix='chaos-deploy-'))
    shutil.copytree(ROOT / 'workflows', work, dirs_exist_ok=True)
    f = work / 'chaos-echo.json'
    f.write_text(f.read_text().replace('version: 1', 'version: 2'))
    load = Load(c.echo, 'deploy', 5)
    time.sleep(3)
    t = time.time()
    if (r := n8nctl_run('push', c.instance, work, '-m', 'chaos: version 2')).returncode:
        sys.exit(r.stdout + r.stderr)
    wait_until(lambda: load.last_version() == 2, 60, 'version 2 to answer')
    to_v2 = time.time() - t
    time.sleep(3)
    t = time.time()
    if (r := n8nctl_run('rollback', c.instance, work)).returncode:
        sys.exit(r.stdout + r.stderr)
    wait_until(lambda: load.last_version() == 1, 60, 'version 1 to answer again')
    to_v1 = time.time() - t
    time.sleep(3)
    stats = load.stop()
    shutil.copytree(work / '.n8nctl', ROOT / 'workflows' / '.n8nctl', dirs_exist_ok=True)  # bring n8nctl's state back to the repo
    shutil.rmtree(work)
    return {**stats, 'v2_live_after_s': round(to_v2, 1), 'v1_back_after_s': round(to_v1, 1),
            'matches_git_after_rollback': n8nctl_run('diff', c.instance, ROOT / 'workflows').returncode == 0}


def restore(c):
    t = time.time()
    compose('exec', '-T', 'backup', 'backup.sh', 'run')
    backup_s = time.time() - t
    live = json.loads(compose('exec', '-T', 'main', 'sh', '-c', FINGERPRINT).splitlines()[-1])
    t = time.time()
    try:
        restored = json.loads(compose('--profile', 'drill', 'run', '--rm', '-T', '--entrypoint', 'sh', 'drill-n8n',
                                      '-c', FINGERPRINT).splitlines()[-1])
    finally:
        compose('--profile', 'drill', 'rm', '--stop', '--force', 'drill-postgres', 'drill-restore')
        subprocess.run(['docker', 'volume', 'rm', '--force', 'n8n_drill-key'], capture_output=True)
    return {'backup_s': round(backup_s, 1), 'restore_and_read_s': round(time.time() - t, 1),
            'live': live, 'restored': restored}


SCENARIOS = {
    'baseline': baseline,
    'worker-restart': worker_restart,
    'worker-kill': worker_kill,
    'main-down': main_down,
    'redis-restart': lambda c: restart_under_load(c, 'redis'),
    'postgres-restart': lambda c: restart_under_load(c, 'postgres'),
    'deploy': deploy,
    'restore': restore,
}


def problems(r, ignored):
    """what each scenario must show to pass"""
    found = []
    for name in ('worker-restart', 'main-down', 'deploy'):
        if name in r and r[name]['failed']:
            found.append(f'{name}: requests failed: {r[name]["failed"]}')
    for name in ('baseline', 'worker-kill', 'redis-restart', 'postgres-restart'):
        if name in r and r[name]['failed_after_resending']:
            found.append(f'{name}: requests failed even when sent again: {r[name]["failed_after_resending"]}')
    if 'deploy' in r and not r['deploy']['matches_git_after_rollback']:
        found.append('deploy: the instance differs from workflows/ after the rollback')
    if 'restore' in r and r['restore']['live'] != r['restore']['restored']:
        found.append('restore: the restored database differs from the live one')
    found += [f'alert never resolved: {a}' for a in sorted(unresolved() - ignored)]
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--instance', default='local', help='n8nctl name of the stack')
    ap.add_argument('--only', nargs='+', choices=SCENARIOS, help='run just these scenarios')
    args = ap.parse_args()
    api = n8nctl.Api(args.instance)

    if (r := n8nctl_run('push', args.instance, ROOT / 'workflows', '-m', 'chaos run')).returncode:
        sys.exit(r.stdout + r.stderr)
    c = SimpleNamespace(api=api, echo=Echo(api), instance=args.instance,
                        wid=next(w['id'] for w in api.workflows() if w['name'] == 'chaos echo'))
    wait_until(lambda: c.echo('warm-up')[0] == 'ok', 120, 'the echo workflow to answer')
    if not any(cr['name'] == 'restore drill' for cr in api.all('/credentials')):  # so the drill has something to decrypt
        api('POST', '/credentials', {'name': 'restore drill', 'type': 'httpHeaderAuth',
                                     'data': {'name': 'X-Drill', 'value': os.urandom(16).hex()}})

    gateway = subprocess.run(['docker', 'network', 'inspect', 'bridge', '-f', '{{(index .IPAM.Config 0).Gateway}}'],
                             capture_output=True, text=True, check=True).stdout.strip()  # host.docker.internal
    sink = http.server.ThreadingHTTPServer((gateway, SINK_PORT), Telegram)
    threading.Thread(target=sink.serve_forever, daemon=True).start()
    compose('up', '--detach', '--wait', 'alertmanager',
            env={**os.environ, 'TELEGRAM_API_URL': f'http://host.docker.internal:{SINK_PORT}'})
    ignored = active_alerts()
    if ignored:
        log(f'already active, left out: {", ".join(sorted(ignored))}')
    results = {}
    try:
        for name, run in SCENARIOS.items():
            if args.only and name not in args.only:
                continue
            log(f'--- {name}')
            results[name] = run(c)
            log(json.dumps(results[name]))
        log('waiting for the alerts to resolve')
        end = time.time() + 1500  # ExecutionErrors looks 15 min back, and RESOLVED waits for group_interval
        while unresolved() - ignored and time.time() < end:
            time.sleep(5)
    finally:
        compose('up', '--detach', 'alertmanager')  # back to the Telegram from .env
        sink.shutdown()
    results['telegram'] = Telegram.messages
    results['alerts_left_out'] = sorted(ignored)
    print(json.dumps(results, indent=2))
    if found := problems(results, ignored):
        sys.exit('chaos: FAILED\n  ' + '\n  '.join(found))
    print('chaos: OK')


if __name__ == '__main__':
    main()
