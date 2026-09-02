# Incident Email Agent

An AI agent that monitors an incident mailbox, analyzes each email with Claude,
extracts key details (incident number, severity, affected systems, issue
description), and posts a color-coded Adaptive Card to the appropriate
Microsoft Teams channel — improving incident visibility and cutting manual
triage effort.

```
┌──────────────┐    poll     ┌──────────────────┐   structured    ┌───────────────────┐
│  Mailbox     │────────────▶│  Incident Agent  │────extraction──▶│  Claude API       │
│ (MS Graph)   │             │  (this project)  │◀────analysis────│  (claude-opus-5)  │
└──────────────┘             └────────┬─────────┘                 └───────────────────┘
                                      │ severity-routed Adaptive Card
                                      ▼
                          ┌───────────────────────────┐
                          │ Microsoft Teams channels  │
                          │ (Workflows webhooks)      │
                          └───────────────────────────┘
```

## How it works

1. **Monitor** — polls a mailbox folder via Microsoft Graph for new (optionally
   unread) messages, with optional sender/subject pre-filters. Processed
   message IDs are stored in `.state/processed.json` so nothing is posted twice.
2. **Analyze** — each candidate email goes to Claude with a structured-output
   schema. The model returns a validated object: `is_incident`,
   `incident_number`, `severity` (normalized to SEV1–SEV4), `affected_systems`,
   `issue_description`, `summary`, `impact`, `recommended_actions`, and a
   `confidence` score. Non-incident mail (newsletters, chit-chat) is dropped.
   If the Claude API is unreachable or declines, a **regex fallback extractor**
   still pulls the incident number / severity / affected-CI lines so
   notifications keep flowing (the card is marked "basic extraction").
3. **Notify** — an Adaptive Card (red/orange/yellow/green header by severity)
   is posted to the Teams channel mapped to that severity, with an optional
   "Open ticket" button that deep-links into your ITSM tool.

## Setup

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. Azure AD app registration (mailbox access)

1. Azure Portal → **Microsoft Entra ID → App registrations → New registration**.
2. Add an **API permission**: Microsoft Graph → **Application permissions** →
   `Mail.Read` (add `Mail.ReadWrite` if you want the agent to mark mail as read)
   → **Grant admin consent**.
3. Create a **client secret** (Certificates & secrets).
4. Recommended: scope the app to only the incident mailbox with an
   [application access policy](https://learn.microsoft.com/en-us/graph/auth-limit-mailbox-access)
   (`New-ApplicationAccessPolicy` in Exchange Online PowerShell).

### 3. Teams webhook (per channel)

The legacy Office 365 "Incoming Webhook" connectors are retired — use
**Workflows**:

1. In the target Teams channel: **⋯ → Workflows → "Post to a channel when a
   webhook request is received"** (a Power Automate template).
2. Copy the generated HTTP URL.
3. Repeat for each channel (e.g. a *Major Incidents* channel for SEV1/SEV2 and
   an *IT Ops* channel for the rest).

### 4. Configure

```bash
cp config.yaml.example config.yaml
cp .env.example .env
```

Fill in `.env` (Azure credentials, `ANTHROPIC_API_KEY`, webhook URLs) and edit
`config.yaml` (mailbox address, severity→channel routing, filters, optional
ServiceNow URL template). Any config value written as `env:VAR_NAME` is read
from the environment/.env — keep all secrets there.

## Running

```bash
# Continuous monitoring (default; polls every poll_interval_seconds)
python -m incident_agent --watch

# Single poll cycle (ideal for Task Scheduler / cron)
python -m incident_agent --once

# Analyze a local email file without touching the mailbox; print card JSON only
python -m incident_agent --test-email tests/sample_emails/sev1_database_outage.txt --dry-run

# Same, but without the Claude API (regex fallback only — no API key needed)
python -m incident_agent --test-email tests/sample_emails/sev1_database_outage.txt --dry-run --no-llm
```

`--dry-run` works with `--once`/`--watch` too: it analyzes real mailbox mail and
prints the cards instead of posting — useful for tuning before going live.

### Run on a schedule (Windows)

```powershell
schtasks /Create /TN "IncidentEmailAgent" /SC MINUTE /MO 5 `
  /TR "python -m incident_agent --once --config \"C:\path\to\config.yaml\""
```

Or keep `--watch` running as a service (NSSM / Task Scheduler "At startup").

## Tests

```bash
python -m pytest tests -q
```

The test suite covers the regex fallback extraction, severity normalization,
HTML-to-text conversion, sender/subject filters, Adaptive Card structure,
severity→webhook routing, and dedup-state persistence — everything that runs
without external services.

## Project layout

```
incident_agent/
  __main__.py       CLI (--watch / --once / --test-email / --dry-run / --no-llm)
  agent.py          Orchestration: poll → filter → analyze → route → post → record
  email_monitor.py  Microsoft Graph polling + HTML→text
  analyzer.py       Claude structured extraction + regex fallback
  teams_notifier.py Adaptive Card builder + webhook delivery (with retries)
  config.py         config.yaml + .env loading (env: indirection for secrets)
  state.py          Processed-message dedup store
  models.py         Pydantic models incl. the IncidentAnalysis schema
tests/              Unit tests + sample incident emails
```

## Design notes

- **Structured outputs, not prompt-and-pray**: extraction uses the Claude API's
  structured output support (`messages.parse` with a Pydantic schema), so the
  response is always a validated `IncidentAnalysis` — no JSON parsing failures.
- **Refusal/outage resilience**: any Claude failure (network, rate limit after
  SDK retries, or a safety refusal) degrades to the regex extractor instead of
  dropping the notification; the card footer flags degraded analysis.
- **At-least-once with dedup**: an email is only marked processed after the
  pipeline ran; a crash mid-poll means it is retried next cycle, and the ID
  store prevents duplicate Teams posts.
- **Severity routing** lives entirely in config, so channels can be re-mapped
  without code changes; `suppress_severities` can silence low-priority noise.
