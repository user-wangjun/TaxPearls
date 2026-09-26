"""事务邮件发送：通过 Resend HTTP API 发信。

密钥从环境变量 ``TAXPEARLS_RESEND_API_KEY`` 读取（由项目根目录 ``.env`` 提供），
明文只存在于 ``.env`` 与进程内存，**不进日志、不进数据库、不进异常信息**。

域名 ``taxpearls.wxtech.site`` 须在 Resend 后台完成 SPF/DKIM 验证，
否则发出的邮件会被收件方判为垃圾邮件（详见 第 7.3 节）。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

API_ENDPOINT = "https://api.resend.com/emails"
DEFAULT_FROM_ADDRESS = "noreply@taxpearls.wxtech.site"
DEFAULT_FROM_NAME = "税海拾珠"


class MailError(RuntimeError):
    """发信失败（未配置密钥 / 网络 / 上游拒绝）。"""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_request(request, timeout):
    # Do not forward a bearer key or notification payload through redirects.
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


def _api_key() -> str:
    key = os.getenv("TAXPEARLS_RESEND_API_KEY", "").strip()
    if not key:
        raise MailError("未配置发信密钥：请在 .env 中填写 TAXPEARLS_RESEND_API_KEY。")
    return key


def _from_header() -> str:
    name = os.getenv("TAXPEARLS_EMAIL_FROM_NAME", "").strip() or DEFAULT_FROM_NAME
    address = os.getenv("TAXPEARLS_EMAIL_FROM_ADDRESS", "").strip() or DEFAULT_FROM_ADDRESS
    return f"{name} <{address}>"


def _validate_recipient(to: str) -> str:
    address = to.strip()
    if not address or " " in address or "@" not in address:
        raise MailError("收件人邮箱格式不正确。")
    return address


def send_email(*, to: str, subject: str, html: str, text: str | None = None,
               reply_to: str | None = None, timeout: int = 30,
               idempotency_key: str | None = None, from_header: str | None = None) -> str:
    """发送一封邮件，返回 Resend 受理后的邮件 ID。

    参数：
        to:       收件人邮箱（单个地址）
        subject:  邮件主题（纯文本）
        html:     邮件正文（HTML）
        text:     可选的纯文本正文；不填由收件端自行渲染
        reply_to: 可选的 Reply-To 地址
        timeout:  请求超时秒数

    失败抛出 :class:`MailError`，消息中含上游状态码与响应摘要，但**不含密钥**。
    """
    recipient = _validate_recipient(to)
    if idempotency_key is not None and not re.fullmatch(r"[A-Za-z0-9/_-]{1,256}", idempotency_key):
        raise MailError("邮件幂等键格式不正确。")
    sender = from_header if from_header is not None else _from_header()
    if not sender or len(sender) > 200 or any(ord(char) < 32 for char in sender):
        raise MailError("发件人配置无效。")
    payload: dict[str, object] = {
        "from": sender,
        "to": [recipient],
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text
    if reply_to:
        payload["reply_to"] = reply_to.strip()

    request = urllib.request.Request(
        API_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
            # Resend 的 API 在 Cloudflare 后面：默认的 Python-urllib UA 会被其
            # 机器人规则拦截（HTTP 403, error 1010），必须带一个正常 UA。
            "User-Agent": "taxpearls-mailer/1.0",
        },
        method="POST",
    )
    if idempotency_key:
        request.add_header("Idempotency-Key", idempotency_key)
    try:
        with _open_request(request, timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise MailError(f"Resend 拒绝了请求（HTTP {exc.code}）：{detail}",
                        status=exc.code) from None
    except urllib.error.URLError as exc:
        raise MailError(f"无法连接 Resend：{exc.reason}") from None
    except TimeoutError:
        raise MailError("连接 Resend 超时。") from None
    except json.JSONDecodeError:
        raise MailError("Resend 返回了非 JSON 响应。") from None

    mail_id = body.get("id", "")
    if not mail_id:
        raise MailError("Resend 返回了异常响应：缺少邮件 ID。")
    return str(mail_id)


def _page(title: str, body_html: str, footnote: str) -> str:
    return (
        '<div style="font-family:sans-serif;max-width:560px;margin:0 auto;'
        'padding:24px;border:1px solid #e0e0e0;border-radius:8px">'
        f'<h2 style="color:#1a3a5c;margin-top:0">{title}</h2>'
        f"{body_html}"
        f'<p style="color:#888;font-size:13px">{footnote}</p>'
        "</div>"
    )


def send_password_reset_email(*, to: str, reset_url: str, expires_minutes: int = 10) -> str:
    """发送密码重置邮件。reset_url 为一次性重置链接（含长随机令牌）。"""
    subject = "【税海拾珠】重置登录密码"
    body_html = (
        "<p>有人申请了重置本邮箱对应的登录密码。点击下面的链接设置新密码：</p>"
        f'<p><a href="{reset_url}">重置登录密码（{expires_minutes} 分钟内有效）</a></p>'
        f'<p style="color:#666">若按钮无法点击，可将以下地址粘贴到浏览器打开：<br>'
        f"<span style='word-break:break-all'>{reset_url}</span></p>"
        "<p><b>该链接 10 分钟内有效且只能使用一次。</b>重置成功后，此账号的所有登录状态将被注销。</p>"
    )
    footnote = "若非本人操作，请忽略本邮件——您的账号不会被修改。请勿回复本邮件。"
    return send_email(to=to, subject=subject, html=_page("重置登录密码", body_html, footnote))


def send_registration_code_email(*, to: str, code: str, signup_url: str,
                                 expires_minutes: int = 10) -> str:
    """发送注册验证邮件：6 位验证码 + 一键预填链接。"""
    subject = f"【税海拾珠】注册验证码 {code}"
    body_html = (
        "<p>您的注册验证码为：</p>"
        f'<p style="font-size:30px;font-weight:700;letter-spacing:.35em;color:#1a3a5c;'
        f'background:#f4f7fb;border:1px solid #dbe2ec;border-radius:8px;'
        f'padding:14px 10px;text-align:center">{code}</p>'
        "<p>也可以点击下面的链接，验证码会自动填入：</p>"
        f'<p><a href="{signup_url}">完成注册（{expires_minutes} 分钟内有效）</a></p>'
        "<p><b>验证码 10 分钟内有效；连续输错 5 次将作废，需重新获取。</b></p>"
    )
    footnote = "若非本人操作，请忽略本邮件。请勿回复本邮件。"
    return send_email(to=to, subject=subject, html=_page("注册验证码", body_html, footnote))
