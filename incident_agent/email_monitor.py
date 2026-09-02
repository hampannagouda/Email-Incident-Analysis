"""Mailbox monitoring via Microsoft Graph (client-credentials / daemon flow)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import List, Optional

import msal
import requests

from .config import MailboxConfig
from .models import EmailMessage

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]

# Tags whose content is never useful as email text.
_SKIP_CONTENT_TAGS = {"style", "script", "head", "title"}
_BLOCK_TAGS = {"p", "div", "br", "tr", "li", "table", "h1", "h2", "h3", "h4"}


class _HtmlToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: List[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_CONTENT_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        lines = [line.strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def html_to_text(html: str) -> str:
    parser = _HtmlToText()
    parser.feed(html)
    return parser.text()


class GraphMailMonitor:
    """Polls one mailbox folder for new messages via Microsoft Graph."""

    def __init__(self, cfg: MailboxConfig):
        self.cfg = cfg
        self._app = msal.ConfidentialClientApplication(
            client_id=cfg.client_id,
            client_credential=cfg.client_secret,
            authority=f"https://login.microsoftonline.com/{cfg.tenant_id}",
        )

    def _token(self) -> str:
        result = self._app.acquire_token_for_client(scopes=GRAPH_SCOPE)
        if "access_token" not in result:
            raise RuntimeError(
                f"Graph auth failed: {result.get('error')} - {result.get('error_description')}"
            )
        return result["access_token"]

    def fetch_new_messages(self, since_utc: Optional[datetime] = None) -> List[EmailMessage]:
        """Return messages received after `since_utc` (default: lookback window)."""
        if since_utc is None:
            since_utc = datetime.now(timezone.utc) - timedelta(
                minutes=self.cfg.lookback_minutes
            )
        since_str = since_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        filters = [f"receivedDateTime ge {since_str}"]
        if self.cfg.only_unread:
            filters.append("isRead eq false")

        url = (
            f"{GRAPH_BASE}/users/{self.cfg.address}"
            f"/mailFolders/{self.cfg.folder}/messages"
        )
        params = {
            "$filter": " and ".join(filters),
            "$orderby": "receivedDateTime asc",
            "$top": "25",
            "$select": "id,internetMessageId,subject,from,receivedDateTime,body",
        }
        headers = {"Authorization": f"Bearer {self._token()}"}

        messages: List[EmailMessage] = []
        while url:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("value", []):
                messages.append(self._to_email(item))
            url = data.get("@odata.nextLink")
            params = None  # nextLink already carries the query string
        return messages

    @staticmethod
    def _to_email(item: dict) -> EmailMessage:
        body = item.get("body") or {}
        content = body.get("content", "")
        if (body.get("contentType") or "").lower() == "html":
            content = html_to_text(content)
        sender = (
            (item.get("from") or {}).get("emailAddress", {}).get("address", "")
        )
        received = item.get("receivedDateTime")
        return EmailMessage(
            id=item.get("id", ""),
            internet_message_id=item.get("internetMessageId"),
            subject=item.get("subject") or "",
            sender=sender,
            received=datetime.fromisoformat(received.replace("Z", "+00:00"))
            if received
            else None,
            body_text=content.strip(),
        )

    def mark_read(self, message_id: str) -> None:
        url = f"{GRAPH_BASE}/users/{self.cfg.address}/messages/{message_id}"
        headers = {"Authorization": f"Bearer {self._token()}"}
        resp = requests.patch(url, headers=headers, json={"isRead": True}, timeout=30)
        if resp.status_code >= 400:
            log.warning("Could not mark message %s as read: %s", message_id, resp.text)


def matches_filters(email: EmailMessage, cfg: MailboxConfig) -> bool:
    """Cheap pre-filter before spending an LLM call. Empty filters = accept all."""
    if cfg.from_addresses:
        if email.sender.lower() not in {a.lower() for a in cfg.from_addresses}:
            return False
    if cfg.subject_keywords:
        subject = email.subject.lower()
        if not any(kw.lower() in subject for kw in cfg.subject_keywords):
            return False
    return True
