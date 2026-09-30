#!/usr/bin/env python3
"""bring the stack up on this machine and point n8nctl at it

    tools/setup.py [--instance local] [--owner-email admin@example.com]

1. fill secrets/ with random values for whatever is missing. secrets/telegram_bot_token gets a
   placeholder, put the real bot token there. .env is copied from .env.example if there's none
2. docker compose up --build --wait
3. create the n8n owner (password in secrets/owner_password) and an API key for n8nctl, saved to
   ~/.config/n8nctl/INSTANCE.env. for a local SITE_ADDRESS that file also points at Caddy's root cert
4. take the first backup so the backup alerts have something to look at

safe to rerun: keeps existing secrets, the owner and a working key. stdlib only
"""
import argparse
import json
import secrets
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from n8nctl import read_env  # noqa: E402

GENERATED = ['postgres_password', 'n8n_encryption_key', 'runners_auth_token', 'restic_password',
             'grafana_admin_password', 'owner_password']


def compose(*args, **kw):
    return subprocess.run(['docker', 'compose', *args], cwd=ROOT, text=True, **kw)


def make_secrets():
    d = ROOT / 'secrets'
    d.mkdir(mode=0o700, exist_ok=True)
    for name in GENERATED + ['telegram_bot_token']:
        p = d / name
        if p.exists():
            continue
        if name == 'telegram_bot_token':
            value = '0:placeholder'
        elif name == 'owner_password':
            value = 'N8n-' + secrets.token_urlsafe(24)  # n8n wants a digit and an uppercase letter
        else:
            value = secrets.token_urlsafe(32)
        p.write_text(value)  # no newline, n8n complains about whitespace in _FILE values
        # containers read them as their own users, the dir mode keeps everyone else out
        p.chmod(0o600 if name == 'owner_password' else 0o644)
        print(f'created secrets/{name}')
    env = ROOT / '.env'
    if not env.exists():
        shutil.copy(ROOT / '.env.example', env)
        env.chmod(0o600)
        print('created .env from .env.example')


class N8n:
    def __init__(self, url, cafile):
        self.url, self.tls, self.cookie = url, ssl.create_default_context(cafile=cafile), None

    def __call__(self, method, path, body=None, headers=None):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', **(headers or {}),
                                              **({'Cookie': self.cookie} if self.cookie else {})})
        with urllib.request.urlopen(req, context=self.tls, timeout=30) as r:
            for c in r.headers.get_all('Set-Cookie') or []:
                if c.startswith('n8n-auth='):
                    self.cookie = c.split(';')[0]
            return json.loads(r.read() or b'null')


def connect_n8nctl(instance, owner_email):
    env = read_env(ROOT / '.env')
    url = env['PUBLIC_URL'].rstrip('/')
    conf = Path.home() / '.config' / 'n8nctl'
    conf.mkdir(parents=True, exist_ok=True)
    cafile = None
    root_cert = compose('exec', '-T', 'caddy', 'cat', '/data/caddy/pki/authorities/local/root.crt',
                        capture_output=True)
    if root_cert.returncode == 0:  # site cert is from Caddy's own CA
        cafile = conf / f'{instance}-ca.crt'
        cafile.write_text(root_cert.stdout)
    n8n = N8n(url, cafile)

    target = conf / f'{instance}.env'
    if target.exists():
        try:
            n8n('GET', '/api/v1/workflows?limit=1', headers={'X-N8N-API-KEY': read_env(target)['N8N_API_KEY']})
            print(f'{target} already works')
            return
        except (urllib.error.HTTPError, KeyError):
            pass

    password = (ROOT / 'secrets' / 'owner_password').read_text()
    try:
        n8n('POST', '/rest/owner/setup', {'email': owner_email, 'firstName': 'n8n', 'lastName': 'Owner',
                                          'password': password})
        print(f'created the n8n owner {owner_email}')
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        n8n('POST', '/rest/login', {'emailOrLdapLoginId': owner_email, 'password': password})
    scopes = n8n('GET', '/rest/api-keys/scopes')['data']
    key = n8n('POST', '/rest/api-keys', {'label': f'n8nctl {time.strftime("%Y-%m-%d %H:%M")}', 'scopes': scopes,
                                        'expiresAt': None})['data']['rawApiKey']
    target.touch(mode=0o600)
    target.write_text(f'N8N_URL={url}\nN8N_API_KEY={key}\n' + (f'N8N_CA_FILE={cafile}\n' if cafile else ''))
    print(f'wrote {target}')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--instance', default='local', help='n8nctl name for this stack')
    ap.add_argument('--owner-email', default='admin@example.com')
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # so our lines don't mix with docker's

    make_secrets()
    if compose('up', '--detach', '--build', '--wait', '--wait-timeout', '600').returncode:
        sys.exit('the stack did not come up healthy: docker compose ps; docker compose logs <service>')
    connect_n8nctl(args.instance, args.owner_email)
    if compose('exec', '-T', 'backup', 'backup.sh', 'run').returncode:
        sys.exit('the first backup failed')
    env = read_env(ROOT / '.env')
    print(f"""
n8n:        {env['PUBLIC_URL']}  ({args.owner_email}, password in secrets/owner_password)
Grafana:    http://{env.get('GRAFANA_PUBLISH', '127.0.0.1:3000')}  (admin, password in secrets/grafana_admin_password)
Prometheus: http://{env.get('PROMETHEUS_PUBLISH', '127.0.0.1:9090')}
n8nctl:     python3 n8nctl.py diff {args.instance} <workflows dir>""")


if __name__ == '__main__':
    main()
