"""Teams notifications: Adaptive Card construction + webhook delivery.

Cards are posted to Microsoft Teams Workflows webhooks ("When a Teams webhook
request is received" trigger) using the standard message/attachments envelope.
The legacy Office 365 connector format is retired; Workflows is the supported path.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

from .models import EmailMessage, IncidentAnalysis

log = logging.getLogger(__name__)

_SEVERITY_STYLE = {
    "SEV1": ("attention", "\U0001F534"),  # red circle
    "SEV2": ("warning", "\U0001F7E0"),    # orange circle
    "SEV3": ("accent", "\U0001F7E1"),     # yellow circle
    "SEV4": ("good", "\U0001F7E2"),       # green circle
    "UNKNOWN": ("default", "⚪"),     # white circle
}


def build_adaptive_card(
    analysis: IncidentAnalysis,
    email: EmailMessage,
    ticket_url: Optional[str] = None,
    degraded: bool = False,
) -> dict:
    """Build the Workflows webhook payload containing one Adaptive Card."""
    style, emoji = _SEVERITY_STYLE.get(analysis.severity, _SEVERITY_STYLE["UNKNOWN"])

    title_bits = [emoji, analysis.severity, "Incident"]
    if analysis.incident_number:
        title_bits.append(f"— {analysis.incident_number}")

    facts = [
        {"title": "Incident #", "value": analysis.incident_number or "Not provided"},
        {"title": "Severity", "value": analysis.severity},
        {
            "title": "Affected systems",
            "value": ", ".join(analysis.affected_systems) or "Not identified",
        },
        {"title": "Reported by", "value": email.sender or "Unknown"},
    ]
    if email.received:
        facts.append(
            {"title": "Received", "value": email.received.strftime("%Y-%m-%d %H:%M UTC")}
        )
    if analysis.impact:
        facts.append({"title": "Impact", "value": analysis.impact})

    body = [
        {
            "type": "Container",
            "style": style,
            "bleed": True,
            "items": [
                {
                    "type": "TextBlock",
                    "size": "Large",
                    "weight": "Bolder",
                    "wrap": True,
                    "text": " ".join(title_bits),
                }
            ],
        },
        {
            "type": "TextBlock",
            "wrap": True,
            "weight": "Bolder",
            "spacing": "Medium",
            "text": analysis.summary,
        },
        {"type": "FactSet", "facts": facts},
        {
            "type": "TextBlock",
            "wrap": True,
            "spacing": "Medium",
            "text": analysis.issue_description,
        },
    ]

    if analysis.recommended_actions:
        body.append(
            {
                "type": "TextBlock",
                "weight": "Bolder",
                "spacing": "Medium",
                "text": "Recommended actions",
            }
        )
        body.append(
            {
                "type": "TextBlock",
                "wrap": True,
                "text": "\n".join(f"- {a}" for a in analysis.recommended_actions),
            }
        )

    footer = "Posted by Incident Email Agent"
    if degraded:
        footer += " (basic extraction — AI analysis unavailable)"
    body.append(
        {
            "type": "TextBlock",
            "size": "Small",
            "isSubtle": True,
            "wrap": True,
            "spacing": "Medium",
            "text": footer,
        }
    )

    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "msteams": {"width": "Full"},
        "body": body,
    }
    if ticket_url:
        card["actions"] = [
            {"type": "Action.OpenUrl", "title": "Open ticket", "url": ticket_url}
        ]

    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": card,
            }
        ],
    }


def post_to_teams(webhook_url: str, payload: dict, max_attempts: int = 3) -> bool:
    """POST the card, retrying on 429/5xx with backoff. Returns True on success."""
    delay = 2.0
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.post(webhook_url, json=payload, timeout=30)
        except requests.RequestException as exc:
            log.warning("Teams post attempt %d failed: %s", attempt, exc)
        else:
            if resp.status_code < 300:
                return True
            if resp.status_code not in (429, 500, 502, 503, 504):
                log.error("Teams webhook rejected the card (%d): %s", resp.status_code, resp.text[:500])
                return False
            log.warning("Teams post attempt %d got HTTP %d", attempt, resp.status_code)
        if attempt < max_attempts:
            time.sleep(delay)
            delay *= 2
    log.error("Giving up posting to Teams after %d attempts", max_attempts)
    return False
