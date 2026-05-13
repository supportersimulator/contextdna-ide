# Backpressure integration patch

**Status:** PATCH ONLY — do **not** apply against live `memory/*.py` until
Synaptic + 3-surgeon review signs off. Targets are the migrate3 ports
(forward-compatible) and the legacy mothership modules so both surfaces
stay in lockstep.

**Purpose:** Route every `llm_generate` call site in
`memory/anticipation_engine.py` and `memory/failure_pattern_analyzer.py`
through `contextdna_ide_oss.migrate4.backpressure.anticipation_backpressure`
so they share a single concurrency budget (2 slots default, hysteresis
resize to 1 under load).

**Sibling files referenced:**
- `contextdna_ide_oss/migrate4/backpressure/anticipation_backpressure.py` (new — this PR)
- `contextdna_ide_oss/migrate4/backpressure/circuit_breaker.py` (optional, future PR — the
  backpressure module probes for it lazily; absent = direct `llm_generate`)

---

## 1. `memory/anticipation_engine.py`

Two existing `llm_generate` call sites today (`migrate3` port lines 508-515
and 1183-1190; legacy lines 116 and 542). Both must route through the
backpressure manager.

### 1a. Add an import shim near the existing `_try_import_priority_queue` helper (~line 289)

```python
# Existing — unchanged:
def _try_import_priority_queue():
    """Return (llm_generate, Priority) or (None, None)."""
    try:
        from memory.llm_priority_queue import (  # type: ignore[import-not-found]
            Priority,
            llm_generate,
        )
        return llm_generate, Priority
    except Exception as exc:  # noqa: BLE001
        _bump("upstream_missing_llm_priority_queue_total")
        logger.debug("llm_priority_queue unavailable: %s", exc)
        return None, None


# NEW — add immediately below:
def _try_import_backpressure():
    """Return submit_under_backpressure_sync or None."""
    try:
        from contextdna_ide_oss.migrate4.backpressure.anticipation_backpressure import (  # type: ignore[import-not-found]
            submit_under_backpressure_sync,
        )
        return submit_under_backpressure_sync
    except Exception as exc:  # noqa: BLE001
        _bump("upstream_missing_backpressure_total")
        logger.debug("backpressure manager unavailable: %s", exc)
        return None
```

### 1b. Add the new counter (alongside the existing block ~line 109)

```python
COUNTERS: dict[str, Any] = {
    # ... existing entries unchanged ...
    "upstream_missing_llm_priority_queue_total": 0,
    "upstream_missing_backpressure_total": 0,   # NEW
    "llm_calls_via_backpressure_total": 0,      # NEW
    "llm_calls_direct_total": 0,                # NEW (fallback path)
}
```

### 1c. Patch `predict_next_actions` LLM call (~line 506-522)

```python
# OLD (lines 506-522):
        _bump("llm_calls_total")
        try:
            response = llm_generate(
                system_prompt=system,
                user_prompt=user,
                priority=Priority.BACKGROUND,
                profile=profile,
                caller="anticipation_engine",
                timeout_s=_LLM_TIMEOUT_S,
            )
        except TimeoutError:
            _bump("llm_call_timeouts_total")
            return self._degraded_candidates(prompt, session_id, max_candidates)
        except Exception as exc:  # noqa: BLE001
            _bump("llm_call_errors_total")
            logger.warning("anticipation LLM probe failed: %s", exc)
            return self._degraded_candidates(prompt, session_id, max_candidates)

# NEW:
        _bump("llm_calls_total")
        submit_bp = _try_import_backpressure()
        try:
            if submit_bp is not None:
                _bump("llm_calls_via_backpressure_total")
                response = submit_bp(
                    Priority.BACKGROUND,
                    {
                        "system_prompt": system,
                        "user_prompt": user,
                        "profile": profile,
                        "caller": "anticipation_engine",
                        "timeout_s": _LLM_TIMEOUT_S,
                    },
                )
            else:
                _bump("llm_calls_direct_total")
                response = llm_generate(
                    system_prompt=system,
                    user_prompt=user,
                    priority=Priority.BACKGROUND,
                    profile=profile,
                    caller="anticipation_engine",
                    timeout_s=_LLM_TIMEOUT_S,
                )
        except TimeoutError:
            _bump("llm_call_timeouts_total")
            return self._degraded_candidates(prompt, session_id, max_candidates)
        except Exception as exc:  # noqa: BLE001
            _bump("llm_call_errors_total")
            logger.warning("anticipation LLM probe failed: %s", exc)
            return self._degraded_candidates(prompt, session_id, max_candidates)
```

