#!/usr/bin/env python3
"""
HOOK VARIANT MANAGER CLI

Interactive and command-line management of Context DNA hook variants.
Enables A/B testing, outcome tracking, and experience-based evaluation.

PHILOSOPHY:
The same rigor we apply to success patterns should apply to the hooks
that inject context. If a hook consistently hurts outcomes, we should
know and fix it.

A/B TESTING STRATEGY:
- Control: Current production (baseline)
- Variant A: Conservative changes (minor tweaks)
- Variant B: Experimental changes (major restructuring)

Commands:
    python hook_manager.py menu              - Interactive menu
    python hook_manager.py list [hook_type]  - List all variants
    python hook_manager.py stats <variant>   - Show variant statistics
    python hook_manager.py test start <name> - Start A/B test
    python hook_manager.py test status       - Check running tests
    python hook_manager.py test conclude <id>- Conclude a test
    python hook_manager.py protect <variant> - Protect from pruning
    python hook_manager.py revert <hook>     - Revert to default
    python hook_manager.py outcome <variant> - Record manual outcome
    python hook_manager.py worst [min]       - View worst performers
    python hook_manager.py auto-prune        - Auto-prune underperformers
    python hook_manager.py compare <v1> <v2> - Compare two variants
"""

import sys
import json
from datetime import datetime
from pathlib import Path

# Ensure imports work from both context_dna package and memory/ locations
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import hook_evolution - try context_dna first, then memory/
try:
    from context_dna.hook_evolution import (
        HookEvolutionEngine,
        get_hook_evolution_engine,
        HookType,
        OutcomeType,
        ChangeMagnitude,
        HookVariant,
        ABTest,
        VariantStats,
        DEFAULT_HOOK_CONFIGS,
        PROTECTED_ASPECTS,
    )
except ImportError:
    from memory.hook_evolution import (
        HookEvolutionEngine,
        get_hook_evolution_engine,
        HookType,
        OutcomeType,
        ChangeMagnitude,
        HookVariant,
        ABTest,
        VariantStats,
        DEFAULT_HOOK_CONFIGS,
        PROTECTED_ASPECTS,
    )

# Import prompt pattern analyzer for user wisdom management
try:
    from context_dna.prompt_pattern_analyzer import (
        PromptPatternAnalyzer,
        get_analyzer,
        PatternCategory,
        PromptPattern,
        PatternStats as PromptPatternStats,
    )
    PROMPT_PATTERNS_AVAILABLE = True
except ImportError:
    try:
        from memory.prompt_pattern_analyzer import (
            PromptPatternAnalyzer,
            get_analyzer,
            PatternCategory,
            PromptPattern,
            PatternStats as PromptPatternStats,
        )
        PROMPT_PATTERNS_AVAILABLE = True
    except ImportError:
        PROMPT_PATTERNS_AVAILABLE = False


