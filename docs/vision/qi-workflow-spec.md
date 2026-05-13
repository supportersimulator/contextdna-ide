# Quality Improvement (QI) Workflow — ER Simulator

**Issue**: #12 (parent: #4 Case Factory)
**Status**: Spec — 2026-05-12

---

## 1. Purpose

Close the loop between learner performance and case content. Every case attempt
produces structured feedback data. That data rolls up into aggregate metrics,
surfaces gaps to educators, and drives Case Factory iterations.

---

## 2. QI Cycle

```
Case attempt (learner)
        │
        ▼
Feedback event captured (JSON schema — see qi-feedback-schema.json)
        │
        ▼
Django backend stores event → Postgres `case_feedback` table
        │
        ├──► Real-time: immediate or debrief feedback shown to learner
        │
        ▼
Nightly aggregate job (per-case / per-learner / per-institution roll-up)
        │
        ▼
Google Sheets QI dashboard (auto-exported via Apps Script)
        │
        ▼
Educator reviews → Case Factory revision tickets (GitHub issues, label: qi-revision)
        │
        ▼
Updated case content deployed → cycle restarts
```

---

## 3. Feedback Event Schema

Full JSON Schema (draft-07) lives in `docs/vision/qi-feedback-schema.json`.

### 3.1 Top-level fields

| Field | Type | Required | Description |
|---|---|---|---|
| `schema_version` | string | yes | Semver, e.g. `"1.0.0"` |
| `case_id` | string | yes | Stable case slug, e.g. `"stemi-anterior-v2"` |
| `learner_id` | string | yes | Opaque UUID for the learner |
| `institution_id` | string | yes | Opaque UUID for the institution |
| `session_id` | string | yes | UUID per simulator launch |
| `started_at` | string (ISO 8601) | yes | UTC timestamp |
| `completed_at` | string (ISO 8601) | yes | UTC timestamp |
| `mode` | enum | yes | `"training"` \| `"assessment"` \| `"debrief"` |
| `actions` | array | yes | Ordered list of learner actions (see 3.2) |
| `vitals_checks` | integer | yes | Count of times learner reviewed vitals panel |
| `diagnosis_attempts` | array | yes | Ordered list of diagnosis guesses (see 3.3) |
| `final_diagnosis` | string | yes | Last submitted diagnosis string |
| `outcome` | enum | yes | `"correct"` \| `"partial"` \| `"incorrect"` |
| `score_pct` | number | yes | 0–100 computed score |
| `time_to_key_action_s` | integer | yes | Seconds from start to first critical action |
| `time_to_diagnosis_s` | integer | yes | Seconds from start to first diagnosis attempt |
| `feedback_shown` | array | yes | Feedback items displayed to learner (see 3.4) |
| `flags` | array | no | QI flag codes, e.g. `["missed_ecg","delayed_aspirin"]` |

### 3.2 Action object

```json
{
  "seq": 1,
  "t_s": 42,
  "action_type": "order_medication",
  "detail": "aspirin 325mg",
  "correct": true,
  "critical": true
}
```

`action_type` values: `order_medication`, `order_imaging`, `order_lab`,
`perform_procedure`, `consult`, `reassess_vitals`, `administer_treatment`,
`transfer`, `other`.

### 3.3 Diagnosis attempt object

```json
{
  "seq": 1,
  "t_s": 180,
  "value": "STEMI",
  "confidence": "high",
  "correct": true
}
```

`confidence` values: `"low"` | `"medium"` | `"high"`.

### 3.4 Feedback item object

```json
{
  "id": "fb-missed-ecg",
  "category": "critical_miss",
  "message": "ECG was not ordered within the first 10 minutes.",
  "shown_at": "debrief"
}
```

`category` values: `"correct_action"`, `"critical_miss"`, `"suboptimal_order"`,
`"timing_concern"`, `"diagnosis_error"`.
`shown_at` values: `"immediate"` | `"debrief"` | `"both"`.

---

## 4. Aggregate Metrics

### 4.1 Per-case

| Metric | Description |
|---|---|
| `attempt_count` | Total attempts |
| `correct_rate_pct` | % outcome = correct |
| `partial_rate_pct` | % outcome = partial |
| `median_time_to_key_action_s` | Median across all attempts |
| `p90_time_to_diagnosis_s` | 90th-percentile time to first diagnosis |
| `common_flags` | Top-5 flag codes by frequency |
| `avg_score_pct` | Mean score |
| `discrimination_index` | Δ score (top quartile − bottom quartile) |

