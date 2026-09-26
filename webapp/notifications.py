"""FR-C05 summary boundary: never expose financial values or source evidence."""
from __future__ import annotations

from html import escape
import logging
import os
from threading import Event, Thread

from src.models import Finding

EVENT_LABELS = {"audit_completed": "审计完成", "high_risk": "高风险条目提醒"}
RECIPIENT_ROLES = {"platform_admin", "org_admin", "accountant", "teacher"}


def email_delivery_enabled() -> bool:
    return (os.getenv("TAXPEARLS_NOTIFICATION_EMAIL_ENABLED", "") == "1"
            and bool(os.getenv("TAXPEARLS_RESEND_API_KEY", "").strip()))


def deliver_pending(store, limit: int = 20, sender=None) -> int:
    """Send claimed frozen payloads; unknown outcomes never auto-retry."""
    from src.mailer import MailError, send_email

    sender = sender or send_email
    count = 0
    for _ in range(limit):
        claim = store.claim_notification_delivery()
        if claim is None:
            break
        try:
            provider_id = sender(to=claim["recipient_email"], **claim["payload"],
                                 idempotency_key="audit-notification/" + claim["notification_id"], timeout=10)
            store.finish_notification_delivery(claim["notification_id"], claim["claim_token"], "accepted", provider_id)
        except MailError as exc:
            rejected = exc.status is not None and 400 <= exc.status < 500
            store.finish_notification_delivery(claim["notification_id"], claim["claim_token"],
                                               "failed" if rejected else "uncertain", error_code="provider_rejected" if rejected else "send_unknown")
        except Exception:
            # Neither exception text nor provider response is safe to log.
            store.finish_notification_delivery(claim["notification_id"], claim["claim_token"], "uncertain", error_code="send_unknown")
        count += 1
    return count


class NotificationWorker:
    def __init__(self, store_getter, interval: float = 2):
        self.store_getter = store_getter
        self.interval = interval
        self.stop_event = Event()
        self.thread = Thread(target=self.run, name="notification-mail", daemon=True)

    def start(self):
        if email_delivery_enabled():
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=12)

    def run(self):
        while not self.stop_event.is_set():
            try:
                if email_delivery_enabled():
                    target = self.store_getter()
                    target.recover_notification_claims()
                    # One send per iteration bounds shutdown to the request timeout.
                    deliver_pending(target, limit=1)
            except Exception:
                logging.getLogger(__name__).warning("Notification worker iteration failed; details withheld")
            self.stop_event.wait(self.interval)


def safe_summary(audit_id: str, findings: list[Finding], event: str, when: str) -> dict:
    if event not in EVENT_LABELS:
        raise ValueError("通知类型无效")
    hits = [item for item in findings if item.status == "hit"]
    high = [item for item in hits if item.rule.severity == "high"]
    shown = high if event == "high_risk" else hits
    return {
        "audit_id": audit_id, "event": event, "audited_at": when,
        "total_rules": len(findings), "hit_count": len(hits), "high_count": len(high),
        "risk_count": len(shown),
        "risks": [{"id": item.rule.id, "name": item.rule.name,
                   "severity": item.rule.severity} for item in shown[:20]],
    }


def email_content(summary: dict) -> tuple[str, str, str]:
    """Format only the explicitly whitelisted summary, not arbitrary JSON fields."""
    label = EVENT_LABELS[summary["event"]]
    lines = [label, f"审计编号：{summary['audit_id']}",
             f"风险条目 {int(summary['hit_count'])} 项，其中高风险 {int(summary['high_count'])} 项。"]
    lines.extend(f"{item['id']}：{item['name']}" for item in summary["risks"][:20])
    if int(summary["risk_count"]) > len(summary["risks"]):
        lines.append("更多条目请登录工作台查看。")
    lines.extend(["本邮件不附带原始财务数据；风险摘要不等于违法认定。",
                  "如需退订，请登录工作台关闭邮件通知。"])
    text = "\n".join(lines)
    html = "<div>" + "".join(f"<p>{escape(line)}</p>" for line in lines) + "</div>"
    return f"【税海拾珠】{label}", html, text
