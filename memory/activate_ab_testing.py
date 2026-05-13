#!/usr/bin/env python3
"""
ACTIVATE A/B TESTING

This script activates A/B testing for Context DNA hooks.
It creates meaningful variants and starts a proper test.

The A/B testing framework exists but is DORMANT because:
1. Running tests have no variant_a defined
2. Almost no outcomes are being recorded
3. No proper control vs experimental comparison

This script fixes that by:
1. Creating a control variant (static wisdom - baseline)
2. Creating variant_a (domain-specific wisdom - focused on infrastructure)
3. Starting an A/B test comparing them
4. Logging the test_id for tracking

Usage:
    python memory/activate_ab_testing.py
    python memory/activate_ab_testing.py --status  # Check current status
    python memory/activate_ab_testing.py --stop    # Stop running tests
"""

import sys
import json
from pathlib import Path
from datetime import datetime

# Add memory to path
sys.path.insert(0, str(Path(__file__).parent))

from hook_evolution import get_hook_evolution_engine, DEFAULT_HOOK_CONFIGS


def check_status():
    """Check current A/B testing status."""
    engine = get_hook_evolution_engine()

    print("=" * 60)
    print("A/B TESTING STATUS")
    print("=" * 60)

    summary = engine.get_evolution_summary()
    print(f"\nVariants: {summary['active_variants']} active / {summary['total_variants']} total")
    print(f"Protected: {summary['protected_variants']}")
    print(f"Running Tests: {summary['running_tests']}")
    print(f"Total Outcomes Recorded: {summary['total_outcomes']}")

    # List all tests
    tests = engine.list_ab_tests()
    if tests:
        print(f"\n--- All Tests ({len(tests)}) ---")
        for t in tests:
            status_marker = "[RUNNING]" if t.status == "running" else f"[{t.status}]"
            print(f"  {status_marker} {t.test_id}")
            print(f"    Name: {t.test_name}")
            print(f"    Hook: {t.hook_type}")
            print(f"    Control: {t.control_variant_id}")
            print(f"    Variant A: {t.variant_a_id or 'NONE'}")
            print(f"    Variant B: {t.variant_b_id or 'NONE'}")
            if t.wisdom_injection_enabled:
                print(f"    Wisdom Injection: ENABLED")
            print()

    # List variants by hook type
    print("--- Variants by Hook Type ---")
    for hook_type in ["UserPromptSubmit", "PostToolUse", "SessionEnd", "GitPostCommit"]:
        variants = engine.list_variants(hook_type)
        print(f"\n{hook_type}:")
        for v in variants:
            markers = []
            if v.is_default:
                markers.append("DEFAULT")
            if v.is_protected:
                markers.append("PROTECTED")
            if v.ab_group:
                markers.append(f"AB:{v.ab_group}")
            marker_str = f" [{', '.join(markers)}]" if markers else ""
            print(f"  - {v.variant_id}{marker_str}")
            print(f"    {v.name} ({v.change_magnitude})")


def stop_running_tests():
    """Stop all running tests."""
    engine = get_hook_evolution_engine()

    tests = engine.list_ab_tests(status="running")
    if not tests:
        print("No running tests to stop.")
        return

    for test in tests:
        print(f"Stopping test: {test.test_id}")
        # Mark as paused rather than completed (no winner)
        engine.db.execute("""
            UPDATE hook_ab_tests SET status = 'paused' WHERE test_id = ?
        """, (test.test_id,))

        # Clear AB groups from variants
        for vid in [test.control_variant_id, test.variant_a_id, test.variant_b_id, test.variant_c_id]:
            if vid:
                engine.db.execute("""
                    UPDATE hook_variants SET ab_group = NULL, ab_test_id = NULL
                    WHERE variant_id = ?
                """, (vid,))

    engine.db.commit()
    print(f"Stopped {len(tests)} tests.")


def create_domain_specific_variant(engine):
    """
    Create a domain-specific wisdom variant for infrastructure tasks.

    This variant adds extra context about:
    - AWS infrastructure patterns
    - Docker/container gotchas
    - Database migration safety
    """
    # Get base config and modify it
    base_config = DEFAULT_HOOK_CONFIGS["UserPromptSubmit"].copy()

    # Create enhanced config with domain-specific adjustments
    enhanced_config = {
        **base_config,
        "version": 2,
        "domain_focus": "infrastructure",
        "layers": {
            **base_config["layers"],
            # Enable brain state for more risk levels
            "brain_state": {"enabled": True, "for_risk": ["critical", "high", "moderate"]},
            # Add extra gotcha checking for infrastructure
            "gotcha_check": {"enabled": True, "for_risk": ["critical", "high", "moderate", "low"]},
        },
        "extra_wisdom": {
            "infrastructure": [
                "AWS ASG gives new IPs on restart - use NLB for stable endpoints",
                "docker restart doesn't reload env vars - must recreate container",
                "WebRTC needs direct UDP - disable Cloudflare proxy for those records",
                "boto3 streaming is per-chunk synchronous - non-streaming often faster"
            ],
            "database": [
                "Always backup before schema migrations",
                "Test migrations on staging first",
                "Large table alterations may lock - use pt-online-schema-change"
            ],
            "containers": [
                "Check HOME env var is set in containers",
                "Volume mounts need explicit permissions",
                "Health checks should be simple and fast"
            ]
        }
    }

    # Create the variant
    variant_id = engine.create_variant(
        hook_type="UserPromptSubmit",
        name="Domain Infrastructure Wisdom",
        config=enhanced_config,
        description="Enhanced variant with domain-specific infrastructure wisdom. "
                    "Includes AWS, Docker, and database gotchas proactively.",
        magnitude="minor",  # Conservative change - just more context
        parent_id="userpromptsubmit_default",
        created_by="activate_ab_testing.py"
    )

    print(f"Created variant: {variant_id}")
    return variant_id


