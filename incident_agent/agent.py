"""Orchestration: poll mailbox -> filter -> analyze -> route -> notify -> record."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import List, Optional

from .analyzer import IncidentAnalyzer
from .config import AgentConfig
from .email_monitor import GraphMailMonitor, matches_filters
from .models import EmailMessage, ProcessedIncident
from .state import ProcessedStore
from .teams_notifier import build_adaptive_card, post_to_teams

log = logging.getLogger(__name__)


class IncidentEmailAgent:
    def __init__(self, config: AgentConfig, use_llm: bool = True, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        self.analyzer = IncidentAnalyzer(config.claude, use_llm=use_llm)
        self.store = ProcessedStore(config.state.path)
        self._monitor: Optional[GraphMailMonitor] = None

    @property
    def monitor(self) -> GraphMailMonitor:
        if self._monitor is None:
            self._monitor = GraphMailMonitor(self.config.mailbox)
        return self._monitor

    # --- single email pipeline -------------------------------------------

    def process_email(self, email: EmailMessage) -> Optional[ProcessedIncident]:
        """Analyze one email and post to Teams. Returns None if skipped."""
        analysis, source = self.analyzer.analyze(email)

        if not analysis.is_incident:
            log.info("Skipping non-incident email: %r", email.subject)
            return None
        if analysis.severity in self.config.teams.suppress_severities:
            log.info(
                "Suppressed %s notification for %r per config",
                analysis.severity,
                email.subject,
            )
            return None

        webhook = self.config.teams.webhook_for(analysis.severity)
        ticket_url = self.config.ticketing.url_for(analysis.incident_number)
        payload = build_adaptive_card(
            analysis, email, ticket_url=ticket_url, degraded=(source == "regex-fallback")
        )

        posted = False
        if self.dry_run:
            log.info("[dry-run] Would post to %s", webhook or "<no webhook configured>")
            print(json.dumps(payload, indent=2))
        elif not webhook:
            log.error(
                "No Teams webhook configured for severity %s and no default set; "
                "card not posted",
                analysis.severity,
            )
        else:
            posted = post_to_teams(webhook, payload)

        return ProcessedIncident(
            email=email,
            analysis=analysis,
            analysis_source=source,  # type: ignore[arg-type]
            webhook_used=webhook,
            posted=posted,
        )

    # --- polling loop ------------------------------------------------------

    def run_once(self) -> List[ProcessedIncident]:
        since = None
        if self.store.last_poll_utc:
            since = datetime.fromisoformat(self.store.last_poll_utc)
        poll_started = datetime.now(timezone.utc)

        emails = self.monitor.fetch_new_messages(since_utc=since)
        log.info("Fetched %d message(s) from %s", len(emails), self.config.mailbox.address)

        results: List[ProcessedIncident] = []
        for email in emails:
            if self.store.is_processed(email.id):
                continue
            if not matches_filters(email, self.config.mailbox):
                log.debug("Filtered out: %r", email.subject)
                self.store.mark_processed(email.id)
                continue

            try:
                result = self.process_email(email)
            except Exception:
                # Leave the email unmarked so the next poll retries it.
                log.exception("Failed to process %r; will retry next poll", email.subject)
                continue

            self.store.mark_processed(email.id)
            if result is not None:
                results.append(result)
                if result.posted:
                    log.info(
                        "Posted %s %s to Teams: %s",
                        result.analysis.severity,
                        result.analysis.incident_number or "(no number)",
                        result.analysis.summary,
                    )
            if self.config.mailbox.mark_as_read and not self.dry_run:
                self.monitor.mark_read(email.id)

        self.store.last_poll_utc = poll_started.isoformat()
        self.store.save()
        return results

    def watch(self) -> None:
        interval = max(15, self.config.mailbox.poll_interval_seconds)
        log.info("Watching mailbox every %ds. Ctrl+C to stop.", interval)
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("Poll cycle failed; retrying after interval")
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                log.info("Stopped.")
                return