class HookManager:
    """Manager for hook variants with CLI and menu interfaces."""

    def __init__(self):
        self.engine = get_hook_evolution_engine()
        self.prompt_analyzer = get_analyzer() if PROMPT_PATTERNS_AVAILABLE else None

    def get_summary(self) -> dict:
        """Get summary statistics."""
        return self.engine.get_evolution_summary()

    def list_variants(self, hook_type: str = None, show_inactive: bool = False):
        """List variants with formatted output."""
        variants = self.engine.list_variants(hook_type, active_only=not show_inactive)

        if not variants:
            print("  No variants found.")
            return

        # Group by hook type
        by_type = {}
        for v in variants:
            if v.hook_type not in by_type:
                by_type[v.hook_type] = []
            by_type[v.hook_type].append(v)

        for htype, vlist in sorted(by_type.items()):
            print(f"\n  {htype}:")
            print("  " + "─" * 50)

            for v in vlist:
                status = "✓" if v.is_active else "✗"
                flags = []
                if v.is_default:
                    flags.append("DEFAULT")
                if v.is_protected:
                    flags.append("PROTECTED")
                if v.ab_group:
                    flags.append(f"A/B:{v.ab_group}")

                magnitude = ""
                if v.change_magnitude == "minor":
                    magnitude = " [A:minor]"
                elif v.change_magnitude == "major":
                    magnitude = " [B:major]"
                elif v.change_magnitude == "baseline":
                    magnitude = " [baseline]"

                flags_str = f" ({', '.join(flags)})" if flags else ""

                print(f"  {status} {v.variant_id}{magnitude}{flags_str}")
                print(f"      {v.name}")

                # Show quick stats
                stats = self.engine.get_variant_stats(v.variant_id)
                if stats.total_outcomes > 0:
                    print(f"      📊 {stats.total_outcomes} outcomes: "
                          f"{stats.positive_rate:.0%} positive, "
                          f"{stats.negative_rate:.0%} negative")

    def show_variant_details(self, variant_id: str):
        """Show detailed information about a variant."""
        variant = self.engine.get_variant(variant_id)
        if not variant:
            print(f"  Variant not found: {variant_id}")
            return

        stats = self.engine.get_variant_stats(variant_id)
        modifier, explanation = self.engine.get_experience_risk_modifier(variant_id)

        print(f"\n  VARIANT: {variant.variant_id}")
        print("  " + "═" * 50)
        print(f"  Name: {variant.name}")
        print(f"  Type: {variant.hook_type}")
        print(f"  Description: {variant.description or 'None'}")
        print(f"  Magnitude: {variant.change_magnitude}")
        print(f"  Active: {'Yes' if variant.is_active else 'No'}")
        print(f"  Default: {'Yes' if variant.is_default else 'No'}")
        print(f"  Protected: {'Yes' if variant.is_protected else 'No'}")
        print(f"  Version: {variant.version}")
        print(f"  Created: {variant.created_at}")
        print(f"  Created By: {variant.created_by}")

        if variant.parent_variant_id:
            print(f"  Parent: {variant.parent_variant_id}")

        if variant.ab_test_id:
            print(f"  A/B Test: {variant.ab_test_id} (group: {variant.ab_group})")

        print(f"\n  PERFORMANCE:")
        print("  " + "─" * 40)
        print(f"  Total Outcomes: {stats.total_outcomes}")
        print(f"  Experience Level: {stats.experience_level}")

        if stats.total_outcomes > 0:
            print(f"  Positive: {stats.positive_count} ({stats.positive_rate:.1%})")
            print(f"  Negative: {stats.negative_count} ({stats.negative_rate:.1%})")
            print(f"  Neutral: {stats.neutral_count}")
            print(f"  Avg Retries: {stats.avg_retry_count:.2f}")
            if stats.avg_time_ms:
                print(f"  Avg Time: {stats.avg_time_ms:.0f}ms")

        print(f"\n  RISK ASSESSMENT:")
        print("  " + "─" * 40)
        print(f"  Risk Modifier: {modifier:+.2f}")
        print(f"  Reason: {explanation}")

        print(f"\n  CONFIGURATION:")
        print("  " + "─" * 40)
        config_str = json.dumps(variant.config, indent=4)
        for line in config_str.split('\n')[:20]:
            print(f"  {line}")
        if len(config_str.split('\n')) > 20:
            print("  ... (truncated)")

    def compare_variants(self, variant_id_1: str, variant_id_2: str):
        """Compare two variants side by side."""
        v1 = self.engine.get_variant(variant_id_1)
        v2 = self.engine.get_variant(variant_id_2)

        if not v1:
            print(f"  Variant not found: {variant_id_1}")
            return
        if not v2:
            print(f"  Variant not found: {variant_id_2}")
            return

        s1 = self.engine.get_variant_stats(variant_id_1)
        s2 = self.engine.get_variant_stats(variant_id_2)

        m1, e1 = self.engine.get_experience_risk_modifier(variant_id_1)
        m2, e2 = self.engine.get_experience_risk_modifier(variant_id_2)

        print(f"\n  VARIANT COMPARISON")
        print("  " + "═" * 60)
        print(f"  {'Metric':<25} {'Variant 1':<15} {'Variant 2':<15} {'Diff':<10}")
        print("  " + "─" * 60)
        print(f"  {'Name':<25} {v1.name[:14]:<15} {v2.name[:14]:<15}")
        print(f"  {'Magnitude':<25} {v1.change_magnitude:<15} {v2.change_magnitude:<15}")
        print(f"  {'Outcomes':<25} {s1.total_outcomes:<15} {s2.total_outcomes:<15}")

        if s1.total_outcomes > 0 and s2.total_outcomes > 0:
            diff_pos = s1.positive_rate - s2.positive_rate
            diff_neg = s1.negative_rate - s2.negative_rate

            print(f"  {'Positive Rate':<25} {s1.positive_rate:.1%:<15} {s2.positive_rate:.1%:<15} {diff_pos:+.1%}")
            print(f"  {'Negative Rate':<25} {s1.negative_rate:.1%:<15} {s2.negative_rate:.1%:<15} {diff_neg:+.1%}")
            print(f"  {'Avg Retries':<25} {s1.avg_retry_count:.2f:<15} {s2.avg_retry_count:.2f:<15}")
            print(f"  {'Risk Modifier':<25} {m1:+.2f:<15} {m2:+.2f:<15}")

        print()
        if s1.positive_rate > s2.positive_rate:
            print(f"  🏆 {v1.name} has higher positive rate")
        elif s2.positive_rate > s1.positive_rate:
            print(f"  🏆 {v2.name} has higher positive rate")
        else:
            print("  🤝 Both variants have similar positive rates")

    def show_ab_tests(self, status: str = None):
        """Show A/B/C tests."""
        tests = self.engine.list_ab_tests(status=status)

        if not tests:
            print("  No A/B/C tests found.")
            return

        for t in tests:
            status_icon = {
                "draft": "📝",
                "running": "🔄",
                "paused": "⏸️",
                "completed": "✅",
            }.get(t.status, "❓")

            print(f"\n  {status_icon} {t.test_name}")
            print(f"     ID: {t.test_id}")
            print(f"     Hook: {t.hook_type}")
            print(f"     Status: {t.status}")
            print(f"     Control: {t.control_variant_id}")
            if t.variant_a_id:
                print(f"     Variant A (minor): {t.variant_a_id}")
            if t.variant_b_id:
                print(f"     Variant B (major): {t.variant_b_id}")
            if t.variant_c_id:
                print(f"     Variant C (wisdom): {t.variant_c_id}")
            if t.wisdom_injection_enabled:
                print(f"     Variant C: Dynamic wisdom injection ✨")

            if t.status == "running":
                # Show current stats
                sig = self.engine.check_significance(t.test_id)
                print(f"     Samples needed: {t.min_samples_per_variant}")
                if sig.get("control"):
                    print(f"     Control: {sig['control']['total']} samples, {sig['control']['positive_rate']:.1%}" if sig['control']['positive_rate'] else f"     Control: {sig['control']['total']} samples")
                if sig.get("variant_a"):
                    print(f"     Var A: {sig['variant_a']['total']} samples, {sig['variant_a']['positive_rate']:.1%}" if sig['variant_a']['positive_rate'] else f"     Var A: {sig['variant_a']['total']} samples")
                if sig.get("variant_b"):
                    print(f"     Var B: {sig['variant_b']['total']} samples, {sig['variant_b']['positive_rate']:.1%}" if sig['variant_b']['positive_rate'] else f"     Var B: {sig['variant_b']['total']} samples")
                if sig.get("variant_c"):
                    wisdom_note = " (wisdom)" if sig['variant_c'].get('is_wisdom_injection') else ""
                    print(f"     Var C{wisdom_note}: {sig['variant_c']['total']} samples, {sig['variant_c']['positive_rate']:.1%}" if sig['variant_c']['positive_rate'] else f"     Var C{wisdom_note}: {sig['variant_c']['total']} samples")
                print(f"     Recommendation: {sig.get('recommendation', 'N/A')}")

            if t.winner_variant_id:
                print(f"     Winner: {t.winner_variant_id}")

    def show_worst_performers(self, min_outcomes: int = 10):
        """Show worst performing variants."""
        worst = self.engine.get_worst_performing_variants(min_outcomes=min_outcomes)

        if not worst:
            print(f"  No variants with {min_outcomes}+ outcomes to evaluate.")
            return

        print(f"\n  WORST PERFORMING VARIANTS (min {min_outcomes} outcomes)")
        print("  " + "═" * 55)

        for i, w in enumerate(worst, 1):
            neg_bar = "█" * int(w['negative_rate'] * 20)
            print(f"\n  {i}. {w['variant_id']}")
            print(f"     {w['hook_type']}: {w['name']}")
            print(f"     Negative Rate: [{neg_bar:<20}] {w['negative_rate']:.1%}")
            print(f"     ({w['negative_count']}/{w['total_outcomes']} outcomes)")