def create_static_wisdom_control(engine):
    """
    Create a static wisdom control variant.

    This is the baseline - uses default config without domain-specific enhancements.
    """
    base_config = DEFAULT_HOOK_CONFIGS["UserPromptSubmit"].copy()

    control_config = {
        **base_config,
        "version": 2,
        "variant_type": "control",
        "wisdom_mode": "static",
        # Keep default layers exactly as-is
    }

    variant_id = engine.create_variant(
        hook_type="UserPromptSubmit",
        name="Static Wisdom Control",
        config=control_config,
        description="Control variant with static/default wisdom injection. "
                    "Baseline for comparison against domain-specific variants.",
        magnitude="baseline",
        parent_id="userpromptsubmit_default",
        created_by="activate_ab_testing.py"
    )

    print(f"Created control variant: {variant_id}")
    return variant_id


def activate_ab_testing():
    """Main activation function."""
    print("=" * 60)
    print("ACTIVATING A/B TESTING")
    print("=" * 60)
    print(f"Timestamp: {datetime.now().isoformat()}")
    print()

    engine = get_hook_evolution_engine()

    # First, stop any running tests that are incomplete
    running_tests = engine.list_ab_tests(status="running")
    if running_tests:
        print(f"Found {len(running_tests)} running tests. Pausing them...")
        for test in running_tests:
            # Check if test has actual data
            sig = engine.check_significance(test.test_id)
            control_samples = sig.get("control", {}).get("total", 0)
            a_samples = sig.get("variant_a", {}).get("total", 0) if sig.get("variant_a") else 0

            if control_samples < 5 and a_samples < 5:
                print(f"  Pausing {test.test_id} (insufficient data: {control_samples}+{a_samples} samples)")
                engine.db.execute(
                    "UPDATE hook_ab_tests SET status = 'paused' WHERE test_id = ?",
                    (test.test_id,)
                )
        engine.db.commit()
        print()

    # Create variants
    print("Creating test variants...")
    control_id = create_static_wisdom_control(engine)
    variant_a_id = create_domain_specific_variant(engine)

    # Create the A/B test
    print("\nCreating A/B test...")
    test_id = engine.create_ab_test(
        test_name="Static vs Domain-Specific Wisdom",
        hook_type="UserPromptSubmit",
        control_variant_id=control_id,
        variant_a_id=variant_a_id,
        variant_b_id=None,  # Keep it simple for now
        wisdom_injection_enabled=False,
        min_samples=30,
        confidence_threshold=0.95
    )

    print(f"Created test: {test_id}")

    # Start the test
    print("\nStarting test...")
    success = engine.start_ab_test(test_id)

    if success:
        print(f"\n{'=' * 60}")
        print("A/B TEST ACTIVATED SUCCESSFULLY")
        print(f"{'=' * 60}")
        print(f"\nTest ID: {test_id}")
        print(f"Control: {control_id}")
        print(f"Variant A: {variant_a_id}")
        print(f"\nDistribution:")
        print(f"  - 50% traffic -> Control (static wisdom)")
        print(f"  - 25% traffic -> Variant A (domain-specific)")
        print(f"  - 25% traffic -> Default (fallback)")
        print(f"\nMinimum samples needed: 30 per variant")
        print(f"\nTo check status:")
        print(f"  python memory/activate_ab_testing.py --status")
        print(f"\nTo check significance:")
        print(f"  python memory/hook_evolution.py stats {control_id}")
        print(f"  python memory/hook_evolution.py stats {variant_a_id}")

        # Log to file
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "action": "activate_ab_test",
            "test_id": test_id,
            "control_variant_id": control_id,
            "variant_a_id": variant_a_id,
            "status": "running"
        }

        log_path = Path(__file__).parent / ".ab_testing_log.json"
        logs = []
        if log_path.exists():
            try:
                logs = json.loads(log_path.read_text())
            except Exception:
                logs = []
        logs.append(log_entry)
        log_path.write_text(json.dumps(logs, indent=2))
        print(f"\nLogged to: {log_path}")

        return test_id
    else:
        print("FAILED to start test!")
        return None


def main():
    if len(sys.argv) > 1:
        if sys.argv[1] == "--status":
            check_status()
        elif sys.argv[1] == "--stop":
            stop_running_tests()
        elif sys.argv[1] == "--help":
            print(__doc__)
        else:
            print(f"Unknown argument: {sys.argv[1]}")
            print("Use --status, --stop, or --help")
    else:
        activate_ab_testing()


if __name__ == "__main__":
    main()
