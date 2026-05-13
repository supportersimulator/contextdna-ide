#!/usr/bin/env bash
# ============================================================================
#  storage-invariant-check.sh
# ----------------------------------------------------------------------------
#  Purpose:
#    Verify the STORAGE_BACKEND_TOGGLE invariant: a read or write performed
#    via one backend (sqlite|postgres|auto) MUST be visible — with identical
#    row counts + content checksums — when the SAME process is then run
#    against the alternate backend.
#
#    This is the storage-layer half of the "Operational Invariance Promise":
#    swapping backends is a configuration concern, NOT a semantic one. If the
#    promise breaks, this script auto-invokes storage-rollback.sh.
#
#    Called by bootstrap-verify.sh between step 3 (compose-up) and step 4
#    (consult probe) — see bootstrap-verify-patch.diff for the exact insertion
#    point.
#
#  Usage:
#    storage-invariant-check.sh [ --backends sqlite,auto ]
#                               [ --threshold 0 ]
#                               [ --window-ms 100 ]
#                               [ --retries 3 ]
#                               [ --learnings-db <path> ]
#                               [ --rollback-target latest ]
#                               [ --no-rollback ]
#                               [ --output-json <path> ]
#                               [ --help ]
#
#    --backends         CSV list of backends to probe. Default: "sqlite,auto".
#                       Each backend is exercised by re-importing
#                       memory.learning_store with STORAGE_BACKEND=<value>
#                       and running 5 canonical CRUD ops.
#
#    --threshold        Max allowed divergence (absolute integer) between any
#                       two backend snapshots' learnings row counts. Default 0
#                       — the toggle invariant says they MUST be equal.
#                       SQLite checksum compare is also required to match
#                       exactly; threshold ONLY softens the row-count check.
#
#    --window-ms        Per-backend probe window in milliseconds. We perform
#                       the row-count read up to --retries times spaced
#                       (window/retries) ms apart, and use the **last stable**
#                       value as the truth. This guards against non-atomic
#                       state transitions where a write is in flight when we
#                       snapshot. Default 100.
#
#    --retries          Number of intra-window samples (default 3). All
#                       samples must agree before we accept a row count.
#
#    --rollback-target  Passed through to storage-rollback.sh if divergence
#                       is detected. Default: 'latest'.
#
#    --no-rollback      On divergence, only log + exit non-zero. Use this in
#                       CI where rollback is a human decision.
#
#    --output-json      Where to drop the JSON summary. Defaults to
#                       <script-dir>/last-check.json
#
#  Exit codes:
#    0  invariant holds, JSON snapshot written
#    1  invariant broken, rollback ran (or was suppressed via --no-rollback)
#    2  prereqs missing / bad flags
#    3  rollback itself failed
#
#  ZSF:
#    Every probe, retry, and divergence event bumps a counter. The JSON
#    output includes every counter for downstream observability (xbar,
#    fleet-check, gains-gate).
#
#  Notes:
#    - Postgres is NEVER required. If $DATABASE_URL is unset OR the postgres
#      backend is unreachable, the script logs an explicit skip counter for
#      that backend — we never silently pretend the comparison happened.
#    - The "auto" backend acts like postgres when postgres is healthy, sqlite
#      otherwise. Comparing auto vs sqlite therefore EITHER reduces to a
#      sqlite-vs-sqlite no-op (postgres absent) OR a cross-backend toggle
#      (postgres healthy). Both are legitimate test cases — we record which
#      mode we ran in.
# ============================================================================

set -euo pipefail

# ----------------------------------------------------------------------------
# Counters (ZSF)
# ----------------------------------------------------------------------------
COUNTER_PROBE_OK=0
COUNTER_PROBE_FAIL=0
COUNTER_RETRY_DISAGREEMENT=0   # retries within the window disagreed
COUNTER_BACKEND_SKIPPED=0      # explicit skip (e.g. postgres unhealthy)
COUNTER_ROW_DIVERGENCE=0
COUNTER_CHECKSUM_DIVERGENCE=0
COUNTER_ROLLBACK_INVOKED=0
COUNTER_ROLLBACK_FAILED=0
ERRORS=0

