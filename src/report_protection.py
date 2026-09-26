"""Visible traceability marks; these are not signatures or copy prevention."""
from __future__ import annotations

from hashlib import sha256
from html import escape
import json
import re

IDENTIFIER = re.compile(r"^TPV-[0-9a-f]{40}$")
STYLES = """
.tp-watermark{position:fixed;inset:0;z-index:9999;pointer-events:none;opacity:.075;
 display:flex;flex-direction:column;justify-content:space-around;overflow:hidden;
 font:18pt/1.5 'Microsoft YaHei','PingFang SC',sans-serif;color:#315477;text-align:center}
.tp-watermark span{display:block;transform:rotate(-25deg);overflow-wrap:anywhere;padding:0 12mm}
.tp-protection{font:7pt/1.4 'Microsoft YaHei','PingFang SC',sans-serif;color:#63758b;
 text-align:left;overflow-wrap:anywhere;pointer-events:none}
@media print{.tp-protection{display:none}}
@media screen{.tp-protection{margin:8px 0;padding:8px;background:#f3f7fc}}
"""


def verification_id_from_html(html: str) -> str:
    match=re.search(r'<aside class="tp-protection" data-verification-id="(TP[VL]-[0-9a-f]{40})">',html)
    return match.group(1) if match else ""


def protect_html(html: str, customer_name: str, report_date: str, report_no: str,
                 context: str = "", registered: bool = False) -> tuple[str, dict]:
    """Repeat name/date and identifier on every printed page without reflow.

    Content-derived IDs keep archive deduplication stable. Context binds a mark
    to its full frozen manifest, not just the non-unique human report number.
    Only a matching server archive/file hash proves original-byte equality.
    """
    if not re.search(r"<body\b[^>]*>", html, flags=re.I):
        raise ValueError("报告 HTML 缺少正文，无法添加水印。")
    name = " ".join(customer_name.split())
    record = {"schema":1, "customer_name":name, "report_date":report_date, "report_no":report_no,
              "method":"archive-original" if registered else "local-content",
              "base_html_sha256":sha256(html.encode()).hexdigest()}
    encoded = json.dumps(record,ensure_ascii=False,sort_keys=True,separators=(",",":"))
    fingerprint = sha256((encoded + "\n" + context + "\n" + STYLES).encode()).hexdigest()[:40]
    record["id"] = ("TPV-" if registered else "TPL-") + fingerprint
    label = escape(f"{name} · 报告日期 {report_date} · 仅限授权用途")
    message = ("登录工作台使用此标识核对原件文件；标识本身不能证明文件未修改。"
               if registered else "本地导出未登记服务器原件；此标识不是服务器来源认证。")
    markup = (f"<style>{STYLES}</style><div class=\"tp-watermark\" aria-hidden=\"true\">"
              + "".join(f"<span>{label}</span>" for _ in range(4)) + "</div>"
              + f'<aside class="tp-protection" data-verification-id="{record["id"]}">'
              + f'追溯标识 {record["id"]} · {escape(report_no)}<br>{message}'
              + "水印用于追溯，不能阻止复制、转发或截图；不是第三方数字签名。</aside>")
    return re.sub(r"(<body\b[^>]*>)",lambda match:match.group(1)+markup,html,count=1,flags=re.I), record
