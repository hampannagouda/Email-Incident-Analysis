"""Configuration loading: config.yaml + .env, with env: indirection for secrets."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


def _resolve_env(value: Any) -> Any:
    """Resolve 'env:VAR_NAME' strings to the environment variable's value."""
    if isinstance(value, str) and value.startswith("env:"):
        return os.environ.get(value[4:], "")
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    return value


class MailboxConfig(BaseModel):
    address: str = ""
    folder: str = "inbox"
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    poll_interval_seconds: int = 60
    lookback_minutes: int = 60
    only_unread: bool = True
    mark_as_read: bool = True
    from_addresses: List[str] = Field(default_factory=list)
    subject_keywords: List[str] = Field(default_factory=list)


class ClaudeConfig(BaseModel):
    model: str = "claude-opus-5"
    effort: str = "high"
    max_tokens: int = 8000


class TeamsConfig(BaseModel):
    default_webhook_url: str = ""
    severity_webhooks: Dict[str, str] = Field(default_factory=dict)
    # Skip posting entirely for these severities (e.g. ["SEV4"]).
    suppress_severities: List[str] = Field(default_factory=list)

    def webhook_for(self, severity: str) -> Optional[str]:
        url = self.severity_webhooks.get(severity) or self.default_webhook_url
        return url or None


# Incident numbers come from email content (possibly attacker-controlled), so
# only plain ticket-shaped identifiers may be substituted into the ticket URL.
_SAFE_INCIDENT_NUMBER = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


class TicketingConfig(BaseModel):
    # e.g. "https://instance.service-now.com/nav_to.do?uri=incident.do%3Fsysparm_query=number%3D{incident_number}"
    url_template: str = ""

    def url_for(self, incident_number: Optional[str]) -> Optional[str]:
        if not self.url_template or not incident_number:
            return None
        if not _SAFE_INCIDENT_NUMBER.match(incident_number):
            return None
        try:
            return self.url_template.format(
                incident_number=quote(incident_number, safe="")
            )
        except (KeyError, IndexError, ValueError):
            return None


class StateConfig(BaseModel):
    path: str = ".state/processed.json"


class AgentConfig(BaseModel):
    mailbox: MailboxConfig = Field(default_factory=MailboxConfig)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    teams: TeamsConfig = Field(default_factory=TeamsConfig)
    ticketing: TicketingConfig = Field(default_factory=TicketingConfig)
    state: StateConfig = Field(default_factory=StateConfig)


def load_config(path: str | Path = "config.yaml") -> AgentConfig:
    """Load config.yaml (if present) after loading .env from the working directory."""
    load_dotenv()
    config_path = Path(path)
    raw: Dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    return AgentConfig.model_validate(_resolve_env(raw))
