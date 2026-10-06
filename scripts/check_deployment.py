"""One-shot local container checks, synthetic data only; never production acceptance.

python scripts/check_deployment.py --image RELEASE_TAG --rollback-image COMPATIBLE_TAG
Creates uniquely named temporary Compose projects and removes only their volumes.
Never reads .env or operates an existing container or volume.
"""
import argparse
import base64
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def request(ctx, path, data=None, *, content_type='application/json', token=None, expected=200, timeout=90):
    headers = {'Content-Type': content_type}
    if token or ctx.get('token'): headers['Cookie'] = 'taxpearls_session=' + (token or ctx['token'])
    req = Request(ctx['url'] + path, data=data, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as response: code, raw = response.status, response.read()
    except HTTPError as error:
        with error: code, raw = error.code, error.read()
    if code != expected: raise RuntimeError(f'{path}: expected {expected}, got {code}')
    return raw


def upload_and_confirm(ctx, payload, content_type, *, timeout=60, poll=.5):
    """Confirm only the completed job from this upload, using its saved analysis."""
    batch = json.loads(request(ctx, '/api/enterprise/materials', payload,
                               content_type=content_type, expected=202))
    jobs = batch.get('jobs', [])
    if len(jobs) != 1 or not jobs[0].get('id') or jobs[0].get('batch_id') != batch['id']:
        raise RuntimeError('Upload did not return one identifiable material job')
    job_id = jobs[0]['id']
    path = '/api/enterprise/materials/' + batch['id']
    deadline = time.monotonic() + timeout
    state = 'queued'

    def read(suffix):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(f'Material job timed out (last state: {state})')
        try:
            return json.loads(request(ctx, path + suffix, timeout=min(5, remaining)))
        except (TimeoutError, URLError):
            raise RuntimeError('Material job status/read request failed or timed out') from None

    while True:
        status = read('/status')
        job = next((j for j in status.get('jobs', []) if j['id'] == job_id), None)
        if status.get('id') != batch['id'] or not job or job.get('batch_id') != batch['id']:
            raise RuntimeError('Uploaded material job missing from its batch status')
        state = job['state']
        if state == 'done':
            batch = read('')
            if (batch.get('id') != status['id'] or type(job.get('result_revision')) is not int
                    or batch.get('revision') != job['result_revision']
                    or batch.get('analysis_revision') != job['result_revision']):
                raise RuntimeError('Completed material job does not match the current analysis revision')
            if batch.get('analysis', {}).get('can_confirm') is not True:
                raise RuntimeError('Completed material analysis is not confirmable')
            break
        if state not in {'queued', 'running'}:
            terminal = state if state in {'failed', 'superseded'} else 'unknown'
            raise RuntimeError(f'Material job ended without a usable analysis ({terminal})')
        time.sleep(min(poll, max(0, deadline - time.monotonic())))
    result = json.loads(request(ctx, path + '/confirm',
                                json.dumps({'expected_revision': batch['revision']}).encode()))
    return batch, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, help='Existing local taxpearls image tag; no pull/build')
    parser.add_argument('--rollback-image', required=True, help='Different, storage-compatible local tag')
    args = parser.parse_args()
    for tag in (args.image, args.rollback_image):
        if not tag or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-' for c in tag):
            parser.error('Use an explicit local image tag, not an arbitrary image reference')
    secrets = []
    child_env = {k:v for k,v in os.environ.items() if not k.startswith('TAXPEARLS_')}

    def command(*parts, input=None, check=True):
        result = subprocess.run(parts, input=input, text=True, encoding='utf-8', capture_output=True,
                                cwd=ROOT, env=child_env, timeout=180)
        if check and result.returncode:
            message = result.stderr or result.stdout
            for secret in secrets: message = message.replace(secret, '[secret]')
            raise RuntimeError('Local container check failed: ' + message[:2000])
        return result

    image_ids = [command('docker', 'image', 'inspect', 'taxpearls:' + tag, '--format', '{{.Id}}').stdout.strip()
                 for tag in (args.image, args.rollback_image)]
    if image_ids[0] == image_ids[1]:
        raise RuntimeError('Version rollback requires two different image IDs')
    prefix = 'taxpearls-check-' + uuid.uuid4().hex[:10]
    print('Local synthetic check: isolated projects, reports, recovery and compatible image rollback.', flush=True)
    with tempfile.TemporaryDirectory(prefix=prefix + '-') as directory:
        contexts = []
        for mode in ('training', 'production'):
            instance = prefix + '-' + mode
            values = {'TAXPEARLS_ENVIRONMENT': mode, 'TAXPEARLS_INSTANCE_ID': instance,
                      'TAXPEARLS_IMAGE_TAG': args.image, 'TAXPEARLS_BIND_PORT': '0',
                      'TAXPEARLS_COOKIE_SECURE': '1' if mode == 'production' else '0',
                      'TAXPEARLS_PUBLIC_BASE_URL': 'https://synthetic.example.test',
                      'TAXPEARLS_TRUSTED_PROXY_IPS': '127.0.0.1',
                      'TAXPEARLS_AI_ENABLED': '0', 'TAXPEARLS_NOTIFICATION_EMAIL_ENABLED': '0',
                      'TAXPEARLS_MATERIAL_QUEUE_ENABLED': '1', 'TAXPEARLS_MATERIAL_WORKERS': '4',
                      'TAXPEARLS_AI_API_KEY': '', 'TAXPEARLS_RESEND_API_KEY': ''}
            for name in ('TAXPEARLS_FIELD_KEY', 'TAXPEARLS_MATERIAL_KEY', 'TAXPEARLS_BACKUP_KEY'):
                values[name] = base64.b64encode(os.urandom(32)).decode(); secrets.append(values[name])
            envfile = Path(directory) / (mode + '.env')
            values['TAXPEARLS_ENV_FILE'] = envfile.as_posix()
            envfile.write_text(''.join(k + '=' + v + '\n' for k,v in values.items()), encoding='utf-8')
            contexts.append({'mode': mode, 'instance': instance, 'file': envfile, 'values': values})

        def compose(ctx, *parts, **kwargs):
            if not ctx['instance'].startswith(prefix + '-'):
                raise RuntimeError('Unexpected cleanup/project scope')
            return command('docker', 'compose', '--env-file', str(ctx['file']), '-f', str(ROOT / 'compose.yaml'),
                           '-p', ctx['instance'], *parts, **kwargs)

        def start(ctx):
            compose(ctx, 'up', '-d', '--no-build', '--pull', 'never', '--force-recreate')
            bound = compose(ctx, 'port', 'taxpearls', '8000').stdout.strip()
            if not bound.startswith('127.0.0.1:'): raise RuntimeError('Non-loopback container publishing')
            ctx['url'] = 'http://' + bound
            deadline = time.monotonic() + 60
            while True:
                try:
                    with urlopen(ctx['url'] + '/healthz', timeout=2) as response:
                        if response.status == 200: break
                except (URLError, TimeoutError, ConnectionError):
                    if time.monotonic() >= deadline: raise RuntimeError('Local container health timeout') from None
                    time.sleep(.5)

        # Before allocating anything, refuse a namespace that already contains
        # containers/volumes. Cleanup must never adopt a user's existing project.
        for ctx in contexts:
            label = 'label=com.docker.compose.project=' + ctx['instance']
            if (command('docker', 'ps', '-aq', '--filter', label).stdout.strip()
                    or command('docker', 'volume', 'ls', '-q', '--filter', label).stdout.strip()):
                raise RuntimeError('Synthetic project namespace already exists; nothing removed')
        try:
            for ctx in contexts:
                compose(ctx, 'config', '--quiet'); start(ctx)
                seeded = compose(ctx, 'exec', '-T', 'taxpearls', 'python', '-c',
                                 "from webapp.storage import Store; s=Store(); "
                                 "s.create_user('container-owner','Synthetic-container-2026!','仿真管理员','org_admin','synthetic'); "
                                 "print(s.authenticate('container-owner','Synthetic-container-2026!')[1])").stdout.strip()
                ctx['token'] = seeded; secrets.append(seeded)
            training, production = contexts
            container_ids = [compose(ctx, 'ps', '-q', 'taxpearls').stdout.strip() for ctx in contexts]
            details = json.loads(command('docker', 'inspect', *container_ids).stdout)
            volumes = [{m['Name'] for m in d['Mounts'] if m['Type'] == 'volume'} for d in details]
            networks = [set(d['NetworkSettings']['Networks']) for d in details]
            if volumes[0] & volumes[1] or networks[0] & networks[1]: raise RuntimeError('Shared volume/network')
            request(training, '/api/me', token=production['token'], expected=401)
            request(production, '/api/me', token=training['token'], expected=401)
            # Only synthetic source bytes, delivered through the actual enterprise API.
            wb = Workbook(); ws = wb.active; ws.title = '企业信息'
            for row in [('项目','内容'),('企业名称','纯合成测试 Docker 企业'),('纳税人识别号','TEST-DOCKER-001'),
                        ('所属行业','服务业'),('所属期','2026-01')]: ws.append(row)
            from src import config
            ws = wb.create_sheet('科目余额表'); ws.append(config.COL_ACCOUNTS)
            ws.append(['6001','主营业务收入',0,0,100000,0])
            ws.append(['6051','其他业务收入',0,0,0,0])
            stream = BytesIO(); wb.save(stream); wb.close(); raw = stream.getvalue()
            boundary = uuid.uuid4().hex
            payload = (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="synthetic.xlsx"\r\n'
                       'Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n').encode()
            payload += raw + f'\r\n--{boundary}--\r\n'.encode()
            batch, result = upload_and_confirm(production, payload, 'multipart/form-data; boundary=' + boundary)
            aid = result['audit_id']
            original_path = '/api/enterprise/materials/' + batch['id'] + '/originals/' + batch['files'][0]['id']
            pdf_path = '/api/report/' + aid + '?version=1'
            html_path = '/api/report/' + aid + '/html?version=1'
            original = request(production, original_path)
            html, pdf = request(production, html_path), request(production, pdf_path)
            if original != raw or not pdf.startswith(b'%PDF'): raise RuntimeError('Original/PDF bytes invalid')
            request(training, '/api/audits/' + aid, expected=404)
            compose(production, 'exec', '-T', 'taxpearls', 'python', 'scripts/ops_db.py', 'backup-encrypted',
                    '--database', '/data/taxpearls.db', '--output', '/data/baseline.tpbackup', '--retention-days', '7')
            # Deliberately supply the correct source secrets in an isolated
            # throwaway process: deployment identity must still refuse the copy.
            capsule = compose(production, 'exec', '-T', 'taxpearls', 'python', '-c',
                              "import base64,pathlib; print(base64.b64encode(pathlib.Path('/data/baseline.tpbackup').read_bytes()).decode())").stdout
            compose(training, 'exec', '-T', 'taxpearls', 'python', '-c',
                    "import base64,pathlib,sys; pathlib.Path('/data/foreign.tpbackup').write_bytes(base64.b64decode(sys.stdin.read()))",
                    input=capsule)
            foreign = command('docker', 'run', '--rm', '--env-file', str(production['file']),
                              '--env', 'TAXPEARLS_ENVIRONMENT=training', '--env', 'TAXPEARLS_INSTANCE_ID=' + training['instance'],
                              '--env', 'TAXPEARLS_DB=/data/taxpearls.db',
                              '--mount', 'type=volume,source=' + next(iter(volumes[0])) + ',target=/data',
                              'taxpearls:' + args.image, 'python', 'scripts/ops_db.py', 'restore-encrypted',
                              '/data/foreign.tpbackup', '--database', '/data/taxpearls.db', '--safety-retention-days', '7', '--yes', check=False)
            if foreign.returncode == 0 or '另一部署环境或实例' not in foreign.stderr:
                raise RuntimeError('Container cross-environment restore did not fail at the binding guard')
            print('Container isolation, session boundary, original and real PDF passed; checking recovery.', flush=True)
            compose(production, 'stop')
            stopped = json.loads(command('docker', 'inspect', container_ids[1]).stdout)[0]['State']
            if stopped['Running']: raise RuntimeError('Synthetic application was not stopped')
            compose(production, 'run', '--rm', '--no-deps', '-T', 'taxpearls', 'python', 'scripts/ops_db.py',
                    'checkpoint', '--database', '/data/taxpearls.db', '--yes')
            compose(production, 'run', '--rm', '--no-deps', '-T', 'taxpearls', 'python', 'scripts/ops_db.py',
                    'restore-encrypted', '/data/baseline.tpbackup', '--database', '/data/taxpearls.db',
                    '--safety-retention-days', '7', '--yes')
            start(production)
            for path, expected in ((original_path, original), (html_path, html), (pdf_path, pdf)):
                if request(production, path) != expected: raise RuntimeError('Restored business bytes changed')
            # Different compatible code-image ID, unchanged guarded volume/config/keys.
            compose(production, 'stop')
            production['file'].write_text(''.join(k + '=' + (args.rollback_image if k == 'TAXPEARLS_IMAGE_TAG' else v) + '\n'
                                                for k,v in production['values'].items()), encoding='utf-8')
            start(production)
            actual = json.loads(command('docker', 'inspect', compose(production, 'ps', '-q', 'taxpearls').stdout.strip()).stdout)[0]['Image']
            if actual != image_ids[1]: raise RuntimeError('Rollback image was not activated')
            guard = compose(production, 'exec', '-T', '-e', 'TAXPEARLS_ENVIRONMENT=training',
                            '-e', 'TAXPEARLS_INSTANCE_ID=' + training['instance'],
                            'taxpearls', 'python', '-c', 'from webapp.storage import Store; Store()', check=False)
            if guard.returncode == 0 or '另一部署环境或实例' not in guard.stderr:
                raise RuntimeError('Rollback image did not preserve the deployment identity guard')
            for path, expected in ((original_path, original), (html_path, html), (pdf_path, pdf)):
                if request(production, path) != expected: raise RuntimeError('Rollback changed business bytes')
            print(json.dumps({'local_only': True, 'physical_production_acceptance': False,
                              'image_ids': image_ids, 'isolated_volumes': True, 'isolated_networks': True,
                              'session_isolation': True, 'restored_original_sha256': hashlib.sha256(original).hexdigest(),
                              'cross_environment_restore_refused': True,
                              'pdf_sha256': hashlib.sha256(pdf).hexdigest(), 'html_sha256': hashlib.sha256(html).hexdigest(),
                              'restore_and_compatible_image_rollback': True,
                              'rollback_identity_guard_preserved': True}, ensure_ascii=False))
        finally:
            # Exact newly created projects only. Never use Docker global prune.
            for ctx in contexts: compose(ctx, 'down', '--volumes', '--remove-orphans')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
