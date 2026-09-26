"""发送一封测试邮件，验证 Resend 发信链路与域名信誉是否可用。

用法：
    python scripts/send_test_email.py --to someone@qq.com

- 读取项目根目录 .env 中的 TAXPEARLS_RESEND_API_KEY
- 发件人：税海拾珠 <noreply@taxpearls.wxtech.site>
- 退出码 0 = Resend 已受理；收到后请检查邮件落在收件箱还是垃圾箱
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import settings  # noqa: F401  — 导入即加载 .env
from src.mailer import MailError, send_email


def main() -> int:
    parser = argparse.ArgumentParser(description="发送 Resend 发信链路测试邮件")
    parser.add_argument("--to", required=True, help="收件人邮箱地址")
    args = parser.parse_args()

    subject = "【税海拾珠】发信链路测试"
    html = (
        '<div style="font-family:sans-serif;max-width:560px;margin:0 auto;'
        'padding:24px;border:1px solid #e0e0e0;border-radius:8px">'
        '<h2 style="color:#1a3a5c">发信链路测试</h2>'
        "<p>如果你收到这封邮件，说明 Resend 发信链路与域名 SPF / DKIM 配置正常。</p>"
        "<p><b>请顺手确认一下它落在「收件箱」而不是「垃圾箱」</b>——"
        "落点决定了真实用户能否收到注册验证码。</p>"
        '<p style="color:#888;font-size:13px">'
        "本邮件为系统自动发送的测试邮件，请勿回复。</p>"
        "</div>"
    )

    try:
        mail_id = send_email(to=args.to, subject=subject, html=html)
    except MailError as exc:
        print(f"发送失败：{exc}")
        return 1
    print(f"发送成功：Resend 已受理，邮件 ID = {mail_id}")
    print("请检查收件箱落点（收件箱 / 垃圾箱），并告知结果。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
