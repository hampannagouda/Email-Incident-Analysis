"""Data models shared across the incident agent."""

from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

Severity = Literal["SEV1", "SEV2", "SEV3", "SEV4", "UNKNOWN"]

SEVERITY_ORDER = ["SEV1", "SEV2", "SEV3", "SEV4", "UNKNOWN"]


class IncidentAnalysis(BaseModel):
    """Structured incident details extracted from a single email."""

    is_incident: bool = Field(
        description=(
            "True only if the email reports an IT incident, outage, degradation, or "
            "monitoring alert. False for newsletters, meeting invites, chit-chat, "
            "marketing, or replies that add no new incident information."
        )
    )
    incident_number: Optional[str] = Field(
        default=None,
        description=(
            "Ticket or incident identifier exactly as written in the email, e.g. "
            "'INC0012345', 'CRQ000123', 'PRB0004567'. null if none is present."
        ),
    )
    severity: Severity = Field(
        description=(
            "Normalized severity. Map P1/Priority 1/Critical -> SEV1, "
            "P2/High/Major -> SEV2, P3/Medium/Moderate -> SEV3, P4/Low/Minor -> SEV4. "
            "UNKNOWN only when the email gives no severity signal at all."
        )
    )
    affected_systems: List[str] = Field(
        description=(
            "Names of applications, servers, services, or configuration items that are "
            "impacted (e.g. 'SAP ECC', 'Exchange Online', 'srv-db-prod-02'). Empty list "
            "if none can be identified."
        )
    )
    issue_description: str = Field(
        description=(
            "Factual description of the problem in 1-3 sentences: what is broken, since "
            "when, and any error symptoms. Written for an on-call engineer."
        )
    )
    summary: str = Field(
        description=(
            "One-line headline (max ~120 chars) suitable as a notification title, e.g. "
            "'Production SAP outage - order processing halted in EMEA'."
        )
    )
    impact: Optional[str] = Field(
        default=None,
        description=(
            "Business/user impact if stated or clearly inferable (users affected, "
            "regions, revenue impact). null if not determinable."
        ),
    )
    recommended_actions: List[str] = Field(
        description=(
            "Short, concrete next steps for responders (max 4). Empty list if the email "
            "already states the incident is resolved and no action is needed."
        )
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Confidence (0-1) that the extracted fields are correct given how explicit "
            "the email was."
        ),
    )


class EmailMessage(BaseModel):
    """A normalized email pulled from the mailbox (or a local test file)."""

    id: str
    internet_message_id: Optional[str] = None
    subject: str = ""
    sender: str = ""
    received: Optional[datetime] = None
    body_text: str = ""


class ProcessedIncident(BaseModel):
    """The end-to-end result for one email, used for logging and dry runs."""

    email: EmailMessage
    analysis: IncidentAnalysis
    analysis_source: Literal["claude", "regex-fallback"]
    webhook_used: Optional[str] = None
    posted: bool = False
