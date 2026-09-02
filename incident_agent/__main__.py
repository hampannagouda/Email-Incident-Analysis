"""CLI entry point: python -m incident_agent [--once|--watch|--test-email FILE]"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .agent import IncidentEmailAgent
from .config import load_config
from .models import EmailMessage


def _email_from_file(path: Path) -> EmailMessage:
    """Build an EmailMessage from a plain-text file.

    Format: optional 'Subject:' / 'From:' header lines at the top, then a blank
    line, then the body. Files without headers are treated as body-only.
    """
    text = path.read_text(encoding="utf-8")
    subject, sender = path.stem, "test@local"
    lines = text.splitlines()
    body_start = 0
    for i, line in enumerate(lines):
        low = line.lower()
        if low.startswith("subject:"):
            subject = line.split(":", 1)[1].strip()
            body_start = i + 1
        elif low.startswith("from:"):
            sender = line.split(":", 1)[1].strip()
            body_start = i + 1
        elif not line.strip():
            if body_start:
                body_start = i + 1
            break
        else:
            break
    body = "\n".join(lines[body_start:]).strip()
    return EmailMessage(
        id=f"local-{path.name}",
        subject=subject,
        sender=sender,
        received=datetime.now(timezone.utc),
        body_text=body or text,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="incident_agent",
        description="Monitor incident emails, analyze with Claude, notify Microsoft Teams.",
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Poll the mailbox one time and exit")
    mode.add_argument("--watch", action="store_true", help="Poll continuously (default)")
    mode.add_argument(
        "--test-email",
        metavar="FILE",
        help="Analyze a local .txt email file instead of polling the mailbox",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyze and print the Teams card JSON without posting",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip Claude and use the regex fallback extractor only",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(args.config)
    agent = IncidentEmailAgent(config, use_llm=not args.no_llm, dry_run=args.dry_run)

    if args.test_email:
        email = _email_from_file(Path(args.test_email))
        result = agent.process_email(email)
        if result is None:
            print("Email classified as non-incident (or suppressed); nothing to post.")
            return 0
        print(
            f"\n=== Analysis ({result.analysis_source}) ===\n"
            f"{result.analysis.model_dump_json(indent=2)}"
        )
        if not args.dry_run:
            print(f"Posted to Teams: {result.posted}")
        return 0

    if args.once:
        results = agent.run_once()
        print(f"Processed {len(results)} incident email(s).")
        return 0

    agent.watch()
    return 0


if __name__ == "__main__":
    sys.exit(main())
