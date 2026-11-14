"""Local consent and authenticated adapter regression; never calls Feishu."""
import base64
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from webapp import app as module, channel_bindings, feishu_binding as linking
from webapp.storage import Store


class FeishuBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {
            'TAXPEARLS_FEISHU_BINDING_ENABLED': '1', 'TAXPEARLS_FEISHU_APP_ID': 'cli_test',
            'TAXPEARLS_FEISHU_APP_TENANT_KEY': 'owner_tenant', 'TAXPEARLS_FEISHU_BRIDGE_KEY': 'ab' * 32,
            'TAXPEARLS_PUBLIC_BASE_URL': 'https://taxpearls.wxtech.site',
            'TAXPEARLS_FIELD_KEY': base64.b64encode(os.urandom(32)).decode(),
            'TAXPEARLS_MATERIAL_QUEUE_ENABLED': '0', 'TAXPEARLS_AI_ENABLED': '0',
            'TAXPEARLS_NOTIFICATION_EMAIL_ENABLED': '0',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = Store(Path(self.tmp.name) / 'binding.db', keepalive=True)
        self.addCleanup(self.store.close)
        replacement = patch.object(module, 'store', self.store)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.user = self.store.create_user('alice', 'Binding-safe-2026!', '示例会计', 'accountant', 'enterprise-a')
        self.other = self.store.create_user('bob', 'Binding-safe-2026!', '另一会计', 'accountant', 'enterprise-b')
        self.session = self.store.authenticate('alice', 'Binding-safe-2026!')[1]
        self.other_session = self.store.authenticate('bob', 'Binding-safe-2026!')[1]
        self.config = linking.settings()
        self.client = TestClient(module.app, raise_server_exceptions=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.cookies.set(module.COOKIE_NAME, self.session)
        self.proofs = {}

    def bridge(self, operation, *, nonce=None, stamp=None, key=None, **values):
        raw = json.dumps({'app_id': 'cli_test', 'operation': operation, **values}, separators=(',', ':')).encode()
        stamp, nonce = str(int(time.time()) if stamp is None else stamp), nonce or secrets.token_hex(16)
        message = '\n'.join(['POST', linking.INTERNAL_PATH, stamp, nonce, hashlib.sha256(raw).hexdigest()])
        signature = hmac.new(key or self.config.bridge_key, message.encode(), hashlib.sha256).hexdigest()
        return self.client.post(linking.INTERNAL_PATH, content=raw, headers={
            'Content-Type': 'application/json', 'x-tp-time': stamp, 'x-tp-nonce': nonce, 'x-tp-signature': signature})

    def issue(self, subject='ou_alice', chat='oc_alice', event_id='evt_1'):
        response = self.bridge('issue', subject=subject, chat=chat, event_id=event_id)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['url'].split('#ticket=')[1]

    def claim(self, ticket):
        if ticket not in self.proofs:
            response = self.client.post('/api/channels/feishu/status', json={'ticket': ticket})
            if response.status_code != 200:
                return response
            self.proofs[ticket] = response.json()['web_proof']
        return self.client.post('/api/channels/feishu/claim', json={'ticket': ticket, 'web_proof': self.proofs[ticket]})

    def prompt(self):
        response = self.bridge('pending')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['items'][0]

    def finish(self, item, operation='confirm', **overrides):
        values = {key: item[key] for key in ('id', 'subject', 'chat', 'confirmation')}
        return self.bridge(operation, **{**values, **overrides})

    def complete(self):
        ticket = self.issue()
        self.assertEqual(self.claim(ticket).status_code, 200)
        prompt = self.prompt()
        self.assertEqual(self.finish(prompt).status_code, 200)
        return ticket, prompt

    def test_two_consents_are_required_and_identity_resolves_only_at_end(self):
        ticket = self.issue()
        self.assertIsNone(channel_bindings.actor_for_subject(self.store, self.config.scope, 'ou_alice'))
        self.assertEqual(self.claim(ticket).json()['state'], 'web_confirmed')
        self.assertIsNone(channel_bindings.actor_for_subject(self.store, self.config.scope, 'ou_alice'))
        prompt = self.prompt()
        self.assertEqual(prompt['account_hint'], 'al***e')
        self.assertEqual(self.finish(prompt).json()['state'], 'completed')
        actor = channel_bindings.actor_for_subject(self.store, self.config.scope, 'ou_alice')
        self.assertEqual(actor['id'], self.user['id'])
        self.assertFalse(channel_bindings.list_bindings(self.store, self.session)[0]['notifications_enabled'])
        result = self.client.post('/api/channels/feishu/status', json={'ticket': ticket})
        self.assertEqual(result.json()['state'], 'completed')

    def test_entry_qr_uses_only_fixed_app_url_and_no_secret_or_identity(self):
        response = self.client.get('/api/channels/feishu/entry')
        self.assertEqual(response.json()['url'], 'https://applink.feishu.cn/client/bot/open?appId=cli_test')
        qr = self.client.get('/api/channels/feishu/qr.svg')
        self.assertEqual(qr.status_code, 200)
        self.assertIn('<svg', qr.text)
        self.assertNotIn('ou_alice', qr.text)
        self.assertNotIn(self.session, response.text)

    def test_no_public_route_accepts_provider_identity_or_grants_login(self):
        ticket = self.issue()
        self.client.cookies.clear()
        self.assertEqual(self.claim(ticket).status_code, 401)
        self.assertEqual(self.client.post('/api/channels/feishu/claim', json={'ticket': ticket, 'open_id': 'ou_attacker'}).status_code, 422)
        self.assertEqual(self.client.post(linking.INTERNAL_PATH, json={'operation': 'issue'}).status_code, 401)

    def test_bridge_rejects_wrong_key_timestamp_replay_and_cross_app(self):
        self.assertEqual(self.bridge('pending', key=b'x' * 32).status_code, 401)
        self.assertEqual(self.bridge('pending', stamp=1).status_code, 401)
        nonce = secrets.token_hex(16)
        self.assertEqual(self.bridge('pending', nonce=nonce).status_code, 200)
        self.assertEqual(self.bridge('pending', nonce=nonce).status_code, 409)
        self.assertEqual(self.bridge('pending', app_id='cli_other').status_code, 403)

    def test_claim_cannot_be_replaced_by_a_second_account_or_session(self):
        ticket = self.issue()
        self.claim(ticket)
        self.client.cookies.set(module.COOKIE_NAME, self.other_session)
        self.assertEqual(self.claim(ticket).status_code, 409)
        self.assertEqual(self.client.post('/api/channels/feishu/status', json={'ticket': ticket}).status_code, 404)
        self.assertEqual(self.prompt()['account_hint'], 'al***e')

    def test_account_switched_in_another_tab_requires_a_fresh_display_confirmation(self):
        ticket = self.issue()
        old = self.client.post('/api/channels/feishu/status', json={'ticket': ticket}).json()['web_proof']
        self.client.cookies.set(module.COOKIE_NAME, self.other_session)
        result = self.client.post('/api/channels/feishu/claim', json={'ticket': ticket, 'web_proof': old})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.bridge('pending').json()['items'], [])
        new = self.client.post('/api/channels/feishu/status', json={'ticket': ticket}).json()['web_proof']
        self.assertEqual(self.client.post('/api/channels/feishu/claim', json={'ticket': ticket, 'web_proof': new}).status_code, 200)
        self.assertEqual(self.prompt()['account_hint'], 'bo***b')