def interactive_menu():
    """Interactive hook management menu."""
    manager = HookManager()

    while True:
        print("\n" + "=" * 60)
        print("  HOOK VARIANT MANAGER")
        print("=" * 60)

        summary = manager.get_summary()
        print(f"\n  Active Hooks: {len(summary['hook_types'])} | Variants: {summary['active_variants']}/{summary['total_variants']} | Tests: {summary['running_tests']} running")

        # Show health warning
        worst = manager.engine.get_worst_performing_variants(min_outcomes=10)
        underperformers = [w for w in worst if w['negative_rate'] > 0.3]
        if underperformers:
            print(f"\n  ⚠️  HOOK HEALTH: {len(underperformers)} variants underperforming (>30% negative)")

        print("\n  VARIANT MANAGEMENT:")
        print("  ───────────────────")
        print("  1. List all variants")
        print("  2. Create new variant (A=minor, B=major)")
        print("  3. Clone existing variant")
        print("  4. View variant details")
        print("  5. Deactivate variant")

        print("\n  A/B TESTING:")
        print("  ────────────")
        print("  6. Start new A/B test")
        print("  7. View A/B tests")
        print("  8. Check test significance")
        print("  9. Conclude test (promote winner)")

        print("\n  PERFORMANCE ANALYSIS:")
        print("  ─────────────────────")
        print("  10. View variant statistics")
        print("  11. Compare two variants")
        print("  12. View worst performers")
        print("  13. Record manual outcome")

        print("\n  PROTECTION & DEFAULTS:")
        print("  ───────────────────────")
        print("  14. Protect variant from pruning")
        print("  15. Set new default")
        print("  16. Revert to system default")
        print("  17. Auto-prune underperformers")

        if PROMPT_PATTERNS_AVAILABLE:
            print("\n  USER WISDOM PATTERNS (C Variant):")
            print("  ───────────────────────────────────")
            print("  18. List user prompt patterns")
            print("  19. View pattern effectiveness")
            print("  20. Test wisdom injection")
            print("  21. Create custom pattern")
            print("  22. Start A/B/C test with wisdom")

        print("\n  q. Quit")

        choice = input("\n  Enter choice: ").strip().lower()

        if choice == "q":
            print("\n  Goodbye!")
            break

        elif choice == "1":
            # List variants
            hook_type = input("\n  Filter by hook type (or press Enter for all): ").strip()
            show_inactive = input("  Show inactive? (y/N): ").strip().lower() == "y"
            manager.list_variants(hook_type if hook_type else None, show_inactive)

        elif choice == "2":
            # Create variant
            print("\n  CREATE NEW VARIANT")
            print("  ──────────────────")
            print("  Available hook types:")
            for ht in HookType:
                print(f"    - {ht.value}")

            hook_type = input("\n  Hook type: ").strip()
            if not hook_type:
                continue

            name = input("  Variant name: ").strip()
            if not name:
                continue

            description = input("  Description (what's different): ").strip()

            print("\n  Change magnitude:")
            print("    minor (A) - Small phrasing tweaks")
            print("    major (B) - Dramatic structural changes")
            magnitude = input("  Magnitude [minor]: ").strip() or "minor"

            # Start from default config
            default = manager.engine.get_default_variant(hook_type)
            if default:
                print(f"\n  Starting from default config for {hook_type}")
                config = default.config.copy()

                # Let user modify
                print("  (Config modification would be interactive here)")
                print("  Using default config for now...")
            else:
                config = {}

            variant_id = manager.engine.create_variant(
                hook_type=hook_type,
                name=name,
                config=config,
                description=description,
                magnitude=magnitude
            )
            print(f"\n  ✓ Created variant: {variant_id}")

        elif choice == "3":
            # Clone variant
            source_id = input("\n  Source variant ID to clone: ").strip()
            source = manager.engine.get_variant(source_id)
            if not source:
                print("  Variant not found.")
                continue

            new_name = input(f"  New name (based on '{source.name}'): ").strip()
            if not new_name:
                continue

            magnitude = input("  Magnitude [minor]: ").strip() or "minor"
            description = input("  Description: ").strip()

            variant_id = manager.engine.create_variant(
                hook_type=source.hook_type,
                name=new_name,
                config=source.config.copy(),
                description=description,
                magnitude=magnitude,
                parent_id=source_id
            )
            print(f"\n  ✓ Cloned to: {variant_id}")

        elif choice == "4":
            # View details
            variant_id = input("\n  Variant ID: ").strip()
            if variant_id:
                manager.show_variant_details(variant_id)

        elif choice == "5":
            # Deactivate
            variant_id = input("\n  Variant ID to deactivate: ").strip()
            reason = input("  Reason: ").strip()
            if manager.engine.deactivate_variant(variant_id, reason):
                print(f"\n  ✓ Deactivated: {variant_id}")
            else:
                print("  ✗ Failed (may be protected or default)")

        elif choice == "6":
            # Start A/B test
            print("\n  START A/B TEST")
            print("  ───────────────")

            test_name = input("  Test name: ").strip()
            if not test_name:
                continue

            hook_type = input("  Hook type: ").strip()
            control_id = input("  Control variant ID: ").strip()
            a_id = input("  Variant A ID (minor changes, optional): ").strip() or None
            b_id = input("  Variant B ID (major changes, optional): ").strip() or None

            if not a_id and not b_id:
                print("  Need at least one test variant (A or B)")
                continue

            min_samples = input("  Min samples per variant [30]: ").strip()
            min_samples = int(min_samples) if min_samples else 30

            test_id = manager.engine.create_ab_test(
                test_name=test_name,
                hook_type=hook_type,
                control_variant_id=control_id,
                variant_a_id=a_id,
                variant_b_id=b_id,
                min_samples=min_samples
            )

            start = input("\n  Start test now? (Y/n): ").strip().lower() != "n"
            if start:
                if manager.engine.start_ab_test(test_id):
                    print(f"\n  ✓ Test started: {test_id}")
                else:
                    print("  ✗ Failed to start test")
            else:
                print(f"\n  Test created (draft): {test_id}")
                print("  Use 'start' command to begin")

        elif choice == "7":
            # View tests
            status_filter = input("\n  Filter by status (running/completed/all): ").strip()
            if status_filter == "all":
                status_filter = None
            manager.show_ab_tests(status_filter if status_filter else None)

        elif choice == "8":
            # Check significance
            test_id = input("\n  Test ID: ").strip()
            if test_id:
                sig = manager.engine.check_significance(test_id)
                print(f"\n  TEST: {test_id}")
                print("  " + "─" * 40)
                print(f"  Has enough samples: {sig.get('has_enough_samples', False)}")
                print(f"  Is significant: {sig.get('is_significant', False)}")
                if sig.get('winner'):
                    print(f"  Winner: {sig['winner']}")
                if sig.get('difference'):
                    print(f"  Difference: {sig['difference']:.1%}")
                print(f"  Recommendation: {sig.get('recommendation', 'N/A')}")

        elif choice == "9":
            # Conclude test
            test_id = input("\n  Test ID to conclude: ").strip()
            winner_id = input("  Winner variant ID (or Enter for auto): ").strip() or None
            promote = input("  Promote winner to default? (Y/n): ").strip().lower() != "n"

            if manager.engine.conclude_ab_test(test_id, winner_id, promote):
                print(f"\n  ✓ Test concluded")
                if promote:
                    print("  Winner promoted to default")
            else:
                print("  ✗ Failed to conclude test")

        elif choice == "10":
            # View stats
            variant_id = input("\n  Variant ID: ").strip()
            if variant_id:
                manager.show_variant_details(variant_id)

        elif choice == "11":
            # Compare variants
            v1 = input("\n  First variant ID: ").strip()
            v2 = input("  Second variant ID: ").strip()
            if v1 and v2:
                manager.compare_variants(v1, v2)

        elif choice == "12":
            # Worst performers
            min_outcomes = input("\n  Minimum outcomes [10]: ").strip()
            min_outcomes = int(min_outcomes) if min_outcomes else 10
            manager.show_worst_performers(min_outcomes)

        elif choice == "13":
            # Record outcome
            print("\n  RECORD MANUAL OUTCOME")
            print("  ─────────────────────")
            variant_id = input("  Variant ID: ").strip()
            session_id = input("  Session ID: ").strip() or datetime.now().isoformat()

            print("\n  Outcome types:")
            print("    positive - Hook clearly helped")
            print("    negative - Hook hurt (errors, retries)")
            print("    neutral  - No measurable impact")
            outcome = input("  Outcome: ").strip()

            if outcome not in ("positive", "negative", "neutral"):
                print("  Invalid outcome")
                continue

            context = input("  Context (what happened): ").strip()
            signals = input("  Signals (comma-separated): ").strip().split(",")
            signals = [s.strip() for s in signals if s.strip()]

            confidence = input("  Confidence [0.7]: ").strip()
            confidence = float(confidence) if confidence else 0.7

            if manager.engine.record_outcome(
                variant_id=variant_id,
                session_id=session_id,
                outcome=outcome,
                signals=signals,
                confidence=confidence,
                trigger_context=context
            ):
                print(f"\n  ✓ Outcome recorded")
            else:
                print("  ✗ Failed to record outcome")

        elif choice == "14":
            # Protect variant
            variant_id = input("\n  Variant ID to protect: ").strip()
            if manager.engine.protect_variant(variant_id, True):
                print(f"\n  ✓ Protected: {variant_id}")
            else:
                print("  ✗ Failed")

        elif choice == "15":
            # Set default
            variant_id = input("\n  Variant ID to make default: ").strip()
            if manager.engine.set_as_default(variant_id):
                print(f"\n  ✓ Set as default: {variant_id}")
            else:
                print("  ✗ Failed")

        elif choice == "16":
            # Revert to default
            print("\n  Available hook types:")
            for ht in HookType:
                print(f"    - {ht.value}")

            hook_type = input("\n  Hook type to revert: ").strip()
            if hook_type:
                restored = manager.engine.revert_to_default(hook_type)
                if restored:
                    print(f"\n  ✓ Reverted to system default: {restored}")
                else:
                    print("  ✗ Failed to revert")

        elif choice == "17":
            # Auto-prune
            print("\n  AUTO-PRUNE UNDERPERFORMERS")
            print("  ──────────────────────────")
            min_outcomes = input("  Minimum outcomes [20]: ").strip()
            min_outcomes = int(min_outcomes) if min_outcomes else 20

            max_neg = input("  Maximum negative rate [0.35]: ").strip()
            max_neg = float(max_neg) if max_neg else 0.35

            # Dry run first
            to_prune = manager.engine.auto_prune_underperformers(
                min_outcomes=min_outcomes,
                max_negative_rate=max_neg,
                dry_run=True
            )

            if not to_prune:
                print("\n  No variants qualify for pruning.")
                continue

            print(f"\n  Would prune {len(to_prune)} variants:")
            for vid in to_prune:
                v = manager.engine.get_variant(vid)
                s = manager.engine.get_variant_stats(vid)
                print(f"    - {vid}")
                if v:
                    print(f"      {v.name} ({s.negative_rate:.1%} negative)")

            confirm = input("\n  Proceed with pruning? (y/N): ").strip().lower() == "y"
            if confirm:
                pruned = manager.engine.auto_prune_underperformers(
                    min_outcomes=min_outcomes,
                    max_negative_rate=max_neg,
                    dry_run=False
                )
                print(f"\n  ✓ Pruned {len(pruned)} variants")
            else:
                print("  Cancelled.")

        # =====================================================================
        # USER WISDOM PATTERNS (C Variant) - Options 18-22
        # =====================================================================

        elif choice == "18" and PROMPT_PATTERNS_AVAILABLE:
            # List prompt patterns
            print("\n  USER PROMPT PATTERNS")
            print("  ────────────────────")

            category_filter = input("  Filter by category (or Enter for all): ").strip() or None

            patterns = manager.prompt_analyzer.list_patterns(category=category_filter)

            if not patterns:
                print("  No patterns found.")
                continue

            # Group by category
            by_cat = {}
            for p in patterns:
                if p.category not in by_cat:
                    by_cat[p.category] = []
                by_cat[p.category].append(p)

            for cat, plist in sorted(by_cat.items()):
                print(f"\n  {cat.upper()}:")
                print("  " + "─" * 45)
                for p in plist:
                    stats = manager.prompt_analyzer.get_pattern_stats(p.pattern_id)
                    status = "✓" if p.is_active else "✗"
                    protected = " [PROTECTED]" if p.is_protected else ""
                    eff = f" ({stats.effectiveness})" if stats.total_sessions > 0 else " (no data)"
                    print(f"    {status} {p.pattern_id}{protected}{eff}")
                    print(f"       {p.name}")
                    if stats.total_sessions > 0:
                        print(f"       📊 {stats.total_sessions} sessions, {stats.positive_rate:.0%} positive")

        elif choice == "19" and PROMPT_PATTERNS_AVAILABLE:
            # View pattern effectiveness
            print("\n  PATTERN EFFECTIVENESS RANKING")
            print("  ─────────────────────────────")

            min_sessions = input("  Minimum sessions [5]: ").strip()
            min_sessions = int(min_sessions) if min_sessions else 5

            effective = manager.prompt_analyzer.get_effective_patterns(min_sessions)

            if not effective:
                print(f"\n  No patterns with {min_sessions}+ sessions yet.")
                print("  Patterns need outcome data from actual sessions to rank.")
                continue

            print(f"\n  Top patterns by positive outcome rate (min {min_sessions} sessions):\n")
            for i, p in enumerate(effective, 1):
                bar = "█" * int(p['positive_rate'] * 20)
                print(f"  {i}. {p['pattern_id']} ({p['category']})")
                print(f"     {p['name']}")
                print(f"     Positive Rate: [{bar:<20}] {p['positive_rate']:.1%}")
                print(f"     ({p['total_sessions']} sessions)")
                print()

        elif choice == "20" and PROMPT_PATTERNS_AVAILABLE:
            # Test wisdom injection
            print("\n  TEST WISDOM INJECTION")
            print("  ─────────────────────")

            prompt = input("\n  Enter a sample prompt: ").strip()
            if not prompt:
                continue

            risk = input("  Risk level (critical/high/moderate/low) [moderate]: ").strip() or "moderate"

            # Show detected patterns
            patterns = manager.prompt_analyzer.extract_patterns(prompt)
            print(f"\n  Detected {len(patterns)} patterns in prompt:")
            for p in patterns:
                print(f"    - {p.pattern_id}: \"{p.matched_text}\" ({p.confidence:.2f})")

            # Generate injection
            injection = manager.prompt_analyzer.generate_wisdom_injection(prompt, risk)

            if not injection:
                print("\n  No injection generated.")
                print("  (Need patterns with 5+ sessions and >50% positive rate)")
            else:
                print(f"\n  GENERATED INJECTION:")
                print("  " + "═" * 55)
                print(f"  Patterns used: {', '.join(injection.patterns_used)}")
                print(f"  Confidence: {injection.confidence:.1%}")
                print(f"  Risk modifier: {injection.risk_modifier:+.2f}")
                print()
                print(injection.injection_text)

        elif choice == "21" and PROMPT_PATTERNS_AVAILABLE:
            # Create custom pattern
            print("\n  CREATE CUSTOM PATTERN")
            print("  ─────────────────────")

            print("\n  Available categories:")
            for cat in PatternCategory:
                print(f"    - {cat.value}")

            category = input("\n  Category: ").strip()
            if not category:
                continue

            name = input("  Pattern name: ").strip()
            if not name:
                continue

            print("\n  Enter regex patterns (one per line, empty line to finish):")
            regex_patterns = []
            while True:
                regex = input("    regex> ").strip()
                if not regex:
                    break
                regex_patterns.append(regex)

            if not regex_patterns:
                print("  Need at least one regex pattern.")
                continue

            injection = input("\n  Injection template (the text to inject): ").strip()
            if not injection:
                continue

            description = input("  Description (optional): ").strip()

            pattern_id = manager.prompt_analyzer.create_pattern(
                category=category,
                name=name,
                regex_patterns=regex_patterns,
                injection_template=injection,
                description=description,
                created_by="user"
            )

            print(f"\n  ✓ Created pattern: {pattern_id}")

        elif choice == "22" and PROMPT_PATTERNS_AVAILABLE:
            # Start A/B/C test with wisdom injection
            print("\n  START A/B/C TEST WITH WISDOM INJECTION")
            print("  ───────────────────────────────────────")
            print("  This creates a test where the C variant uses dynamic")
            print("  wisdom injection based on user prompt patterns.")
            print()

            test_name = input("  Test name: ").strip()
            if not test_name:
                continue

            hook_type = input("  Hook type [UserPromptSubmit]: ").strip() or "UserPromptSubmit"
            control_id = input("  Control variant ID: ").strip()
            if not control_id:
                # Get default
                default = manager.engine.get_default_variant(hook_type)
                control_id = default.variant_id if default else f"{hook_type.lower()}_default"
                print(f"  Using default: {control_id}")

            a_id = input("  Variant A ID (minor changes, optional): ").strip() or None
            b_id = input("  Variant B ID (major changes, optional): ").strip() or None

            print("\n  C Variant will use DYNAMIC WISDOM INJECTION")
            print("  (Injects proven user patterns based on context)")

            min_samples = input("\n  Min samples per variant [30]: ").strip()
            min_samples = int(min_samples) if min_samples else 30

            test_id = manager.engine.create_ab_test(
                test_name=test_name,
                hook_type=hook_type,
                control_variant_id=control_id,
                variant_a_id=a_id,
                variant_b_id=b_id,
                variant_c_id=None,
                wisdom_injection_enabled=True,
                min_samples=min_samples
            )

            print(f"\n  ✓ Test created: {test_id}")
            print("  C variant will inject user wisdom patterns dynamically")

            start = input("\n  Start test now? (Y/n): ").strip().lower() != "n"
            if start:
                if manager.engine.start_ab_test(test_id):
                    print(f"\n  ✓ A/B/C test started!")
                    print("  Distribution: 40% control, 20% A, 20% B, 20% C (wisdom)")
                else:
                    print("  ✗ Failed to start test")