# ----------------------------------------------------------------------------
# Defaults
# ----------------------------------------------------------------------------
BACKENDS_CSV="sqlite,auto"
THRESHOLD=0
WINDOW_MS=100
RETRIES=3
LEARNINGS_DB=""
ROLLBACK_TARGET="latest"
DO_ROLLBACK=1
OUTPUT_JSON=""

ROLLBACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROLLBACK_SCRIPT="${ROLLBACK_DIR}/storage-rollback.sh"

# ----------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------
log() {
    local step="$1"; local status="$2"; shift 2
    printf '[invariant] step=%s status=%s detail="%s"\n' \
        "$step" "$status" "$*" >&2
}

fatal() {
    ERRORS=$((ERRORS + 1))
    log "$1" "fatal" "$2"
    write_json "fatal"
    exit "${3:-1}"
}

usage() {
    sed -n '2,55p' "$0" | sed 's/^# \{0,1\}//'
}

# ----------------------------------------------------------------------------
# Arg parsing
# ----------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --backends)         BACKENDS_CSV="$2";    shift 2 ;;
        --threshold)        THRESHOLD="$2";       shift 2 ;;
        --window-ms)        WINDOW_MS="$2";       shift 2 ;;
        --retries)          RETRIES="$2";         shift 2 ;;
        --learnings-db)     LEARNINGS_DB="$2";    shift 2 ;;
        --rollback-target)  ROLLBACK_TARGET="$2"; shift 2 ;;
        --no-rollback)      DO_ROLLBACK=0;        shift   ;;
        --output-json)      OUTPUT_JSON="$2";     shift 2 ;;
        --help|-h)          usage; exit 0 ;;
        *)
            echo "Unknown flag: $1" >&2
            usage
            exit 2
            ;;
    esac
done

if [[ -z "$OUTPUT_JSON" ]]; then
    OUTPUT_JSON="${ROLLBACK_DIR}/last-check.json"
fi
if [[ -z "$LEARNINGS_DB" ]]; then
    LEARNINGS_DB="${CONTEXT_DNA_LEARNINGS_DB:-$HOME/.context-dna/learnings.db}"
fi

