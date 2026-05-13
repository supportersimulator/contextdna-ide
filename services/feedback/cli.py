"""``context-dna-ide feedback`` — Click-based operator CLI.

Usage examples
--------------
    context-dna-ide feedback bug "panel HF crashes on M1" \\
        --body "S2 Professor panel SIGABRT after 30s on cold cache" \\
        --severity critical --attach /tmp/panic.log

    context-dna-ide feedback success_story "shift trades flow nailed it" \\
        --body "First-try success after S2 wisdom update"

    context-dna-ide feedback list --limit 10

The CLI is intentionally thin — every interesting choice lives in
:mod:`.handler`. Failures are surfaced on stderr with a non-zero exit code
(2 = ledger write failed but fallback ok, 3 = fallback also failed) so
operators can tell the difference between "logged" and "lost".
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
from typing import Any

try:
    import click
except ImportError as exc:  # pragma: no cover — install hint, not a silent skip
    sys.stderr.write(
        "context-dna-ide feedback CLI requires `click`. Install with:\n"
        "  pip install click\n"
    )
    raise SystemExit(1) from exc

from .handler import (
    FeedbackError,
    FeedbackKind,
    FeedbackRecord,
    Severity,
    list_recent,
    record_feedback,
    stats,
)


_KIND_CHOICES = [k.value for k in FeedbackKind]
_SEVERITY_CHOICES = [s.value for s in Severity]


def _default_client_id() -> str:
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "operator"
    host = os.environ.get("MULTIFLEET_NODE_ID") or os.environ.get(
        "CONTEXTDNA_NODE_ID"
    ) or "local"
    return f"cli:{user}@{host}"


def _summarize_artifact(path: pathlib.Path) -> dict[str, Any]:
    info: dict[str, Any] = {"path": str(path)}
    try:
        stat = path.stat()
        info["size"] = stat.st_size
        sha = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                sha.update(chunk)
        info["sha256"] = sha.hexdigest()
    except OSError as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


@click.group(help="Operator feedback — write to the evidence ledger.")
def feedback() -> None:
    """Top-level group registered as ``context-dna-ide feedback`` in pyproject."""


@feedback.command("record", help="Record an operator feedback report.")
@click.argument("kind", type=click.Choice(_KIND_CHOICES))
@click.argument("title")
@click.option("--body", default="", help="Long-form description (markdown ok).")
@click.option(
    "--severity",
    type=click.Choice(_SEVERITY_CHOICES),
    default=Severity.INFO.value,
    show_default=True,
)
@click.option(
    "--attach",
    "attachments",
    multiple=True,
    type=click.Path(exists=False),
    help="File path(s) to capture as artifacts. Repeat for multiple.",
)
@click.option(
    "--client-id",
    default=None,
    help="Override the auto-generated client id (default: cli:$USER@$NODE).",
)
@click.option(
    "--stack-trace-file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Path to a file containing a pre-captured traceback.",
)
def cmd_record(  # noqa: PLR0913 — CLI fan-out is fine
    kind: str,
    title: str,
    body: str,
    severity: str,
    attachments: tuple[str, ...],
    client_id: str | None,
    stack_trace_file: str | None,
) -> None:
    """Write feedback. Exits non-zero only when the report is at risk."""
    artifacts = [_summarize_artifact(pathlib.Path(p)) for p in attachments]
    stack_trace: str | None = None
    if stack_trace_file:
        try:
            stack_trace = pathlib.Path(stack_trace_file).read_text(encoding="utf-8")
        except OSError as exc:
            click.echo(f"warn: failed to read stack trace file: {exc}", err=True)
    try:
        rec: FeedbackRecord = record_feedback(
            client_id=client_id or _default_client_id(),
            kind=kind,
            title=title,
            body=body,
            severity=severity,
            artifacts=artifacts,
            stack_trace=stack_trace,
        )
    except FeedbackError as exc:
        click.echo(f"FAIL: {exc}", err=True)
        sys.exit(3)
    click.echo(
        json.dumps(
            {
                "persisted_to": rec.persisted_to,
                "record_id": rec.record_id,
                "kind": rec.kind,
                "severity": rec.severity,
                "created_at": rec.created_at,
            },
            indent=2,
        )
    )
    sys.exit(0 if rec.persisted_to == "ledger" else 2)


@feedback.command("list", help="Show recent operator feedback rows.")
@click.option("--limit", type=int, default=20, show_default=True)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit raw JSON rather than the table format.",
)
def cmd_list(limit: int, as_json: bool) -> None:
    rows = list_recent(limit=limit)
    if as_json:
        click.echo(json.dumps(rows, indent=2, default=str))
        return
    if not rows:
        click.echo("(no feedback recorded yet)")
        return
    for row in rows:
        click.echo(
            f"[{row.get('created_at','?')}] "
            f"{row.get('severity','?'):8} "
            f"{row.get('kind','?'):20} "
            f"{row.get('title','(no title)')}  "
            f"<{row.get('client_id','?')}>"
        )


@feedback.command("health", help="Print the feedback ZSF counter snapshot.")
def cmd_health() -> None:
    click.echo(json.dumps(stats(), indent=2, sort_keys=True))


# Allow ``python -m services.feedback.cli ...`` for ad-hoc use without the
# pyproject script entry being installed yet (migrate2 is pre-package).
if __name__ == "__main__":  # pragma: no cover — exercised via subprocess
    feedback()