### 1d. Patch `_activate_superhero_anticipation` LLM call (~line 1181-1194)

```python
# OLD (lines 1181-1194), inside the for-loop over (mission/gotchas/architecture/failures):
        _bump("llm_calls_total")
        try:
            txt = llm_generate(
                system_prompt=system,
                user_prompt=f"TASK: {task[:400]}",
                priority=Priority.BACKGROUND,
                profile="extract",
                caller=f"superhero_{name}",
                timeout_s=_LLM_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001
            _bump("llm_call_errors_total")
            logger.warning("superhero %s LLM failed: %s", name, exc)
            txt = None

# NEW:
        _bump("llm_calls_total")
        submit_bp = _try_import_backpressure()
        try:
            if submit_bp is not None:
                _bump("llm_calls_via_backpressure_total")
                txt = submit_bp(
                    Priority.BACKGROUND,
                    {
                        "system_prompt": system,
                        "user_prompt": f"TASK: {task[:400]}",
                        "profile": "extract",
                        "caller": f"superhero_{name}",
                        "timeout_s": _LLM_TIMEOUT_S,
                    },
                )
            else:
                _bump("llm_calls_direct_total")
                txt = llm_generate(
                    system_prompt=system,
                    user_prompt=f"TASK: {task[:400]}",
                    priority=Priority.BACKGROUND,
                    profile="extract",
                    caller=f"superhero_{name}",
                    timeout_s=_LLM_TIMEOUT_S,
                )
        except Exception as exc:  # noqa: BLE001
            _bump("llm_call_errors_total")
            logger.warning("superhero %s LLM failed: %s", name, exc)
            txt = None
```

### 1e. Equivalent legacy mothership patch

In `memory/anticipation_engine.py` (legacy, not the migrate3 port), the
same two call sites live at lines 116 and 542. Apply the same wrap (the
`Priority` import is already there at line 49). The `_bump` /
`_try_import_backpressure` additions are identical.

---

## 2. `memory/failure_pattern_analyzer.py`

The migrate3 port does **not** call `llm_generate` today — but the legacy
analyzer's `_promote_to_landmine` path was earmarked (Synaptic 2026-04-30
roadmap) for an LLM-synthesized "landmine phrasing" pass so the
emitted warning reads naturally for the webhook injector. That hook is
the integration point and must go through backpressure from the start.

### 2a. Add the import shim near `_try_anticipation_engine` (~line 240)

```python
# Existing helpers unchanged. Add below them:

def _try_import_priority_queue():
    try:
        from memory.llm_priority_queue import (  # type: ignore[import-not-found]
            Priority, llm_generate,
        )
        return llm_generate, Priority
    except Exception as exc:  # noqa: BLE001
        _bump("upstream_missing_llm_priority_queue_total")
        logger.debug("llm_priority_queue unavailable: %s", exc)
        return None, None


def _try_import_backpressure():
    try:
        from contextdna_ide_oss.migrate4.backpressure.anticipation_backpressure import (  # type: ignore[import-not-found]
            submit_under_backpressure_sync,
        )
        return submit_under_backpressure_sync
    except Exception as exc:  # noqa: BLE001
        _bump("upstream_missing_backpressure_total")
        logger.debug("backpressure manager unavailable: %s", exc)
        return None
```

### 2b. Add counters (alongside the existing block ~line 85)

```python
COUNTERS: dict[str, Any] = {
    # ... existing entries unchanged ...
    "upstream_missing_dialogue_mirror_total": 0,
    "db_errors_total": 0,
    "upstream_missing_llm_priority_queue_total": 0,   # NEW
    "upstream_missing_backpressure_total": 0,          # NEW
    "landmine_llm_refinements_total": 0,               # NEW
    "landmine_llm_refinement_errors_total": 0,         # NEW
}
```

