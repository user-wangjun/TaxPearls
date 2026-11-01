"""Explicit browser-bound proofs for storage tests; no alternate production auth."""
from datetime import UTC, datetime, timedelta
import secrets
from webapp import email_auth

BROWSER = "B" * 43


def register_code(store, email):
    delivery = email_auth.issue(store, email, 'register', BROWSER)
    if delivery is None:
        raise ValueError('该邮箱已注册。')
    return delivery['code']


def reset_proof(store, email):
    delivery = email_auth.issue(store, email, 'reset', BROWSER)
    if delivery is None:
        return None
    return email_auth.redeem_magic(store, delivery['token'], BROWSER)['proof']


def reset_password(store, proof, password):
    return email_auth.reset_password(store, proof, password, BROWSER)


def legacy_token(store, email, purpose):
    """Seed an old persisted row solely to verify its rejection after upgrade."""
    token = secrets.token_urlsafe(32)
    with store.connect() as db:
        db.execute('INSERT INTO email_tokens(token_hash,email,purpose,expires_at,created_at) VALUES (?,?,?,?,?)',
                   (email_auth.digest(token),email,purpose,(datetime.now(UTC)+timedelta(minutes=10)).isoformat(),datetime.now(UTC).isoformat()))
    return token


def content_session(store, client):
    """Give content-maintenance tests an actual platform session."""
    username, password = "content-maintenance-test", "ContentMaintenance-2026!"
    result = store.authenticate(username, password)
    if result is None:
        store.create_user(username, password, "内容维护测试", "platform_admin", "platform")
        result = store.authenticate(username, password)
    client.cookies.set("taxpearls_session", result[1])
