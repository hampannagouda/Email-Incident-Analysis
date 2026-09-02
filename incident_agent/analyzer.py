"""Incident analysis: Claude structured extraction with a regex fallback.

The primary path sends the email to Claude and gets back a validated
IncidentAnalysis (via messages.parse + Pydantic). If the API is unreachable,
rate-limited beyond retries, or declines the request, the regex fallback keeps
notifications flowing with whatever fields it can pull from the raw text.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional, Tuple

import anthropic

from .config import ClaudeConfig
from .models import EmailMessage, IncidentAnalysis

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an incident-triage analyst for an enterprise IT operations team.
You will receive one email (subject, sender, timestamp, body). Extract the incident
details into the required structure.

Rules:
- Base every field strictly on the email content. Never invent incident numbers,
  system names, or impact statements that are not supported by the text.
- Normalize severity: P1/Priority 1/Critical -> SEV1; P2/High/Major -> SEV2;
  P3/Medium/Moderate -> SEV3; P4/Low/Minor -> SEV4; otherwise UNKNOWN.
- Ignore email signatures, legal disclaimers, and quoted reply chains unless they
  contain the only incident details.
- If the email is not about an IT incident, outage, degradation, or alert, set
  is_incident to false and keep the other fields minimal.
- Write issue_description and summary for an on-call engineer: specific, factual,
  no filler."""


def render_email(email: EmailMessage) -> str:
    received = email.received.isoformat() if email.received else "unknown"
    return (
        f"Subject: {email.subject}\n"
        f"From: {email.sender}\n"
        f"Received: {received}\n"
        f"--- Email body ---\n"
        f"{email.body_text}"
    )


class AnalyzerError(Exception):
    pass


class AnalyzerCredentialsError(AnalyzerError):
    """Missing or rejected Claude credentials — not recoverable within a run."""


class ClaudeAnalyzer:
    def __init__(self, cfg: ClaudeConfig):
        self.cfg = cfg
        self.client = anthropic.Anthropic()

    def analyze(self, email: EmailMessage) -> IncidentAnalysis:
        try:
            response = self.client.messages.parse(
                model=self.cfg.model,
                max_tokens=self.cfg.max_tokens,
                output_config={"effort": self.cfg.effort},
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": render_email(email)}],
                output_format=IncidentAnalysis,
            )
        except anthropic.AuthenticationError as exc:
            raise AnalyzerCredentialsError("Claude API key was rejected") from exc
        except anthropic.RateLimitError as exc:
            raise AnalyzerError("Claude API rate limited") from exc
        except anthropic.APIStatusError as exc:
            raise AnalyzerError(f"Claude API error ({exc.status_code})") from exc
        except anthropic.APIConnectionError as exc:
            raise AnalyzerError("Cannot reach the Claude API") from exc
        except TypeError as exc:
            # The SDK raises TypeError at request time when no credentials
            # resolve (no ANTHROPIC_API_KEY, auth token, or profile).
            raise AnalyzerCredentialsError(
                "No Claude API credentials configured (set ANTHROPIC_API_KEY in .env)"
            ) from exc

        if getattr(response, "stop_reason", None) == "refusal":
            raise AnalyzerError("Claude declined to process this email")
        if response.parsed_output is None:
            raise AnalyzerError("Claude returned no parsable output")
        return response.parsed_output


# --- Regex fallback -----------------------------------------------------------

INCIDENT_NUMBER_RE = re.compile(
    r"\b((?:INC|CRQ|PRB|CHG|REQ|RITM|TASK|SR|IM|PM)[-# ]?\d{4,})\b", re.IGNORECASE
)

_SEVERITY_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("SEV1", re.compile(r"\b(sev(?:erity)?[\s:\-]*1|p1|priority[\s:\-]*1|critical)\b", re.I)),
    ("SEV2", re.compile(r"\b(sev(?:erity)?[\s:\-]*2|p2|priority[\s:\-]*2|high|major)\b", re.I)),
    ("SEV3", re.compile(r"\b(sev(?:erity)?[\s:\-]*3|p3|priority[\s:\-]*3|medium|moderate)\b", re.I)),
    ("SEV4", re.compile(r"\b(sev(?:erity)?[\s:\-]*4|p4|priority[\s:\-]*4|low|minor)\b", re.I)),
]

_AFFECTED_LINE_RE = re.compile(
    r"^(?:affected (?:system|systems|ci|service|services|application|applications)"
    r"|configuration item|impacted (?:system|systems|service|services)|ci)"
    r"\s*[:\-]\s*(.+)$",
    re.IGNORECASE,
)

_INCIDENT_HINT_RE = re.compile(
    r"\b(incident|outage|down|degrad\w*|unavailable|unreachable|failure|failed|failing"
    r"|alert|sev\s*\d|p[1-4]\b|unusable|not working|crash\w*|urgent\w*|affected"
    r"|drop(?:s|ping|ped)?|slow|errors?)\b",
    re.IGNORECASE,
)


class RegexFallbackAnalyzer:
    """Best-effort extraction when the LLM is unavailable. No invention: only
    fields that literally appear in the text are filled in."""

    def analyze(self, email: EmailMessage) -> IncidentAnalysis:
        text = f"{email.subject}\n{email.body_text}"

        number_match = INCIDENT_NUMBER_RE.search(text)
        incident_number = number_match.group(1).upper().replace(" ", "") if number_match else None

        severity = "UNKNOWN"
        for label, pattern in _SEVERITY_PATTERNS:
            if pattern.search(text):
                severity = label
                break

        affected: List[str] = []
        for line in email.body_text.splitlines():
            m = _AFFECTED_LINE_RE.match(line.strip())
            if m:
                affected.extend(
                    part.strip() for part in re.split(r"[,;/]", m.group(1)) if part.strip()
                )

        is_incident = bool(
            incident_number or severity != "UNKNOWN" or _INCIDENT_HINT_RE.search(text)
        )

        snippet = " ".join(email.body_text.split())[:300]
        return IncidentAnalysis(
            is_incident=is_incident,
            incident_number=incident_number,
            severity=severity,  # type: ignore[arg-type]
            affected_systems=affected,
            issue_description=snippet or email.subject,
            summary=email.subject[:120] or "Incident email received",
            impact=None,
            recommended_actions=[],
            confidence=0.3,
        )


class IncidentAnalyzer:
    """Claude first, regex fallback second. Returns (analysis, source)."""

    def __init__(self, cfg: ClaudeConfig, use_llm: bool = True):
        self.fallback = RegexFallbackAnalyzer()
        self.claude: Optional[ClaudeAnalyzer] = ClaudeAnalyzer(cfg) if use_llm else None

    def analyze(self, email: EmailMessage) -> Tuple[IncidentAnalysis, str]:
        if self.claude is not None:
            try:
                return self.claude.analyze(email), "claude"
            except AnalyzerCredentialsError as exc:
                log.warning(
                    "%s — disabling AI analysis for this run; using regex fallback", exc
                )
                self.claude = None
            except AnalyzerError as exc:
                log.warning("Claude analysis failed (%s); using regex fallback", exc)
        return self.fallback.analyze(email), "regex-fallback"
