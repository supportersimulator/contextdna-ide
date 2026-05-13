"""HHH8 (2026-05-12) — Stats-init contract test for webhook-related counters.

This test guards against the regression class that caused **two** webhook
silence P0s in a row (WW1 and WW7-A):

  1. WW1 (`deb9d031e`) added the offsite-NATS watchdog + 3 ZSF counters.
  2. `68bc82435 chore(merge): resolve stale UU/AA conflicts` wiped them via
     `git checkout --theirs`.
  3. Webhook silence returned. WW7-A (`7c09590db`) re-instated them.
  4. HHH8 root-causes the cycle: there was **no contract test** asserting
     the daemon's `_stats` dict initializes the webhook-* counter keys.
     Without that, a future merge can silently wipe them again — and
     `/health.stats` will simply omit the key (since /health serializes
     `self._stats` flat), making the regression invisible until the next
     cardio sentinel alarm 44 hours later.

What this test does:

- Instantiates ``FleetNerveNATS`` *without* connecting to NATS (no
  ``serve()`` call — pure ``__init__``).
- Asserts the running ``self._stats`` dict initializes every key
  CLAUDE.md's ``WEBHOOK = #1 PRIORITY`` invariant and the WW7-A audit
  list as required-present-at-startup.
- Each missing key fails the test with the audit reference that
  introduced it, so the next merge resolver knows immediately what they
  wiped and where to look (no archaeology).

This is the **pre-commit guardrail** WW7-A flagged as out-of-scope follow-up
#3. If a future merge wipes the keys, this test fails before commit
(via `gains-gate.sh` / pytest in CI), instead of 44 hours later via a
stale cardiologist finding.

ZSF: the test itself never silences failures — every missing key is named.
"""
from __future__ import annotations

import pytest


# Keys that MUST be present in FleetNerveNATS._stats at construction time.
# Maps key -> (audit reference, purpose). When a new webhook counter is
# added, append it here so a future merge wipe is caught at unit-test time.
REQUIRED_WEBHOOK_STATS_KEYS: dict = {
    # ── WW7-A (2026-05-12) offsite-NATS watchdog ──────────────────────
    # The daemon connects to a peer's NATS server instead of local
    # (libnats reconnect drift). Cluster interest propagates one hop only,
    # so producer's events to 127.0.0.1 are silently dropped at the
    # subscriber on the peer host. These three counters surface the
    # detection + corrective reconnect.
    "webhook_offsite_nats_detected": "WW7-A / WW1 — offsite NATS detection",
    "webhook_offsite_nats_reconnect_total": "WW7-A / WW1 — corrective reconnect attempts",
    "webhook_offsite_nats_reconnect_errors": "WW7-A / WW1 — close-error during reconnect",
    # ── F5 webhook watchdog — "connected but zero subs" resubscribe loop.
    # nc.is_connected==True but len(nc._subs)==0 after libnats reconnect
    # race. Without these counters, the daemon's subscription_count stays
    # 0 and webhook events are silently lost.
    "webhook_subscription_resubscribe_attempts_total": "F5 — webhook resubscribe attempts",
    "webhook_subscription_resubscribe_failures_total": "F5 — webhook resubscribe failures",
    # ── RACE S5 — webhook composite-latency budget alerts.
    "webhook_budget_exceeded_count": "RACE S5 — webhook budget exceeded count",
}


@pytest.fixture
def daemon_stats_keys() -> set:
    """Return the set of keys present in a freshly-constructed daemon's
    ``_stats`` dict. No NATS connect, no ``serve()`` — pure ``__init__``.
    """
    from tools.fleet_nerve_nats import FleetNerveNATS

    nerve = FleetNerveNATS(node_id="test-hhh8", nats_url="nats://127.0.0.1:4222")
    return set(nerve._stats.keys())


def test_webhook_stats_keys_initialized_at_construction(daemon_stats_keys: set):
    """Every required webhook-* counter must appear in ``_stats`` at init.

    Why this exists: ``/health.stats`` is built by ``**self._stats`` (see
    ``_build_health()`` around line 4746). A key absent from the init dict
    is therefore absent from ``/health.stats`` until the first increment —
    and if the increment never fires (which is the very failure mode the
    counter exists to surface), the gap is invisible. Initializing every
    counter to 0 at construction makes "0" distinguishable from "missing".
    """
    missing = {
        key: ref
        for key, ref in REQUIRED_WEBHOOK_STATS_KEYS.items()
        if key not in daemon_stats_keys
    }
    assert not missing, (
        "WW7-A regression class — required webhook counter(s) wiped from "
        "FleetNerveNATS.__init__ self._stats. /health.stats will silently "
        "omit these keys, hiding the next webhook-silence failure mode.\n"
        "Missing keys (with audit ref):\n"
        + "\n".join(f"  - {k}: {ref}" for k, ref in missing.items())
        + "\nRe-add to the self._stats = {...} block in "
        "tools/fleet_nerve_nats.py around line 2830. See "
        ".fleet/audits/2026-05-12-HHH8-webhook-silence-root-fix.md."
    )


def test_webhook_stats_keys_initialize_to_zero(daemon_stats_keys: set):
    """Counters must start at 0, not None / sentinel.

    A counter initialized to None (or absent and lazy-set later) breaks
    monotonic increment assumptions in `_stats[key] = _stats.get(key, 0) + 1`
    only if the get-default is wrong — but more importantly breaks the
    `/health.stats` flat scrape: dashboards see `null` and treat it as
    "metric down" instead of "metric at zero".
    """
    from tools.fleet_nerve_nats import FleetNerveNATS

    nerve = FleetNerveNATS(node_id="test-hhh8", nats_url="nats://127.0.0.1:4222")
    nonzero = {
        key: nerve._stats[key]
        for key in REQUIRED_WEBHOOK_STATS_KEYS
        if key in nerve._stats and nerve._stats[key] != 0
    }
    assert not nonzero, (
        f"Webhook counter(s) initialized non-zero (should be 0 at startup): "
        f"{nonzero}"
    )