def main():
    """Main CLI entry point."""
    if len(sys.argv) < 2:
        interactive_menu()
        return

    cmd = sys.argv[1]
    manager = HookManager()

    if cmd == "menu":
        interactive_menu()

    elif cmd == "list":
        hook_type = sys.argv[2] if len(sys.argv) > 2 else None
        manager.list_variants(hook_type)

    elif cmd == "stats":
        if len(sys.argv) < 3:
            print("Usage: hook_manager.py stats <variant_id>")
            sys.exit(1)
        manager.show_variant_details(sys.argv[2])

    elif cmd == "compare":
        if len(sys.argv) < 4:
            print("Usage: hook_manager.py compare <variant1> <variant2>")
            sys.exit(1)
        manager.compare_variants(sys.argv[2], sys.argv[3])

    elif cmd == "worst":
        min_outcomes = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        manager.show_worst_performers(min_outcomes)

    elif cmd == "test":
        if len(sys.argv) < 3:
            print("Usage: hook_manager.py test <start|status|conclude> [args]")
            sys.exit(1)

        subcmd = sys.argv[2]

        if subcmd == "status":
            manager.show_ab_tests()

        elif subcmd == "start":
            if len(sys.argv) < 4:
                print("Usage: hook_manager.py test start <test_id>")
                sys.exit(1)
            test_id = sys.argv[3]
            if manager.engine.start_ab_test(test_id):
                print(f"✓ Test started: {test_id}")
            else:
                print("✗ Failed to start test")

        elif subcmd == "conclude":
            if len(sys.argv) < 4:
                print("Usage: hook_manager.py test conclude <test_id> [winner_id]")
                sys.exit(1)
            test_id = sys.argv[3]
            winner_id = sys.argv[4] if len(sys.argv) > 4 else None
            if manager.engine.conclude_ab_test(test_id, winner_id):
                print(f"✓ Test concluded: {test_id}")
            else:
                print("✗ Failed to conclude test")

        elif subcmd == "check":
            if len(sys.argv) < 4:
                print("Usage: hook_manager.py test check <test_id>")
                sys.exit(1)
            test_id = sys.argv[3]
            sig = manager.engine.check_significance(test_id)
            print(json.dumps(sig, indent=2))

        else:
            print(f"Unknown test command: {subcmd}")
            sys.exit(1)

    elif cmd == "protect":
        if len(sys.argv) < 3:
            print("Usage: hook_manager.py protect <variant_id>")
            sys.exit(1)
        if manager.engine.protect_variant(sys.argv[2], True):
            print(f"✓ Protected: {sys.argv[2]}")
        else:
            print("✗ Failed")

    elif cmd == "revert":
        if len(sys.argv) < 3:
            print("Usage: hook_manager.py revert <hook_type>")
            sys.exit(1)
        restored = manager.engine.revert_to_default(sys.argv[2])
        if restored:
            print(f"✓ Reverted to: {restored}")
        else:
            print("✗ Failed to revert")

    elif cmd == "auto-prune":
        to_prune = manager.engine.auto_prune_underperformers(dry_run=True)
        if not to_prune:
            print("No variants qualify for pruning.")
            return

        print(f"Would prune: {', '.join(to_prune)}")
        confirm = input("Proceed? (y/N): ").strip().lower() == "y"
        if confirm:
            pruned = manager.engine.auto_prune_underperformers(dry_run=False)
            print(f"✓ Pruned: {len(pruned)} variants")

    elif cmd == "outcome":
        if len(sys.argv) < 4:
            print("Usage: hook_manager.py outcome <variant_id> <positive|negative|neutral>")
            sys.exit(1)
        variant_id = sys.argv[2]
        outcome = sys.argv[3]
        session_id = sys.argv[4] if len(sys.argv) > 4 else datetime.now().isoformat()

        if manager.engine.record_outcome(
            variant_id=variant_id,
            session_id=session_id,
            outcome=outcome,
            signals=["manual_recording"],
            confidence=0.8
        ):
            print(f"✓ Recorded: {outcome} for {variant_id}")
        else:
            print("✗ Failed to record outcome")

    elif cmd == "patterns" and PROMPT_PATTERNS_AVAILABLE:
        # List or manage prompt patterns
        analyzer = get_analyzer()
        subcmd = sys.argv[2] if len(sys.argv) > 2 else "list"

        if subcmd == "list":
            patterns = analyzer.list_patterns()
            by_cat = {}
            for p in patterns:
                if p.category not in by_cat:
                    by_cat[p.category] = []
                by_cat[p.category].append(p)

            for cat, plist in sorted(by_cat.items()):
                print(f"\n{cat.upper()}:")
                for p in plist:
                    stats = analyzer.get_pattern_stats(p.pattern_id)
                    eff = f" ({stats.positive_rate:.0%} positive)" if stats.total_sessions > 0 else ""
                    print(f"  {p.pattern_id}: {p.name}{eff}")

        elif subcmd == "effective":
            min_sessions = int(sys.argv[3]) if len(sys.argv) > 3 else 5
            effective = analyzer.get_effective_patterns(min_sessions)
            if not effective:
                print(f"No patterns with {min_sessions}+ sessions")
            else:
                for p in effective:
                    print(f"{p['pattern_id']}: {p['positive_rate']:.0%} ({p['total_sessions']} sessions)")

        elif subcmd == "test":
            if len(sys.argv) < 4:
                print("Usage: hook_manager.py patterns test \"prompt text\" [risk_level]")
                sys.exit(1)
            prompt = sys.argv[3]
            risk = sys.argv[4] if len(sys.argv) > 4 else "moderate"
            injection = analyzer.generate_wisdom_injection(prompt, risk)
            if injection:
                print(f"Patterns: {', '.join(injection.patterns_used)}")
                print(f"Confidence: {injection.confidence:.1%}")
                print(injection.injection_text)
            else:
                print("No injection generated")

        else:
            print(f"Unknown patterns command: {subcmd}")
            print("Available: list, effective, test")

    elif cmd == "abc":
        # Create A/B/C test with wisdom injection
        if len(sys.argv) < 4:
            print("Usage: hook_manager.py abc <test_name> <control_variant_id> [--start]")
            sys.exit(1)

        test_name = sys.argv[2]
        control_id = sys.argv[3]
        start_now = "--start" in sys.argv

        test_id = manager.engine.create_ab_test(
            test_name=test_name,
            hook_type="UserPromptSubmit",
            control_variant_id=control_id,
            wisdom_injection_enabled=True
        )
        print(f"Created A/B/C test: {test_id}")

        if start_now:
            if manager.engine.start_ab_test(test_id):
                print("Test started (40% control, 20% A, 20% B, 20% C wisdom)")
            else:
                print("Failed to start test")

    else:
        print(f"Unknown command: {cmd}")
        print("\nAvailable commands:")
        print("  menu              - Interactive menu")
        print("  list [hook_type]  - List variants")
        print("  stats <variant>   - Show variant statistics")
        print("  compare <v1> <v2> - Compare two variants")
        print("  worst [min]       - View worst performers")
        print("  test start <id>   - Start an A/B/C test")
        print("  test status       - View test status")
        print("  test conclude <id>- Conclude a test")
        print("  test check <id>   - Check test significance")
        print("  protect <variant> - Protect from pruning")
        print("  revert <hook>     - Revert to default")
        print("  auto-prune        - Auto-prune underperformers")
        print("  outcome <v> <out> - Record manual outcome")
        if PROMPT_PATTERNS_AVAILABLE:
            print("\n  Prompt Pattern Commands:")
            print("  patterns list     - List all prompt patterns")
            print("  patterns effective- View effective patterns")
            print("  patterns test <p> - Test wisdom injection")
            print("  abc <name> <ctrl> - Create A/B/C test with wisdom")
        sys.exit(1)


if __name__ == "__main__":
    main()
