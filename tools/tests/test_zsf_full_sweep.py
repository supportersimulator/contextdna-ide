"""RACE B1 — Zero-Silent-Failures regression guards (full sweep).

Companion to ``test_daemon_zsf.py`` (which guards only
``tools/fleet_nerve_nats.py``). This file extends the invariant across
the four production paths that still carried bare ``except Exception:
pass`` before branch ``race/b1-zsf-full-sweep``:

* ``tools/fleet_nerve_nats.py``                    (3 sites fixed here)
* ``multi-fleet/multifleet/nats_client.py``        (15 sites fixed)
* ``multi-fleet/multifleet/self_heal.py``          (1 site fixed)
* ``multi-fleet/multifleet/discord_bridge.py``     (2 sites fixed)

The invariant (from project CLAUDE.md, "ZERO SILENT FAILURES"):

    No failure persists silently. Every exception → observable channel
    (health, log, Redis, gains-gate). ``except Exception: pass`` forbidden.
    Always record what failed + how many times.

These tests act as a *gate*: any future commit that re-introduces a bare
swallow in these files fails the suite and must be justified in review.

The unit-level counter-coverage tests live in ``test_daemon_zsf.py``
for the daemon. For the multifleet module the cost/benefit of
instantiating ``FleetNerveNATS`` in a unit test is poor (requires NATS
connection, many optional subsystems) — so we verify structurally that
the new counters are initialised in ``_stats`` and that the narrowed
except blocks increment them.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

TARGETS = {
    "tools/fleet_nerve_nats.py":
        REPO_ROOT / "tools" / "fleet_nerve_nats.py",
    "multi-fleet/multifleet/nats_client.py":
        REPO_ROOT / "multi-fleet" / "multifleet" / "nats_client.py",
    "multi-fleet/multifleet/self_heal.py":
        REPO_ROOT / "multi-fleet" / "multifleet" / "self_heal.py",
    "multi-fleet/multifleet/discord_bridge.py":
        REPO_ROOT / "multi-fleet" / "multifleet" / "discord_bridge.py",
}


# Pattern matches ``except Exception: pass`` (with whitespace / newlines
# between the colon and the ``pass``). This is the *exact* ZSF-forbidden
# form. Narrowed tuples (``except (OSError, ValueError): pass``) do not
# match and are allowed — narrowing the type is the root-cause fix.
_BARE_EXCEPT_PATTERN = re.compile(r"except\s+Exception\s*:\s*\n\s*pass\b")

# Also disallow ``except (..., Exception, ...): pass`` — Exception in a
# tuple defeats narrowing because it still catches everything.
_TUPLE_EXCEPT_PATTERN = re.compile(
    r"except\s*\([^)]*\bException\b[^)]*\)\s*:\s*\n\s*pass\b",
)


def _count_bare(src: str) -> int:
    return len(_BARE_EXCEPT_PATTERN.findall(src)) + len(
        _TUPLE_EXCEPT_PATTERN.findall(src),
    )


def test_full_sweep_no_bare_except_pass():
    """No file in TARGETS may contain ``except Exception: pass``.

    Threshold is 0 — the hardening pass eliminated every instance.
    Any regression must either be justified (counter added, test
    added) or reverted.
    """
    offenders = {}
    for name, path in TARGETS.items():
        src = path.read_text(encoding="utf-8")
        count = _count_bare(src)
        if count:
            offenders[name] = count
    assert not offenders, (
        "ZSF regression: bare `except Exception: pass` re-introduced in:\n"
        + "\n".join(f"  {n}: {c} site(s)" for n, c in offenders.items())
        + "\n\nRoot cause fix: narrow the exception type and log at "
        "WARNING+ or increment a _stats counter. See branch "
        "race/b1-zsf-full-sweep for examples."
    )


def test_nats_client_zsf_counters_declared():
    """Counters added to nats_client.FleetNerveNATS._stats must exist
    in the init dict. Otherwise /health.stats won't expose them until
    first increment, hiding failures from fleet operators."""
    src = TARGETS["multi-fleet/multifleet/nats_client.py"].read_text(
        encoding="utf-8",
    )
    required = (
        "presence_probe_decode_errors",
        "js_event_subject_errors",
        "sanitize_unavailable",
        "ack_decode_errors",
        "ack_publish_errors",
        "resubscribe_unsub_errors",
        "p5_ssh_direct_errors",
        "p5_ssh_tunnel_errors",
        "probe_overlay_update_errors",
    )
    for counter in required:
        assert f'"{counter}"' in src, (
            f"ZSF counter {counter!r} missing from nats_client _stats init"
        )


def test_fleet_nerve_nats_race_b1_counters_declared():
    """Counters added to fleet_nerve_nats.FleetNerveNATS._stats for the
    gate-publish and death-alert paths must be initialised so /health
    exposes them from first boot, not first failure."""
    src = TARGETS["tools/fleet_nerve_nats.py"].read_text(encoding="utf-8")
    for counter in ("gate_discord_publish_errors", "death_alert_discord_errors"):
        assert f'"{counter}"' in src, (
            f"ZSF counter {counter!r} missing from fleet_nerve_nats _stats init"
        )


def test_nats_client_counter_increments_wired():
    """The narrowed except blocks in nats_client.py must *increment*
    their counter, not just log. Structural check guards against a
    refactor that drops the `self._stats[...] += 1` line (which would
    revert the counter to silent).
    """
    src = TARGETS["multi-fleet/multifleet/nats_client.py"].read_text(
        encoding="utf-8",
    )
    # For each counter, at least one increment site must exist.
    increment_counters = (
        "presence_probe_decode_errors",
        "js_event_subject_errors",
        "sanitize_unavailable",
        "ack_decode_errors",
        "ack_publish_errors",
        "resubscribe_unsub_errors",
        "p5_ssh_direct_errors",
        "p5_ssh_tunnel_errors",
        "probe_overlay_update_errors",
    )
    for counter in increment_counters:
        pattern = re.compile(
            r"self\._stats\[\"" + re.escape(counter) + r"\"\]\s*\+=\s*1",
        )
        assert pattern.search(src) or re.search(
            r"daemon\._stats\[\"" + re.escape(counter) + r"\"\]\s*\+=\s*1",
            src,
        ), (
            f"Counter {counter!r} declared but never incremented — "
            f"the narrowed except block is not wired to it. "
            f"This regresses ZSF silently."
        )


def test_fleet_nerve_nats_race_b1_counter_increments_wired():
    """Same structural guard for fleet_nerve_nats.py race/b1 counters."""
    src = TARGETS["tools/fleet_nerve_nats.py"].read_text(encoding="utf-8")
    for counter in ("gate_discord_publish_errors", "death_alert_discord_errors"):
        # These use .get() fallback pattern: self._stats[k] = self._stats.get(k, 0) + 1
        pattern = re.compile(
            r"self\._stats\[\"" + re.escape(counter) + r"\"\]\s*=\s*\(?\s*"
            r"self\._stats\.get\(\"" + re.escape(counter) + r"\",\s*0\)\s*\+\s*1",
        )
        assert pattern.search(src), (
            f"Counter {counter!r} declared in _stats init but no "
            f"increment site found. Narrow-except hardening incomplete."
        )


def test_self_heal_repo_root_walk_logs_on_failure():
    """self_heal._load_tunnel_mode's REPO_ROOT parents walk used to
    swallow any exception silently. The narrowed except must call
    logger.debug with the error message so admins can diagnose why
    the path walk was skipped."""
    src = TARGETS["multi-fleet/multifleet/self_heal.py"].read_text(
        encoding="utf-8",
    )
    # The hardened block catches (OSError, RuntimeError) and logs via
    # logging.getLogger("multifleet.self_heal") at debug level.
    assert re.search(
        r"except\s*\([^)]*OSError[^)]*RuntimeError[^)]*\)[^\n]*\n"
        r"\s*#[^\n]*\n(?:\s*#[^\n]*\n)*"
        r"\s*_logger\s*=\s*logging\.getLogger",
        src,
    ), (
        "self_heal REPO_ROOT parents walk must narrow to "
        "(OSError, RuntimeError) and log via logger.debug — not "
        "silently pass."
    )


def test_discord_bridge_attachment_close_logs_on_failure():
    """discord_bridge._download_to_bytes' finally-block resp.close()
    used to swallow silently. Must now catch only (OSError,
    AttributeError) and log at debug so repeated close failures
    are visible to log analysis."""
    src = TARGETS["multi-fleet/multifleet/discord_bridge.py"].read_text(
        encoding="utf-8",
    )
    assert re.search(
        r"resp\.close\(\)\s*\n"
        r"\s*except\s*\(OSError,\s*AttributeError\)[^\n]*\n"
        r"(?:\s*#[^\n]*\n)*"
        r"\s*logger\.debug\(",
        src,
    ), (
        "discord_bridge attachment close must narrow to "
        "(OSError, AttributeError) with logger.debug — not silent pass."
    )
