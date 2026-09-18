"""
Email alerting for insider_flags (app.insiders). All configuration comes from
the environment (backend/.env -- see .env.example); no credentials live in
this file or anywhere else in the repo.

If SMTP isn't configured yet, alerts are logged to stdout instead of sent, so
the detector is testable end-to-end before you've set up a mail relay.
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

from . import config as cfg


def _format(flag, condition_id: str, question: str) -> tuple[str, str]:
    subject = f"[Polymarket Insider Alert] {flag.insider_score:.0f}/100 -- {(question or condition_id)[:60]}"
    body = (
        f"Market: {question or '(unknown)'}\n"
        f"Condition ID: {condition_id}\n"
        f"Wallet: {flag.wallet}\n"
        f"Side: {flag.side}\n"
        f"Position: ${flag.position_usd:,.0f}\n"
        f"Insider score: {flag.insider_score:.0f}/100\n\n"
        f"Reasons:\n" + "\n".join(f"  - {r}" for r in flag.reasons) + "\n\n"
        f"This is an automated, unvalidated heuristic (see app/insiders.py) -- "
        f"verify before acting on it. Not financial advice.\n"
    )
    return subject, body


def send_insider_alert(flag, condition_id: str, question: str) -> bool:
    """Send (or log, if SMTP isn't configured) one insider alert. Returns True if actually emailed."""
    subject, body = _format(flag, condition_id, question)

    if not cfg.ALERTS.configured:
        print(f"[alerts] SMTP not configured (see backend/.env.example) -- logging only:\n"
              f"{subject}\n{body}")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.ALERTS.alert_from
    msg["To"] = cfg.ALERTS.alert_to
    msg.set_content(body)
    try:
        with smtplib.SMTP(cfg.ALERTS.smtp_host, cfg.ALERTS.smtp_port, timeout=10) as s:
            if cfg.ALERTS.smtp_use_tls:
                s.starttls()
            if cfg.ALERTS.smtp_user:
                s.login(cfg.ALERTS.smtp_user, cfg.ALERTS.smtp_password)
            s.send_message(msg)
        return True
    except Exception as e:  # noqa: BLE001 - a mail failure must never crash the poller
        print(f"[alerts] failed to send email: {e}")
        return False