### 2c. New helper method on `FailurePatternAnalyzer` (insert before `_promote_to_landmine`)

```python
    def _llm_refine_landmine(self, pattern: FailurePattern) -> Optional[str]:
        """Optional: ask the local LLM to rephrase landmine_text more crisply.

        Returns ``None`` on any failure — caller falls back to the
        deterministic template. Routes through the shared backpressure
        manager so multiple analyzer runs cannot stampede the priority
        queue alongside anticipation traffic.
        """
        submit_bp = _try_import_backpressure()
        llm_generate, Priority = _try_import_priority_queue()
        if Priority is None:
            return None

        system = (
            "You are a failure-pattern editor. Rephrase this landmine "
            "warning as ONE short sentence the assistant will see before "
            "starting a task. No prose, no emoji."
        )
        user = (
            f"DOMAIN: {pattern.domain}\n"
            f"COUNT: {pattern.occurrence_count}\n"
            f"TEXT: {pattern.landmine_text[:200]}"
        )
        try:
            if submit_bp is not None:
                refined = submit_bp(
                    Priority.BACKGROUND,
                    {
                        "system_prompt": system,
                        "user_prompt": user,
                        "profile": "classify",
                        "caller": "failure_pattern_analyzer",
                        "timeout_s": 20.0,
                    },
                )
            elif llm_generate is not None:
                refined = llm_generate(
                    system_prompt=system,
                    user_prompt=user,
                    priority=Priority.BACKGROUND,
                    profile="classify",
                    caller="failure_pattern_analyzer",
                    timeout_s=20.0,
                )
            else:
                return None
        except Exception as exc:  # noqa: BLE001
            _bump("landmine_llm_refinement_errors_total")
            logger.debug("landmine LLM refinement failed: %s", exc)
            return None

        if refined:
            _bump("landmine_llm_refinements_total")
            return refined.strip()
        return None
```

### 2d. Wire it into `_promote_to_landmine` (~line 360 in `analyze_for_patterns`)

```python
# OLD (line 356-360):
        merged = self._merge_similar(patterns)
        for p in merged:
            self._store_pattern(p)
            if p.occurrence_count >= self.MIN_OCCURRENCES:
                self._promote_to_landmine(p.pattern_id)

# NEW:
        merged = self._merge_similar(patterns)
        for p in merged:
            # Optional LLM refinement of landmine_text — degrades to
            # template on any failure (ZSF).
            refined = self._llm_refine_landmine(p)
            if refined:
                p.landmine_text = refined
            self._store_pattern(p)
            if p.occurrence_count >= self.MIN_OCCURRENCES:
                self._promote_to_landmine(p.pattern_id)
```

### 2e. Equivalent legacy mothership patch

The legacy `memory/failure_pattern_analyzer.py` is at HEAD ~919 lines
without any LLM call. The wrap above is the *forward* edit — apply it
identically to legacy after the migrate3 port stabilizes.

---

## 3. Validation checklist (run after applying the patch)

1. `python -c "from contextdna_ide_oss.migrate4.backpressure.anticipation_backpressure import get_anticipation_semaphore, get_counters; print(get_counters())"` — module imports clean, counters initialized.
2. Stress test (shipped with this PR; see `tests/test_anticipation_backpressure_stress.py`): submit 20 concurrent prompts, assert max in-flight ≤ 2.
3. `./scripts/gains-gate.sh` — must stay 17/17 PASS; new counters appear under `/health.anticipation` and `/health.failure_patterns`.
4. `python memory/anticipation_engine.py` smoke test — `predict_next_actions` still degrades gracefully when the priority queue is missing (now also when backpressure is missing).
5. 3-surgeon `pre-implementation-review` HARD-GATE — Synaptic's 5-level adjustment was rejected in favor of binary hysteresis; capture that rationale in the review trail.

---

## 4. Rollback

Set `CONTEXTDNA_BACKPRESSURE_DISABLED=1` and restart. The wrapper
becomes a transparent passthrough (still bumps
`backpressure_bypass_total` so the bypass is observable on `/health`).
No code revert required.