    def test_forwarded_confirm_card_wrong_chat_and_wrong_proof_fail(self):
        ticket = self.issue(); self.claim(ticket); item = self.prompt()
        for changes in ({'subject': 'ou_other'}, {'chat': 'oc_other'}, {'confirmation': 'ff' * 32}):
            self.assertEqual(self.finish(item, **changes).status_code, 404)
        self.assertEqual(channel_bindings.list_bindings(self.store, self.session), [])

    def test_expiry_and_tampered_ticket(self):
        ticket = self.issue()
        self.assertEqual(self.claim(ticket[:-1] + ('0' if ticket[-1] != '0' else '1')).status_code, 404)
        with patch.object(linking.time, 'time', return_value=time.time() + 601):
            self.assertEqual(self.claim(ticket).status_code, 404)

    def test_logout_and_role_change_invalidate_final_confirmation(self):
        ticket = self.issue(); self.claim(ticket); item = self.prompt()
        self.store.logout(self.session)
        self.assertEqual(self.finish(item).status_code, 409)
        self.assertIsNone(channel_bindings.actor_for_subject(self.store, self.config.scope, 'ou_alice'))

    def test_permission_change_between_confirmations_is_rejected(self):
        ticket = self.issue(); self.claim(ticket); item = self.prompt()
        with self.store.connect() as db:
            db.execute("UPDATE users SET org_id='changed' WHERE id=?", (self.user['id'],))
        self.assertEqual(self.finish(item).status_code, 409)

