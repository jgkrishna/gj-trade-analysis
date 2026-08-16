"""
Alert delivery: email (SMTP) and generic webhook.
=====================================================

Deliberately takes config as explicit parameters rather than reading
environment variables itself -- dashboard.py resolves config from
st.secrets/env (consistent with how the password hash is resolved) and
passes it in; check_alerts.py (the standalone, scheduler-run checker)
resolves config from plain env vars and passes it in the same way. Neither
caller needs a real email/webhook service to use the rest of the app --
if nothing is configured, notify_new_reversals() just returns an empty
list of channels and callers treat that as "no-op."

SMS is intentionally NOT implemented here: it needs a paid third-party
account (e.g. Twilio) with a verified phone number, which nobody can set
up on your behalf. Email + webhook cover the same need without that.
A webhook is enough to reach Slack, Discord, or a service like ntfy.sh.
"""

from __future__ import annotations

import json
import smtplib
import urllib.error
import urllib.request
from email.mime.text import MIMEText


def send_email_alert(*, host: str, port: int, user: str, password: str,
                      to_addrs: list[str], subject: str, body: str) -> bool:
    if not (host and to_addrs):
        return False
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(to_addrs)
    try:
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.starttls()
            if user and password:
                server.login(user, password)
            server.sendmail(user, to_addrs, msg.as_string())
        return True
    except Exception as e:
        print(f"[alerts] email send failed: {e}")
        return False


def send_webhook_alert(*, url: str, payload: dict) -> bool:
    if not url:
        return False
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except urllib.error.URLError as e:
        print(f"[alerts] webhook send failed: {e}")
        return False


def _format_message(ticker: str, new_rows: list[dict]) -> tuple[str, str]:
    subject = f"[{ticker}] {len(new_rows)} new reversal(s) detected"
    lines = [f"{ticker}: {len(new_rows)} new confirmed reversal(s):", ""]
    for row in new_rows:
        lines.append(
            f"  {row['confirmed_on']}: {row['prior_direction']} -> {row['new_direction']} "
            f"(confidence {row['confidence']}/4, methods: {row['methods']})"
        )
    return subject, "\n".join(lines)


def notify_new_reversals(ticker: str, new_rows: list[dict], *,
                          email_config: dict | None = None,
                          webhook_url: str | None = None) -> list[str]:
    """Sends an alert for newly-detected reversals over whichever channels
    are configured. Returns the list of channel names that succeeded."""
    if not new_rows:
        return []

    subject, body = _format_message(ticker, new_rows)
    sent = []

    if email_config:
        ok = send_email_alert(
            host=email_config["host"], port=email_config.get("port", 587),
            user=email_config.get("user", ""), password=email_config.get("password", ""),
            to_addrs=email_config.get("to_addrs", []), subject=subject, body=body,
        )
        if ok:
            sent.append("email")

    if webhook_url:
        ok = send_webhook_alert(url=webhook_url, payload={
            "ticker": ticker, "subject": subject, "body": body, "reversals": new_rows,
        })
        if ok:
            sent.append("webhook")

    return sent
