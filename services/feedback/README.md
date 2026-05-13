# `services/feedback` — operator feedback handler

> Designed by Synaptic (2026-05-13) to close the adaptability gap: until now
> the mothership had no path for operator-driven friction to flow back into
> the evidence ledger. Without that loop the subconscious can only learn
> from its own autopsies — never from a human saying "this confused me".

## What it does

Captures user-reported friction (bugs, confusion, unmet expectations,
success stories, feature requests, panel requests) and writes each report
as an `EvidenceKind.AUDIT` row with `content.event_type =
"feedback.operator_reported"` in the evidence ledger
(`memory/evidence_ledger.py`, SQLite-backed at
`memory/evidence_ledger.db`).

Once a report is in the ledger it becomes available to:

- **S2 Professor Wisdom** — `confusion` and `unmet_expectation` rows surface
  as "operator was here before, watch out" warnings on the next prompt.
- **S3 Awareness** — `bug` rows with `severity = critical` get promoted to
  the awareness section until acknowledged.
- **Pattern promotion / retirement** — `success_story` rows reinforce the
  patterns active at the time; repeated `bug` rows against the same panel
  push that panel toward retirement.
- **`docs/plans/` generator** — `feature_request` and `panel_request` rows
  flow into the next planning cycle.

## Operator CLI

```bash
# Record a bug with an attached log
context-dna-ide feedback record bug "panel HF crashes on M1" \
    --body "S2 panel SIGABRT after 30s on cold cache; happens 1/3 times" \
    --severity critical \
    --attach /tmp/panic.log

# Quick success story (no body required)
context-dna-ide feedback record success_story "shift trades flow nailed it" \
    --body "first-try success after S2 wisdom update"

# Confusion report — the system worked but the operator couldn't tell
context-dna-ide feedback record confusion "can't find where panels register" \
    --body "spent 20 min grepping; expected panels/REGISTRY.md"

# Browse recent reports
context-dna-ide feedback list --limit 10
context-dna-ide feedback list --json | jq '.[] | select(.severity=="critical")'

# Inspect counters (also exposed via /health)
context-dna-ide feedback health
```

### Exit codes

| Code | Meaning |
|------|---------|
| 0    | Persisted to the evidence ledger. |
| 2    | Ledger unreachable; report appended to `/tmp/feedback-fallback.jsonl`. A later reaper will replay. |
| 3    | Both ledger AND fallback failed. The report is at risk — surface immediately. |

## Kind taxonomy

| Kind                 | When to use                                                         | Routes to                                |
|----------------------|---------------------------------------------------------------------|------------------------------------------|
| `bug`                | Something broke. Crash, hang, wrong output, data loss.              | Autopsy intake, S3 Awareness             |
| `confusion`          | Operator couldn't figure out how to do X.                           | S2 Professor Wisdom, doc revision        |
| `unmet_expectation`  | System worked as designed but the design is wrong.                  | S5 Protocol, SOP revision                |
| `success_story`      | A win worth celebrating. Reinforces the patterns that produced it.  | Pattern promotion                         |
| `feature_request`    | New capability wanted.                                              | `docs/plans/` planner                    |
| `panel_request`      | New IDE panel wanted.                                               | `contextdna-ide-oss` panel pipeline       |

## What gets captured

Every report includes:

- `client_id`, `kind`, `title`, `body`, `severity`, `created_at` (UTC, ISO-8601).
- `node_id` — from `MULTIFLEET_NODE_ID` / `CONTEXTDNA_NODE_ID`, else hostname.
- `mothership_version` — from `CONTEXTDNA_VERSION` env (or `"unknown"`).
- `profile` — from `CONTEXTDNA_PROFILE` env (`heavy` / `lite` / `"unknown"`).
- `active_panels` — from `CONTEXTDNA_ACTIVE_PANELS` env (comma-split).
- `artifacts` — file summaries (path, size, SHA-256) for every `--attach`.
- `stack_trace` — ONLY when the caller passed one. We do not synthesize
  tracebacks; operator feedback usually arrives after the fact and a
  synthetic trace would mislead the subconscious.