    def test_retries_are_idempotent_and_concurrent_confirmations_make_one_binding(self):
        ticket = self.issue()
        self.assertEqual(self.issue(), ticket)
        self.assertEqual(self.claim(ticket).status_code, 200)
        self.assertEqual(self.claim(ticket).status_code, 200)
        item = self.prompt()
        args = (self.store, self.config, item['id'], item['subject'], item['chat'], item['confirmation'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: linking.finish(*args), range(2)))
        self.assertEqual(results, [{'state': 'completed'}] * 2)
        self.assertEqual(len(channel_bindings.list_bindings(self.store, self.session)), 1)

    def test_cancellation_from_either_side_prevents_binding(self):
        ticket = self.issue(); self.claim(ticket); item = self.prompt()
        self.assertEqual(self.client.post('/api/channels/feishu/cancel', json={'ticket': ticket}).status_code, 200)
        self.assertEqual(self.finish(item).status_code, 409)
        second = self.issue('ou_second', 'oc_second', 'evt_2'); self.claim(second); item = self.prompt()
        self.assertEqual(self.finish(item, 'cancel').json()['state'], 'cancelled')
        self.assertEqual(self.finish(item).status_code, 409)

    def test_one_identity_and_one_account_cannot_be_silently_rebound(self):
        self.complete()
        self.assertEqual(self.bridge('issue', subject='ou_alice', chat='oc_alice', event_id='evt_2').json()['state'], 'bound')
        ticket = self.issue('ou_second', 'oc_second', 'evt_3')
        self.assertEqual(self.claim(ticket).status_code, 409)

    def test_unlink_revokes_old_confirmation_without_resurrection(self):
        ticket, item = self.complete()
        binding = self.client.get('/api/channels/feishu/bindings').json()[0]
        response = self.client.delete('/api/channels/feishu/bindings/' + binding['id'], headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.finish(item).status_code, 409)
        self.assertIsNone(channel_bindings.actor_for_subject(self.store, self.config.scope, 'ou_alice'))
        self.assertEqual(self.client.post('/api/channels/feishu/status', json={'ticket': ticket}).json()['state'], 'cancelled')

    def test_replacement_request_invalidates_previous_accounts_confirm_card(self):
        first = self.issue(); self.claim(first); old = self.prompt()
        second = self.issue('ou_second', 'oc_second', 'evt_second'); self.claim(second)
        self.assertEqual(self.finish(old).status_code, 409)
        current = self.prompt()
        self.assertEqual(current['subject'], 'ou_second')

    def test_encrypted_identity_and_no_tickets_sessions_or_raw_identity_in_audit_log(self):
        ticket = self.issue(); self.claim(ticket)
        with closing(sqlite3.connect(self.store.path)) as db:
            identity = db.execute('SELECT identity_json FROM feishu_binding_requests').fetchone()[0]
            dump = '\n'.join(db.iterdump())
        self.assertTrue(identity.startswith('tp-field-v1:'))
        for secret in (ticket, self.session, 'ou_alice', 'oc_alice'):
            self.assertNotIn(secret, dump)

    def test_csrf_and_invalid_public_origin(self):
        ticket = self.issue()
        self.assertEqual(self.client.post('/api/channels/feishu/claim', json={'ticket': ticket, 'web_proof': 'a' * 64}, headers={'Origin': 'https://evil.test'}).status_code, 403)
        with patch.dict(os.environ, {'TAXPEARLS_PUBLIC_BASE_URL': 'https://evil.test/path'}):
            self.assertEqual(self.client.get('/api/channels/feishu/entry').status_code, 503)

    def test_feature_disabled_and_static_page_security(self):
        with patch.dict(os.environ, {'TAXPEARLS_FEISHU_BINDING_ENABLED': '0'}):
            self.assertEqual(self.client.get('/api/channels/feishu/entry').status_code, 503)
        response = self.client.get('/connect/feishu')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['referrer-policy'], 'no-referrer')
        self.assertIn("frame-ancestors 'none'", response.headers['content-security-policy'])
        self.assertEqual(self.client.get('/feishu-connect.js').status_code, 200)


if __name__ == '__main__':
    unittest.main()