### 4.2 Per-learner

| Metric | Description |
|---|---|
| `cases_attempted` | Distinct cases tried |
| `cases_mastered` | Cases with outcome=correct on any attempt |
| `avg_score_pct` | Mean score across all cases |
| `improvement_slope` | Linear regression slope of score vs attempt number |
| `top_flag_codes` | Most frequent personal QI flags |

### 4.3 Per-institution

| Metric | Description |
|---|---|
| `active_learners` | Learners with ≥1 attempt in the period |
| `case_coverage_pct` | % of published cases attempted by ≥1 learner |
| `cohort_avg_score_pct` | Mean score across all learners |
| `hardest_cases` | Top-5 cases by lowest correct_rate |
| `qi_revision_backlog` | Cases with common_flag frequency > threshold |

---

## 5. Feedback Display Rules

### 5.1 Immediate mode

Triggered when `mode == "training"`.

- Critical misses surface within 5 seconds of the missed window (e.g., no ECG by
  minute 10 → flag appears at minute 10).
- Correct critical actions show a brief confirmation toast (≤2 seconds, non-blocking).
- No diagnosis spoilers until the learner submits a guess.

### 5.2 Debrief mode

Triggered when `mode == "assessment"` or learner opts into debrief.

- All feedback items shown after `completed_at`.
- Ordered: diagnosis verdict → critical misses → suboptimal orders → timing concerns →
  correct actions.
- Each item links to the relevant case reference (guideline, SOP).
- Learner can drill into action-by-action timeline replay.

### 5.3 Suppression rules

- Feedback suppressed during active resuscitation sequences (CPR, intubation) to
  avoid cognitive overload.
- Rate limit: no more than 3 immediate feedback toasts per 60-second window.

---

## 6. Integration Points

### 6.1 Django backend

- Model: `CaseFeedbackEvent` (maps 1:1 to feedback event JSON).
- API endpoint: `POST /api/v1/feedback/events/` — accepts feedback event JSON,
  validates against schema, stores to Postgres, queues aggregate refresh.
- Aggregate job: Celery beat task `compute_case_aggregates`, runs nightly at 02:00 UTC.
- Admin view: `/admin/feedback/casefeedbackevent/` with filters by case, institution,
  date range.

### 6.2 Google Sheets QI Dashboard

- Apps Script export job: reads aggregate rows from Django REST endpoint
  (`GET /api/v1/feedback/aggregates/?period=30d`), writes to a protected sheet.
- Sheets tabs: `Case Aggregates` | `Learner Progress` | `Institution Summary` |
  `QI Flag Heatmap`.
- Refresh cadence: daily, triggered by Apps Script time-based trigger.

### 6.3 TrialBench

- Feedback events tagged with `mode == "assessment"` are eligible for TrialBench
  run artifacts. The `score_pct` and `outcome` fields map directly to
  `task_complete` and endpoint scoring in `tools/trialbench_score.py`.
- Run artifact path: `artifacts/trialbench/<trial_id>/run_<session_id>.json`.
- TrialBench arm assignment is injected at session start; the feedback event
  carries it in an optional `trialbench_arm` field.

### 6.4 Case Factory (issue #4)

- When `common_flags` for a case exceeds a QI revision threshold (default: flag
  appears in > 30% of attempts), an automated GitHub issue is opened with label
  `qi-revision`, referencing the case slug and the top flag codes.
- Case Factory authors review, update case YAML/JSON, bump case version, and close
  the QI revision issue.

---

## 7. Schema Versioning

- Schema version embedded in every event (`schema_version`).
- Breaking changes require a major version bump and a Django migration.
- Non-breaking additions (new optional fields) bump the minor version only.
- Old events are stored as-is; the aggregate job handles version differences via
  field-presence checks.

---

## 8. Privacy and De-identification

- `learner_id` and `institution_id` are opaque UUIDs; PII never enters the event.
- Events older than 7 years are archived to cold storage and purged from hot DB.
- Aggregate exports to Google Sheets contain no individual learner identifiers.