- `recent_logs` — last 100 lines of files in `logs/` modified within the
  past hour, automatically attached for `bug` / `confusion` /
  `unmet_expectation`. Skipped for positive kinds (operators reporting a
  win don't want their log noise serialized). Override with the
  `include_logs` kwarg if you're calling the handler directly.

## Programmatic use

```python
from services.feedback import record_feedback, FeedbackKind, Severity

rec = record_feedback(
    client_id="atlas@m1",
    kind=FeedbackKind.BUG,
    title="webhook S2 timeout under load",
    body="S2 Professor section >12s on cold cache; budget is 8s.",
    severity=Severity.WARNING,
    artifacts=[{"path": "logs/webhook.log", "size": 12345}],
)
print(rec.record_id, rec.persisted_to)
```

`record_feedback` returns a `FeedbackRecord` dataclass. Inspect
`persisted_to` — `"ledger"` means the row is in SQLite; `"fallback"`
means it landed in `/tmp/feedback-fallback.jsonl` and will be replayed by
a later reaper.

## ZSF (zero silent failures) design

- Every code path bumps a counter in `services.feedback.handler.COUNTERS`:
  - `feedback_records_total` — successful writes (ledger OR fallback).
  - `feedback_records_total_by_kind` / `..._by_severity` — per-bucket fan-out.
  - `feedback_record_errors_total` — ledger write failures (fallback then ran).
  - `feedback_fallback_writes_total` — successful fallback appends.
  - `feedback_fallback_errors_total` — fallback also failed (true silent-loss risk).
- The handler **never** uses `except Exception: pass`. Every caught
  exception bumps a counter, logs at WARNING, and either runs the fallback
  or re-raises as `FeedbackError`.
- The fallback file at `/tmp/feedback-fallback.jsonl` is JSONL (one record
  per line) so a replayer can stream-process it without loading the whole
  file. Each line is the full payload plus a `_ledger_error` field naming
  the original failure.
- Counters are exposed via `services.feedback.stats()` and should be
  scraped into `/health.feedback`. Daemon owners — wire that snapshot into
  the same endpoint that surfaces `webhook_publish_errors`.

## How the subconscious uses feedback

Per the next promotion cycle (default: hourly via the gains-gate), the
session historian queries the ledger for rows with `kind = audit` and
`content.event_type = "feedback.operator_reported"`. The promoter then:

1. Bins reports by `(kind, severity, node_id)`.
2. For `severity = critical` + `kind = bug`: writes a critical finding
   (`memory.session_gold_passes.get_critical_findings`) so the next
   webhook injection raises it under S3 Awareness.
3. For `kind = success_story`: scans the active patterns at
   `created_at` and bumps their confidence weight in `memory/brain.py`.
4. For `kind = confusion`: appends to the S2 Professor Wisdom queue so
   the next prompt warns about the same path.
5. For `kind in {feature_request, panel_request}`: writes a draft
   into `docs/plans/inbox/` for the next planning cycle.

The full promotion pipeline lives in `memory/session_gold_passes.py`. This
module is intentionally write-only — the promoter owns the read side.

## Tests

Place tests under `migrate2/services/feedback/tests/` (or wire into the
repo-wide `tests/` tree once migrate2 graduates). Suggested coverage:

- `record_feedback` returns a `FeedbackRecord` with `persisted_to="ledger"`
  on the happy path.
- Forcing `memory.evidence_ledger` import to fail (via `monkeypatch`) yields
  `persisted_to="fallback"` and a non-empty JSONL line.
- A simulated `OSError` on the fallback path raises `FeedbackError` and
  bumps `feedback_fallback_errors_total`.
- Each `FeedbackKind` and `Severity` value coerces from its string form;
  unknown strings raise `ValueError` (not silent default-to-INFO).
- `MAX_BODY_BYTES` overrun is truncated with the `…truncated N bytes`
  marker; the marker is verifiable in the stored payload.
