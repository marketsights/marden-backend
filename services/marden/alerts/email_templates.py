"""HTML and text emails for alerts: instant (red), daily digest and weekly summary. Code templates only.

Every email lists each alert's headline (linked to its screen), rule, date, the body lines the engine wrote
from the computed numbers, and its sources (links to the filings and documents). The footer says why the
reader gets it and carries the manage link (Account > Alerts) and two signed one-click unsubscribe links
(this kind of email, or every alert email); the List-Unsubscribe headers carry the first (RFC 8058).
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from marden.alerts import links
from marden.alerts import text as t
from marden.alerts.settings import EmailConfig

KIND_SCOPE = {"instant": "instant", "daily_digest": "daily", "weekly_digest": "weekly"}
SCOPE_LABEL = {"instant": "instant red alerts", "daily": "the daily digest", "weekly": "the weekly summary",
               "all": "all alert email"}
SEVERITY_LABEL = {"red": "Red", "amber": "Amber", "green": "Info"}
SEVERITY_COLOR = {"red": "#b42318", "amber": "#b54708", "green": "#475467"}
MAX_SOURCES = 5


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    html: str
    text: str
    headers: dict[str, str]


def subject_for(kind: str, alerts: list[dict[str, Any]], day: datetime) -> str:
    n = len(alerts)
    noun = "alert" if n == 1 else "alerts"
    if kind == "instant":
        return f"[Marden] {alerts[0]['headline']}" if n == 1 else f"[Marden] {n} red alerts on your watchlist"
    if kind == "daily_digest":
        return f"Marden daily digest: {n} {noun} on your watchlist, {t.day(day.date())}"
    return f"Marden weekly summary: {n} {noun} on your watchlist, week to {t.day(day.date())}"


def _intro(kind: str, n: int) -> str:
    if kind == "instant":
        return "A name on your watchlist turned red." if n == 1 else f"{n} red alerts on names on your watchlist."
    if kind == "daily_digest":
        return f"{n} alert{'s' if n != 1 else ''} on your watchlist since yesterday's digest."
    return f"{n} alert{'s' if n != 1 else ''} on your watchlist this week."


def _sources(alert: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in alert.get("sources") or [] if s.get("url") or s.get("name")][:MAX_SOURCES]


def _source_text(s: dict[str, Any]) -> str:
    parts = [s.get("name") or "source"]
    if s.get("tag"):
        parts.append(f"({s['tag']})")
    if s.get("asOf"):
        parts.append(t.day(s["asOf"]))
    return " ".join(parts)


def _link(href: str | None, label: str) -> str:
    if not href or not href.startswith(("https://", "http://")):
        return html.escape(label)
    return f'<a href="{html.escape(href, quote=True)}" style="color:#1849a9">{html.escape(label)}</a>'


def render(kind: str, alerts: list[dict[str, Any]], cfg: EmailConfig, *, org_id: str, user_id: str,
           now: datetime, risk_list_week: str | None = None) -> RenderedEmail:
    assert cfg.app_url and cfg.link_secret, "render needs a configured EmailConfig"
    scope = KIND_SCOPE[kind]
    issued = int(now.timestamp())
    unsub = links.unsubscribe_url(cfg.app_url, links.sign_unsubscribe(
        cfg.link_secret, org_id=org_id, user_id=user_id, scope=scope, issued_at=issued))
    unsub_all = links.unsubscribe_url(cfg.app_url, links.sign_unsubscribe(
        cfg.link_secret, org_id=org_id, user_id=user_id, scope="all", issued_at=issued))
    manage = links.manage_url(cfg.app_url)
    subject = subject_for(kind, alerts, now)
    intro = _intro(kind, len(alerts))

    text_lines = [intro, ""]
    blocks = []
    for a in alerts:
        url = links.screen_url(cfg.app_url, a["ref_url"])
        sev = SEVERITY_LABEL.get(a["severity"], a["severity"])
        when = t.day(a["occurred_on"])
        text_lines += [f"[{sev}] {a['headline']}", f"{a['rule_label']} · {when} · {a['tag']}", url]
        text_lines += [f"  {line}" for line in str(a["body"]).splitlines()]
        text_lines += [f"  Source: {_source_text(s)} {s.get('url') or ''}".rstrip() for s in _sources(a)]
        text_lines.append("")
        body_html = "<br>".join(html.escape(line) for line in str(a["body"]).splitlines())
        sources_html = "".join(f"<li>{_link(s.get('url'), _source_text(s))}</li>" for s in _sources(a))
        blocks.append(
            f'<tr><td style="padding:14px 0;border-top:1px solid #e4e7ec">'
            f'<div style="font-size:12px;font-weight:600;color:{SEVERITY_COLOR.get(a["severity"], "#475467")}">'
            f"{html.escape(sev)} · {html.escape(a['rule_label'])} · {html.escape(when)} · {html.escape(a['tag'])}</div>"
            f'<div style="font-size:16px;font-weight:600;margin:4px 0">{_link(url, a["headline"])}</div>'
            f'<div style="font-size:14px;line-height:1.5;color:#344054">{body_html}</div>'
            + (f'<ul style="font-size:13px;color:#475467;margin:6px 0 0;padding-left:18px">{sources_html}</ul>'
               if sources_html else "")
            + "</td></tr>"
        )
    extra_html = ""
    if risk_list_week:
        risk_url = f"{cfg.app_url}/risk-list?week={risk_list_week}"
        text_lines += [f"This week's risk list: {risk_url}", ""]
        extra_html = f'<p style="font-size:14px">{_link(risk_url, "This week's risk list")}</p>'
    footer = (f"You get this email because these names are on your firm's Marden watchlist. "
              f"Manage alert email: {manage}")
    text_lines += ["--", footer, f"Unsubscribe from {SCOPE_LABEL[scope]}: {unsub}",
                   f"Unsubscribe from {SCOPE_LABEL['all']}: {unsub_all}"]
    html_body = (
        '<!doctype html><html><body style="margin:0;padding:24px;background:#f9fafb;'
        'font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#101828">'
        '<table role="presentation" width="100%" style="max-width:640px;margin:0 auto;background:#ffffff;'
        'border:1px solid #e4e7ec;border-radius:8px;padding:20px 24px">'
        '<tr><td style="font-size:18px;font-weight:700;padding-bottom:4px">Marden</td></tr>'
        f'<tr><td style="font-size:14px;color:#475467;padding-bottom:8px">{html.escape(intro)}</td></tr>'
        + "".join(blocks)
        + f"<tr><td>{extra_html}</td></tr>"
        '<tr><td style="font-size:12px;color:#667085;padding-top:16px;border-top:1px solid #e4e7ec">'
        "You get this email because these names are on your firm's Marden watchlist.<br>"
        f"{_link(manage, 'Manage alert email')} · {_link(unsub, 'Unsubscribe from ' + SCOPE_LABEL[scope])}"
        f" · {_link(unsub_all, 'Unsubscribe from all alert email')}</td></tr>"
        "</table></body></html>"
    )
    headers = {"List-Unsubscribe": f"<{unsub}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
    return RenderedEmail(subject=subject, html=html_body, text="\n".join(text_lines), headers=headers)