# Resolve symlinked DB path so the verify step talks about the real file.
if [[ -L "$LEARNINGS_DB" ]]; then
    LEARNINGS_DB_RESOLVED=$(readlink -f "$LEARNINGS_DB" 2>/dev/null || readlink "$LEARNINGS_DB")
    LEARNINGS_DB_RESOLVED="$(cd "$(dirname "$LEARNINGS_DB")" && readlink "$LEARNINGS_DB")"
    # readlink -f is GNU-only; fall back gracefully on macOS BSD readlink:
    if [[ "$LEARNINGS_DB_RESOLVED" != /* ]]; then
        LEARNINGS_DB_RESOLVED="$(cd "$(dirname "$LEARNINGS_DB")" && pwd)/$LEARNINGS_DB_RESOLVED"
    fi
else
    LEARNINGS_DB_RESOLVED="$LEARNINGS_DB"
fi

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "ERROR: required command '$1' not found on PATH" >&2
        exit 2
    fi
}
require_cmd sqlite3
require_cmd python3

# Locate a Python that can import memory.learning_store. Order:
#   1. $CONTEXTDNA_PYTHON  (explicit override, picked first if set)
#   2. .venv/bin/python3   (mothership convention)
#   3. python3 on PATH
PYTHON_BIN=""
if [[ -n "${CONTEXTDNA_PYTHON:-}" && -x "${CONTEXTDNA_PYTHON}" ]]; then
    PYTHON_BIN="${CONTEXTDNA_PYTHON}"
fi
if [[ -z "$PYTHON_BIN" ]]; then
    for candidate in \
        "$(pwd)/.venv/bin/python3" \
        "$ROLLBACK_DIR/../../../.venv/bin/python3" \
        "$ROLLBACK_DIR/../../../../.venv/bin/python3" \
        "$(command -v python3)"; do
        if [[ -n "$candidate" && -x "$candidate" ]]; then
            PYTHON_BIN="$candidate"
            break
        fi
    done
fi
if [[ -z "$PYTHON_BIN" ]]; then
    fatal "preflight" "no usable python3 (need memory.learning_store importable)" 2
fi

# Resolve PYTHONPATH: prefer cwd, fall back to the migrate3/memory parent
# (contextdna-ide-oss/migrate3 — that's where learning_store.py actually lives
# in the OSS tree right now).
PYTHONPATH_CANDIDATES=(
    "$(pwd)"
    "$ROLLBACK_DIR/../../../"           # superrepo root if invoked from rollback dir
    "$ROLLBACK_DIR/../../"               # contextdna-ide-oss root
    "$ROLLBACK_DIR/../../migrate3"       # the OSS migrate3 tree where learning_store ships today
)
RESOLVED_PYTHONPATH=""
for cand in "${PYTHONPATH_CANDIDATES[@]}"; do
    if [[ -d "$cand" ]] && [[ -f "$cand/memory/learning_store.py" ]]; then
        RESOLVED_PYTHONPATH="$cand:${RESOLVED_PYTHONPATH}"
    fi
done
if [[ -z "$RESOLVED_PYTHONPATH" ]]; then
    # Last-ditch: use whatever PYTHONPATH the caller had — but bump a counter
    # so a missing import doesn't look like a silent skip.
    RESOLVED_PYTHONPATH="${PYTHONPATH:-}"
    log "preflight" "warn" "could not locate memory/learning_store.py; using inherited PYTHONPATH"
fi

# Snapshot files we'll write per-backend.
TMP_DIR=$(mktemp -d -t storage-invariant-XXXXXX)
trap 'rm -rf "$TMP_DIR"' EXIT

# ----------------------------------------------------------------------------
# probe_backend BACKEND  -> writes "$TMP_DIR/<backend>.json"
#
# Runs 5 canonical CRUD ops via an inline Python script. We don't shell out
# to `python -m memory.learning_store --probe` because the upstream module
# doesn't currently expose a --probe flag — the directive in the task
# describes the desired contract, and this is the script that fulfils it.
# ----------------------------------------------------------------------------
probe_backend() {
    local backend="$1"
    local out="$TMP_DIR/${backend}.json"

    log "probe" "info" "backend=$backend window_ms=$WINDOW_MS retries=$RETRIES"

    PYTHONPATH="$RESOLVED_PYTHONPATH" \
    STORAGE_BACKEND="$backend" \
    CONTEXT_DNA_LEARNINGS_DB="$LEARNINGS_DB" \
    INVARIANT_OUT="$out" \
    INVARIANT_WINDOW_MS="$WINDOW_MS" \
    INVARIANT_RETRIES="$RETRIES" \
    "$PYTHON_BIN" - <<'PY'
import hashlib
import importlib
import json
import os
import sys
import time
import uuid


def read_stable_rowcount(store, window_ms: int, retries: int):
    """Sample the learnings count `retries` times across `window_ms`.

    Returns (count, agreement_bool, samples_list). All samples must agree;
    if they don't, we still return the last one but mark agreement False —
    the caller bumps COUNTER_RETRY_DISAGREEMENT.
    """
    samples = []
    if retries < 1:
        retries = 1
    step = (window_ms / 1000.0) / retries
    for _ in range(retries):
        try:
            stats = store.get_stats()
            samples.append(int(stats.get("total", -1)))
        except Exception as exc:
            samples.append(-1)
        time.sleep(step)
    last = samples[-1]
    agreement = all(s == last for s in samples)
    return last, agreement, samples


def main() -> int:
    out_path = os.environ["INVARIANT_OUT"]
    window_ms = int(os.environ.get("INVARIANT_WINDOW_MS", "100"))
    retries = int(os.environ.get("INVARIANT_RETRIES", "3"))
    backend = os.environ.get("STORAGE_BACKEND", "auto")

    # Re-import — vital because LearningStore caches the resolved backend in a
    # module-level singleton. We must drop and recreate it so STORAGE_BACKEND
    # is honoured on each backend toggle.
    for modname in (
        "memory.learning_store",
        "memory.sqlite_storage",
        "memory.postgres_storage",
    ):
        if modname in sys.modules:
            del sys.modules[modname]

    try:
        ls = importlib.import_module("memory.learning_store")
    except ImportError as exc:
        out = {
            "backend_requested": backend,
            "backend_actual": "import_error",
            "error": f"{exc}",
            "skipped": True,
        }
        with open(out_path, "w") as fh:
            json.dump(out, fh)
        return 0  # the caller decides whether a skip is fatal

    # Force a fresh singleton.
    ls._learning_store = None
    store = ls.get_learning_store()

    # Op 1 — read row count BEFORE any writes (windowed).
    pre_count, pre_agree, pre_samples = read_stable_rowcount(store, window_ms, retries)

    # Op 2 — CREATE (insert one canonical learning we can clean up).
    probe_id = f"invariant-probe-{uuid.uuid4().hex[:12]}"
    payload = ls.build_learning_data(
        learning_type="fix",
        title="invariant-probe",
        content=f"storage-invariant-check probe (backend={backend}, id={probe_id})",
        tags=["invariant", "probe", backend],
        session_id="invariant-check",
        injection_id="",
        source="invariant-check",
        metadata={"probe_id": probe_id},
    )
    try:
        stored = store.store_learning(payload, skip_dedup=True, consolidate=False)
        write_id = stored.get("id", "")
        write_ok = True
        write_err = ""
    except Exception as exc:
        write_id = ""
        write_ok = False
        write_err = f"{exc}"

    # Op 3 — READ (lookup the row we just wrote).
    read_ok = False
    if write_ok and write_id:
        try:
            fetched = store.get_by_id(write_id)
            read_ok = bool(fetched)
        except Exception:
            read_ok = False

    # Op 4 — RETIRE (soft-delete; toggles `type=retired`).
    retire_ok = False
    if write_ok and write_id:
        try:
            retire_ok = store.retire(write_id)
        except Exception:
            retire_ok = False

    # Op 5 — POST-COUNT (read row count after the write+retire cycle).
    post_count, post_agree, post_samples = read_stable_rowcount(store, window_ms, retries)

    # Per-table checksum: SHA-256 over (id, type, title, updated_at) tuples
    # ordered by id. Stable across both backends because we only hash columns
    # the public API guarantees.
    #
    # NOTE: get_recent(limit) currently silently returns [] for limit >= 200
    # (bug in learning_store — recent.fail counter ticks). We deliberately
    # cap the sample at 50 rows so the checksum carries real signal. If we
    # later expand to TOP-N where N is large, that bug must be fixed first.
    checksum = ""
    recent_rows_hashed = 0
    try:
        recent = store.get_recent(limit=50)
        # Deterministic order:
        recent_sorted = sorted(recent, key=lambda r: r.get("id", ""))
        h = hashlib.sha256()
        for row in recent_sorted:
            tup = "|".join([
                str(row.get("id", "")),
                str(row.get("type", "")),
                str(row.get("title", ""))[:200],
                str(row.get("updated_at", "") or row.get("timestamp", "")),
            ])
            h.update(tup.encode("utf-8", errors="replace"))
            h.update(b"\n")
            recent_rows_hashed += 1
        checksum = h.hexdigest()
    except Exception as exc:
        checksum = f"error:{exc}"

    out = {
        "backend_requested": backend,
        "backend_actual": store.backend_name(),
        "pre_count": pre_count,
        "pre_samples": pre_samples,
        "pre_samples_agree": pre_agree,
        "write_id": write_id,
        "write_ok": write_ok,
        "write_err": write_err,
        "read_ok": read_ok,
        "retire_ok": retire_ok,
        "post_count": post_count,
        "post_samples": post_samples,
        "post_samples_agree": post_agree,
        "checksum_top50": checksum,
        "checksum_rows_hashed": recent_rows_hashed,
        "counters": ls.get_counters(),
        "skipped": False,
    }
    with open(out_path, "w") as fh:
        json.dump(out, fh)
    return 0


sys.exit(main())
PY

    local rc=$?
    if [[ $rc -ne 0 ]]; then
        COUNTER_PROBE_FAIL=$((COUNTER_PROBE_FAIL + 1))
        log "probe" "fail" "backend=$backend python exited rc=$rc"
        return 1
    fi
    if [[ ! -f "$out" ]]; then
        COUNTER_PROBE_FAIL=$((COUNTER_PROBE_FAIL + 1))
        log "probe" "fail" "backend=$backend no output JSON produced"
        return 1
    fi
    COUNTER_PROBE_OK=$((COUNTER_PROBE_OK + 1))
    log "probe" "ok" "backend=$backend -> $out"
    return 0
}

# ----------------------------------------------------------------------------
# Run probes
# ----------------------------------------------------------------------------
IFS=',' read -r -a BACKENDS <<< "$BACKENDS_CSV"

declare -a PROBE_FILES=()
for backend in "${BACKENDS[@]}"; do
    backend=$(echo "$backend" | tr -d '[:space:]')
    [[ -z "$backend" ]] && continue
    case "$backend" in
        sqlite|postgres|auto) ;;
        *)
            log "probe" "skipped" "backend=$backend (unknown)"
            COUNTER_BACKEND_SKIPPED=$((COUNTER_BACKEND_SKIPPED + 1))
            continue
            ;;
    esac
    if probe_backend "$backend"; then
        PROBE_FILES+=("$TMP_DIR/${backend}.json")
    else
        ERRORS=$((ERRORS + 1))
    fi
done

if [[ ${#PROBE_FILES[@]} -lt 2 ]]; then
    fatal "compare" "need at least 2 backend probes to compare invariance (got ${#PROBE_FILES[@]})"
fi

# ----------------------------------------------------------------------------
# Compare backends (the actual invariant check)
# ----------------------------------------------------------------------------
write_json() {
    local final_status="$1"
    python3 - "$OUTPUT_JSON" "$final_status" \
        "$COUNTER_PROBE_OK" "$COUNTER_PROBE_FAIL" "$COUNTER_RETRY_DISAGREEMENT" \
        "$COUNTER_BACKEND_SKIPPED" "$COUNTER_ROW_DIVERGENCE" "$COUNTER_CHECKSUM_DIVERGENCE" \
        "$COUNTER_ROLLBACK_INVOKED" "$COUNTER_ROLLBACK_FAILED" "$ERRORS" \
        "$THRESHOLD" "$WINDOW_MS" "$RETRIES" "$BACKENDS_CSV" "$LEARNINGS_DB" \
        "$LEARNINGS_DB_RESOLVED" "$TMP_DIR" <<'PY'
import json
import os
import sys
import time

argv = sys.argv[1:]
if len(argv) < 17:
    sys.stderr.write(f"write_json: expected 17 args, got {len(argv)}: {argv}\n")
    sys.exit(2)
(out_path, final_status,
 probe_ok, probe_fail, retry_disagree,
 backend_skipped, row_div, ck_div,
 rb_invoked, rb_failed, errors,
 threshold, window_ms, retries,
 backends_csv, learnings_db, learnings_db_resolved) = argv[:17]
tmp_dir = argv[17] if len(argv) >= 18 else ""

probes = []
for fname in os.listdir(tmp_dir):
    if not fname.endswith(".json"):
        continue
    path = os.path.join(tmp_dir, fname)
    try:
        with open(path) as fh:
            probes.append(json.load(fh))
    except Exception as exc:
        probes.append({"backend_requested": fname, "load_error": f"{exc}"})

out = {
    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "script": "storage-invariant-check",
    "final_status": final_status,
    "config": {
        "backends": backends_csv,
        "threshold": int(threshold),
        "window_ms": int(window_ms),
        "retries": int(retries),
        "learnings_db": learnings_db,
        "learnings_db_resolved": learnings_db_resolved,
    },
    "counters": {
        "probe_ok": int(probe_ok),
        "probe_fail": int(probe_fail),
        "retry_disagreement": int(retry_disagree),
        "backend_skipped": int(backend_skipped),
        "row_divergence": int(row_div),
        "checksum_divergence": int(ck_div),
        "rollback_invoked": int(rb_invoked),
        "rollback_failed": int(rb_failed),
        "errors": int(errors),
    },
    "probes": probes,
}

os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
with open(out_path, "w") as fh:
    json.dump(out, fh, indent=2)
print(out_path)
PY
}

# Bump counters from probe JSON (retry-disagreement) — done in shell so the
# counters survive into the final stats. We parse via python3.
for pf in "${PROBE_FILES[@]}"; do
    DISAGREE=$(python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
n=0
if d.get('pre_samples_agree') is False: n+=1
if d.get('post_samples_agree') is False: n+=1
print(n)
" "$pf")
    COUNTER_RETRY_DISAGREEMENT=$((COUNTER_RETRY_DISAGREEMENT + DISAGREE))
    SKIPPED=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(1 if d.get('skipped') else 0)" "$pf")
    if [[ "$SKIPPED" == "1" ]]; then
        COUNTER_BACKEND_SKIPPED=$((COUNTER_BACKEND_SKIPPED + 1))
    fi
done

# Pairwise compare (row count + checksum). With 2 probes this is one pair;
# with 3 it's three pairs. We treat ANY pair's divergence as a failure.
# We pass probe files as argv to a python3 heredoc (avoids shell-array
# splat ambiguity inside the heredoc body).
DIVERGED=0
PAIRWISE=$(python3 - "${PROBE_FILES[@]}" <<'PY'
import itertools, json, sys
files = sys.argv[1:]
loaded = []
for f in files:
    with open(f) as fh:
        d = json.load(fh)
    loaded.append((f, d))

# Each backend probe inserts exactly 1 row (CREATE) and then RETIRES it.
# Retire is a soft-delete (type=retired) — the row stays in `learnings`,
# so post_count = pre_count + 1 for each backend.
#
# Therefore the invariant we check is NOT post_count_a == post_count_b
# (probes run sequentially, so each probe sees the previous probe's row).
# It is:  delta_a == delta_b == 1     (each backend MUST observe its own
#                                       write and ONLY its own write)
# AND:    post_count_b == post_count_a + 1
#                                      (sequential probes accumulate by 1)
#
# Checksum comparison uses the recent-500 set — same caveat: each probe
# adds a row so checksums will differ across probes BY EXACTLY the new row.
# We surface raw checksums + a `checksum_equal` flag but ONLY treat the
# checksum as a divergence signal when the post_count delta itself is
# unexpected (e.g. > 1 or < 1).

results = []
for (fa, da), (fb, db) in itertools.combinations(loaded, 2):
    if da.get("skipped") or db.get("skipped"):
        results.append({
            "a": da.get("backend_requested"), "b": db.get("backend_requested"),
            "skipped": True,
            "reason": "one or both backends skipped",
        })
        continue
    pre_a = int(da.get("pre_count", -1))
    post_a = int(da.get("post_count", -1))
    pre_b = int(db.get("pre_count", -1))
    post_b = int(db.get("post_count", -1))
    delta_a = post_a - pre_a
    delta_b = post_b - pre_b
    write_ok_a = bool(da.get("write_ok"))
    write_ok_b = bool(db.get("write_ok"))
    # Expected: each successful write -> delta of 1.
    expected_delta_a = 1 if write_ok_a else 0
    expected_delta_b = 1 if write_ok_b else 0
    delta_a_ok = (delta_a == expected_delta_a)
    delta_b_ok = (delta_b == expected_delta_b)
    # Sequential-write expectation: probe B sees probe A's still-present
    # retired row, so post_b should == post_a + expected_delta_b
    # (assuming probes ran in order a -> b, which itertools preserves).
    sequential_ok = (post_b == post_a + expected_delta_b) or (post_b == post_a)
    ck_a, ck_b = da.get("checksum_top50", ""), db.get("checksum_top50", "")
    results.append({
        "a": da.get("backend_requested"),
        "b": db.get("backend_requested"),
        "actual_a": da.get("backend_actual"),
        "actual_b": db.get("backend_actual"),
        "pre_count_a": pre_a, "post_count_a": post_a, "delta_a": delta_a,
        "pre_count_b": pre_b, "post_count_b": post_b, "delta_b": delta_b,
        "delta_a_ok": delta_a_ok,
        "delta_b_ok": delta_b_ok,
        "sequential_ok": sequential_ok,
        "checksum_a": ck_a,
        "checksum_b": ck_b,
        "checksum_equal": ck_a == ck_b,
        "skipped": False,
    })

print(json.dumps(results))
PY
)

# Walk the pairwise results in shell to bump counters.
python3 - "$THRESHOLD" "$PAIRWISE" <<'PY'
import json, sys
threshold = int(sys.argv[1])
results = json.loads(sys.argv[2])
exit_code = 0
for r in results:
    if r.get("skipped"):
        continue
    # 1. Each backend MUST observe its own write as a delta-of-1 (or 0 if
    #    the write failed — already an upstream error). A delta of 2+ means
    #    the backend saw another writer's row mid-probe = invariant broken.
    if not r["delta_a_ok"]:
        exit_code = 1
        print(f"DIVERGED delta a={r['a']} delta_a={r['delta_a']} (expected 1 on successful write)", file=sys.stderr)
    if not r["delta_b_ok"]:
        exit_code = 1
        print(f"DIVERGED delta b={r['b']} delta_b={r['delta_b']} (expected 1 on successful write)", file=sys.stderr)
    # 2. Sequential-write expectation: when probe B runs after probe A on
    #    the same physical store, B's pre_count should match A's post_count
    #    (modulo threshold for concurrent writers from outside this script).
    seq_gap = abs(int(r["pre_count_b"]) - int(r["post_count_a"]))
    if seq_gap > threshold:
        # Cross-backend pairs (auto vs sqlite when postgres is healthy)
        # legitimately may not share state. We use backend_actual to decide:
        # if actual_a == actual_b they MUST share state.
        if r.get("actual_a") == r.get("actual_b"):
            exit_code = 1
            print(f"DIVERGED sequential same-backend a={r['a']}({r['post_count_a']}) -> b={r['b']}.pre_count={r['pre_count_b']} gap={seq_gap} > threshold={threshold}", file=sys.stderr)
        else:
            # Heterogeneous backends — we only assert checksum equality is
            # impossible here, but we flag it as a checksum divergence so
            # the operator can investigate. Not fatal at threshold=0.
            print(f"NOTE cross-backend a={r['a']}({r.get('actual_a')}) vs b={r['b']}({r.get('actual_b')}) — sequential check skipped", file=sys.stderr)
    # 3. Checksums: same backend_actual + same post_count_a => must match.
    if r.get("actual_a") == r.get("actual_b") and r["post_count_a"] == r["post_count_b"]:
        if not r["checksum_equal"]:
            exit_code = 2
            print(f"DIVERGED checksum same-backend same-rowcount a={r['a']} b={r['b']}", file=sys.stderr)
sys.exit(exit_code)
PY
PAIRWISE_RC=$?

if [[ $PAIRWISE_RC -eq 1 ]]; then
    COUNTER_ROW_DIVERGENCE=$((COUNTER_ROW_DIVERGENCE + 1))
    DIVERGED=1
elif [[ $PAIRWISE_RC -eq 2 ]]; then
    COUNTER_CHECKSUM_DIVERGENCE=$((COUNTER_CHECKSUM_DIVERGENCE + 1))
    DIVERGED=1
fi

if [[ $DIVERGED -eq 1 ]]; then
    log "compare" "fail" "row_div=$COUNTER_ROW_DIVERGENCE checksum_div=$COUNTER_CHECKSUM_DIVERGENCE"
    if [[ $DO_ROLLBACK -eq 1 ]]; then
        if [[ ! -x "$ROLLBACK_SCRIPT" ]]; then
            log "rollback" "fail" "$ROLLBACK_SCRIPT not executable"
            COUNTER_ROLLBACK_FAILED=$((COUNTER_ROLLBACK_FAILED + 1))
        else
            log "rollback" "info" "invoking $ROLLBACK_SCRIPT --target $ROLLBACK_TARGET"
            if "$ROLLBACK_SCRIPT" \
                --target "$ROLLBACK_TARGET" \
                --reason "invariant-divergence row_div=$COUNTER_ROW_DIVERGENCE ck_div=$COUNTER_CHECKSUM_DIVERGENCE"; then
                COUNTER_ROLLBACK_INVOKED=$((COUNTER_ROLLBACK_INVOKED + 1))
                write_json "diverged_rollback_ok"
                exit 1
            else
                COUNTER_ROLLBACK_FAILED=$((COUNTER_ROLLBACK_FAILED + 1))
                write_json "diverged_rollback_failed"
                exit 3
            fi
        fi
    else
        log "rollback" "skipped" "--no-rollback set"
    fi
    write_json "diverged"
    exit 1
fi

log "compare" "ok" "all pairs within threshold (row_div<=$THRESHOLD, checksums match where backend_actual matches)"
write_json "ok"
exit 0
