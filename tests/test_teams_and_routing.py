"""Tests for the Adaptive Card builder, webhook routing, and dedup state."""

from datetime import datetime, timezone

from incident_agent.config import TeamsConfig, TicketingConfig
from incident_agent.models import EmailMessage, IncidentAnalysis
from incident_agent.state import ProcessedStore
from incident_agent.teams_notifier import build_adaptive_card


def _analysis(severity="SEV1", number="INC0045871") -> IncidentAnalysis:
    return IncidentAnalysis(
        is_incident=True,
        incident_number=number,
        severity=severity,
        affected_systems=["Oracle RAC PRD01", "OMS"],
        issue_description="Production database cluster unreachable since 06:38 UTC.",
        summary="Production DB outage - EMEA order processing halted",
        impact="~4,200 users, EMEA order intake stopped",
        recommended_actions=["Join the P1 bridge", "Engage storage vendor"],
        confidence=0.95,
    )


def _email() -> EmailMessage:
    return EmailMessage(
        id="m1",
        subject="[P1] INC0045871",
        sender="servicenow@yourcompany.com",
        received=datetime(2026, 8, 19, 6, 42, tzinfo=timezone.utc),
        body_text="...",
    )


def test_card_structure_and_severity_style():
    payload = build_adaptive_card(_analysis(), _email(), ticket_url="https://snow/inc")
    assert payload["type"] == "message"
    card = payload["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard"
    assert card["body"][0]["style"] == "attention"  # SEV1 -> red header
    facts = {f["title"]: f["value"] for f in card["body"][2]["facts"]}
    assert facts["Incident #"] == "INC0045871"
    assert facts["Severity"] == "SEV1"
    assert "Oracle RAC PRD01" in facts["Affected systems"]
    assert card["actions"][0]["url"] == "https://snow/inc"


def test_card_degraded_footer_and_missing_fields():
    analysis = _analysis(severity="UNKNOWN", number=None)
    analysis.recommended_actions = []
    payload = build_adaptive_card(analysis, _email(), degraded=True)
    card = payload["attachments"][0]["content"]
    assert "actions" not in card
    footer = card["body"][-1]["text"]
    assert "basic extraction" in footer
    facts = {f["title"]: f["value"] for f in card["body"][2]["facts"]}
    assert facts["Incident #"] == "Not provided"


def test_webhook_routing_with_default_fallback():
    teams = TeamsConfig(
        default_webhook_url="https://hooks/default",
        severity_webhooks={"SEV1": "https://hooks/major"},
    )
    assert teams.webhook_for("SEV1") == "https://hooks/major"
    assert teams.webhook_for("SEV3") == "https://hooks/default"
    assert TeamsConfig().webhook_for("SEV1") is None


def test_ticket_url_template():
    ticketing = TicketingConfig(url_template="https://snow/inc?number={incident_number}")
    assert ticketing.url_for("INC001") == "https://snow/inc?number=INC001"
    assert ticketing.url_for(None) is None
    assert TicketingConfig().url_for("INC001") is None


def test_ticket_url_rejects_unsafe_incident_numbers():
    ticketing = TicketingConfig(url_template="https://snow/inc?number={incident_number}")
    # URL syntax, spaces, or oversized strings from a hostile email never
    # reach the card's link.
    assert ticketing.url_for("INC001&redirect=//evil.com") is None
    assert ticketing.url_for("INC 001") is None
    assert ticketing.url_for("../../etc") is None
    assert ticketing.url_for("A" * 41) is None


def test_state_dedup_persists(tmp_path):
    path = tmp_path / "state.json"
    store = ProcessedStore(path)
    assert not store.is_processed("a")
    store.mark_processed("a")
    store.last_poll_utc = "2026-08-19T07:00:00+00:00"
    store.save()

    reloaded = ProcessedStore(path)
    assert reloaded.is_processed("a")
    assert not reloaded.is_processed("b")
    assert reloaded.last_poll_utc == "2026-08-19T07:00:00+00:00"
