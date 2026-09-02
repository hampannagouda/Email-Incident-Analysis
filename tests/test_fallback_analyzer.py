"""Tests for the regex fallback extractor and email filters."""

from datetime import datetime, timezone
from pathlib import Path

from incident_agent.analyzer import RegexFallbackAnalyzer
from incident_agent.config import MailboxConfig
from incident_agent.email_monitor import html_to_text, matches_filters
from incident_agent.models import EmailMessage

SAMPLES = Path(__file__).parent / "sample_emails"


def _email(subject: str, body: str, sender: str = "x@y.com") -> EmailMessage:
    return EmailMessage(
        id="t1",
        subject=subject,
        sender=sender,
        received=datetime.now(timezone.utc),
        body_text=body,
    )


def test_extracts_incident_number_and_severity():
    body = (SAMPLES / "sev1_database_outage.txt").read_text(encoding="utf-8")
    email = _email("[P1] INC0045871 - Production database cluster down", body)
    analysis = RegexFallbackAnalyzer().analyze(email)
    assert analysis.is_incident
    assert analysis.incident_number == "INC0045871"
    assert analysis.severity == "SEV1"
    assert any("srv-db-prod-02" in s for s in analysis.affected_systems)


def test_severity_normalization_variants():
    fallback = RegexFallbackAnalyzer()
    assert fallback.analyze(_email("Sev 2 outage", "high impact")).severity == "SEV2"
    assert fallback.analyze(_email("issue", "Priority: 3")).severity == "SEV3"
    assert fallback.analyze(_email("minor glitch", "P4 low")).severity == "SEV4"
    assert fallback.analyze(_email("hello", "lunch plans?")).severity == "UNKNOWN"


def test_freeform_outage_email_is_flagged():
    body = (SAMPLES / "freeform_vpn_degradation.txt").read_text(encoding="utf-8")
    email = _email("VPN really slow again for the Bangalore office", body)
    analysis = RegexFallbackAnalyzer().analyze(email)
    assert analysis.is_incident
    assert analysis.severity == "UNKNOWN"  # no explicit priority in the email


def test_non_incident_email_not_flagged():
    body = (SAMPLES / "non_incident_newsletter.txt").read_text(encoding="utf-8")
    email = _email("IT Monthly Newsletter - August edition", body)
    analysis = RegexFallbackAnalyzer().analyze(email)
    assert not analysis.is_incident


def test_subject_keyword_filter():
    cfg = MailboxConfig(subject_keywords=["incident", "outage"])
    assert matches_filters(_email("Major OUTAGE in prod", "..."), cfg)
    assert not matches_filters(_email("Team lunch Friday", "..."), cfg)


def test_from_address_filter():
    cfg = MailboxConfig(from_addresses=["Alerts@Corp.com"])
    assert matches_filters(_email("x", "y", sender="alerts@corp.com"), cfg)
    assert not matches_filters(_email("x", "y", sender="other@corp.com"), cfg)


def test_empty_filters_accept_everything():
    assert matches_filters(_email("anything", "at all"), MailboxConfig())


def test_html_to_text_strips_markup():
    html = (
        "<html><head><style>p{color:red}</style></head><body>"
        "<p>Incident <b>INC0001234</b></p><div>Priority: P1</div></body></html>"
    )
    text = html_to_text(html)
    assert "INC0001234" in text
    assert "Priority: P1" in text
    assert "color:red" not in text
